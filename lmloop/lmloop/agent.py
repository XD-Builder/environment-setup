"""The agent loop: OpenAI-compatible multi-round tool calling against LM Studio.

No SDK dependency — plain urllib against /v1/chat/completions with `tools`.
The loop mirrors LM Studio's .act() semantics: model responds, requested tools
run locally, results feed back, repeat until the model stops calling tools or
max_rounds is hit. Tool errors are reported back to the model as text so it
can self-correct instead of crashing the session.
"""

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import memory, tools

PROMPTS_DIR = Path(__file__).parent / "prompts"


class ServerError(RuntimeError):
    pass


# ---------------------------------------------------------------- server

def _get(base_url: str, path: str, timeout: int = 5):
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def list_models(base_url: str) -> "list[str]":
    try:
        data = _get(base_url, "/models")
        return [m["id"] for m in data.get("data", [])]
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def _native_base(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        return url[:-3]
    return url


def _get_json(url: str, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _model_matches(entry: dict, model: str) -> bool:
    ids = [
        entry.get("id", ""),
        entry.get("key", ""),
        entry.get("display_name", ""),
    ]
    return model in ids or any(model == i or model.endswith(i) or i.endswith(model) for i in ids if i)


def get_context_limit(model: str, cfg: dict) -> int:
    """Return loaded context window size in tokens. 0 if unknown."""
    manual = int(cfg.get("context_length") or 0)
    if manual > 0:
        return manual

    base = _native_base(cfg["base_url"])
    for path in ("/api/v0/models", "/api/v1/models"):
        try:
            data = _get_json(base + path)
        except (OSError, json.JSONDecodeError):
            continue
        models = data.get("data") or data.get("models") or []
        for entry in models:
            if not _model_matches(entry, model):
                continue
            for inst in entry.get("loaded_instances") or []:
                loaded = (inst.get("config") or {}).get("context_length")
                if loaded:
                    return int(loaded)
            max_ctx = entry.get("max_context_length")
            if max_ctx:
                return int(max_ctx)
    return 0


def estimate_context_tokens(messages: list) -> int:
    """Rough token estimate when the server has not reported usage yet."""
    chars = 0
    for m in messages:
        chars += len(str(m.get("content") or ""))
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"]))
    return max(0, chars // 4)


def _run_lms(args: list, echo=print, timeout: int = 60) -> bool:
    """Run an lms CLI command; echo stderr on failure. Returns True on success."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        echo(f"lms failed: {e}")
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        echo(f"lms {' '.join(args)} failed (exit {proc.returncode})"
             + (f": {err[:500]}" if err else ""))
        return False
    return True


def ensure_server(cfg: dict, echo=print) -> str:
    """Make sure LM Studio is reachable and a model is loaded. Returns model id."""
    base = cfg["base_url"]
    models = list_models(base)
    if not models and cfg.get("auto_start_server") and shutil.which("lms"):
        echo("LM Studio server not reachable — starting it with `lms server start`...")
        _run_lms(["lms", "server", "start"], echo=echo, timeout=60)
        for _ in range(20):
            time.sleep(1)
            models = list_models(base)
            if models:
                break
    if not models:
        # server may be up with nothing loaded — try loading the configured model
        if shutil.which("lms"):
            want = cfg.get("model") or ""
            echo(f"No model loaded — running `lms load {want or '(first available)'}`...")
            args = ["lms", "load", "--yes"] + ([want] if want else [])
            _run_lms(args, echo=echo, timeout=300)
            models = list_models(base)
    if not models:
        raise ServerError(
            f"No models available at {base}. Start LM Studio (or `lms server start`) "
            "and load a model (`lms load <model>`), or fix base_url via "
            "`lmloop config set base_url <url>`."
        )
    want = cfg.get("model")
    if want and want in models:
        return want
    if want:
        # configured model isn't loaded; fall back but say so
        echo(f"note: configured model '{want}' not loaded; using '{models[0]}'")
    return models[0]


# ---------------------------------------------------------------- prompts

def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in PROMPTS_DIR.glob("*.md")))
        raise FileNotFoundError(f"No skill prompt '{name}'. Available: {available}")
    return path.read_text()


def system_prompt(cfg: dict) -> str:
    base = load_prompt("system")
    ctx = memory.context_block(cfg)
    if ctx:
        base += "\n\n## Context recovery (from project memory)\n\n" + ctx
    return base


# ---------------------------------------------------------------- chat call

def _chat(cfg: dict, model: str, messages: list, tool_specs: list) -> "tuple[dict, dict]":
    payload = {
        "model": model,
        "messages": messages,
        "tools": tool_specs,
        "temperature": cfg.get("temperature", 0.7),
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout_s", 600)) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ServerError(f"LM Studio returned HTTP {e.code}: {e.read().decode()[:500]}")
    except OSError as e:
        raise ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e}")
    try:
        usage = data.get("usage") or {}
        return data["choices"][0]["message"], usage
    except (KeyError, IndexError):
        raise ServerError(f"Unexpected response shape: {json.dumps(data)[:500]}")


def _accumulate_usage(stats: "dict | None", usage: dict) -> None:
    if not stats or not usage:
        return
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
    stats["total_tokens"] = stats.get("total_tokens", 0) + int(usage.get("total_tokens") or 0)
    stats["last_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    stats["last_completion_tokens"] = int(usage.get("completion_tokens") or 0)


# ---------------------------------------------------------------- act loop

def act(cfg: dict, model: str, messages: list, session_log: "Path | None" = None,
        confirm_gate=None, echo=print, echo_tool=None, stats: "dict | None" = None) -> list:
    """Run the multi-round tool loop. Mutates and returns `messages`."""
    if echo_tool is None:
        echo_tool = lambda name, preview: echo(f"  ⚙ {name}({preview})")
    tool_specs, impls = tools.build_tools(cfg, confirm_gate=confirm_gate)
    turn_rounds = 0
    turn_tools = 0
    for round_idx in range(cfg.get("max_rounds", 25)):
        msg, usage = _chat(cfg, model, messages, tool_specs)
        _accumulate_usage(stats, usage)
        turn_rounds += 1
        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        assistant_entry = {"role": "assistant", "content": msg.get("content") or ""}
        if tool_calls:
            assistant_entry["tool_calls"] = tool_calls
        messages.append(assistant_entry)

        if content:
            echo(content)
            if session_log:
                memory.log_event(session_log, "assistant", content)

        if not tool_calls:
            if stats is not None:
                stats["turns"] = stats.get("turns", 0) + 1
                stats["rounds"] = stats.get("rounds", 0) + turn_rounds
                stats["tool_calls"] = stats.get("tool_calls", 0) + turn_tools
            return messages

        for call_idx, call in enumerate(tool_calls):
            turn_tools += 1
            fn = call.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments", "")
            arg_preview = args if len(args) <= 160 else args[:160] + "..."
            echo_tool(name, arg_preview)
            result = tools.dispatch(impls, name, args)
            if session_log:
                memory.log_event(session_log, "tool", f"{name}({arg_preview}) -> {result[:500]}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or f"call_{round_idx}_{call_idx}",
                "content": result,
            })
    if stats is not None:
        stats["turns"] = stats.get("turns", 0) + 1
        stats["rounds"] = stats.get("rounds", 0) + turn_rounds
        stats["tool_calls"] = stats.get("tool_calls", 0) + turn_tools
    echo("[lmloop] hit max_rounds — stopping. Say 'continue' to keep going.")
    return messages
