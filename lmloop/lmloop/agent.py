"""The agent loop: OpenAI-compatible multi-round tool calling against LM Studio.

No SDK dependency — plain urllib against /v1/chat/completions with `tools`.
The loop mirrors LM Studio's .act() semantics: model responds, requested tools
run locally, results feed back, repeat until the model stops calling tools or
max_rounds is hit. Tool errors are reported back to the model as text so it
can self-correct instead of crashing the session.

Stream assembly lives in ``stream.py``. Live printers live in ``display.py``.
"""

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import memory, steer, tools
from .commands import RESERVED_SKILL_NAMES
from .config import STATE_ROOT
from . import status as status_mod
from .display import (
    _GeneratingIndicator,
    _ThinkingLive,
    _emit_assistant_content,
    _open_live_display,
)
from .stream import StreamError, _read_sse
from .ui import estimate_context_tokens

SKILLS_DIR = Path(__file__).parent / "skills"
USER_SKILLS_DIR = STATE_ROOT / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

API_CHAT_PATH = "/chat/completions"
API_MODELS_PATH = "/models"
NATIVE_MODELS_PATHS = ("/api/v0/models", "/api/v1/models")
CONTEXT_PRESSURE_RATIO = 0.80
CONTINUE_NUDGE_MAX_CHARS = 400


class ServerError(RuntimeError):
    pass


# ---------------------------------------------------------------- server

def _get_json(url: str, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(base_url: str, path: str, timeout: int = 5):
    return _get_json(base_url.rstrip("/") + path, timeout)


def list_models(base_url: str) -> "list[str]":
    try:
        data = _get(base_url, API_MODELS_PATH)
        return [m["id"] for m in data.get("data", [])]
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def _native_base(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        return url[:-3]
    return url


def _model_matches(entry: dict, model: str) -> bool:
    """Prefer exact id/key match; allow suffix match only for multi-segment ids."""
    ids = [entry.get("id", ""), entry.get("key", ""), entry.get("display_name", "")]
    ids = [i for i in ids if i]
    if model in ids:
        return True
    # Avoid bare endswith on short fragments (wrong-model context window).
    if "/" in model or len(model) >= 8:
        return any(model == i or i.endswith("/" + model) or model.endswith("/" + i)
                   for i in ids)
    return False


def get_context_limit(model: str, cfg: dict) -> int:
    """Return loaded context window size in tokens. 0 if unknown."""
    manual = int(cfg.get("context_length") or 0)
    if manual > 0:
        return manual

    base = _native_base(cfg["base_url"])
    for path in NATIVE_MODELS_PATHS:
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
        echo(status_mod.msg_starting_server())
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
            echo(status_mod.msg_loading_model(want))
            args = ["lms", "load", "--yes"] + ([want] if want else [])
            _run_lms(args, echo=echo, timeout=300)
            models = list_models(base)
    if not models:
        raise ServerError(status_mod.msg_no_models(base))
    want = cfg.get("model")
    if want and want in models:
        return want
    if want:
        echo(status_mod.msg_model_fallback(want, models[0]))
    return models[0]


# ---------------------------------------------------------------- skills

def _is_public_skill(stem: str) -> bool:
    """User-facing skills: skip system prompt and private _*.md authoring files."""
    return stem != "system" and not stem.startswith("_")


def skill_dirs() -> "list[Path]":
    """Search order for loading: user skills override packaged skills."""
    dirs = []
    if USER_SKILLS_DIR.is_dir():
        dirs.append(USER_SKILLS_DIR)
    dirs.append(SKILLS_DIR)
    return dirs


def skill_path(name: str) -> "Path | None":
    for d in skill_dirs():
        path = d / f"{name}.md"
        if path.is_file():
            return path
    return None


def list_skills() -> "list[str]":
    """Skill names from packaged + ~/.lmloop/skills/, excluding system/_author."""
    names = set()
    for d in skill_dirs():
        for p in d.glob("*.md"):
            if _is_public_skill(p.stem):
                names.add(p.stem)
    return sorted(names)


def _invalid_skill_name(name: str) -> bool:
    """Reject empty or path-like names before joining under skills dirs."""
    return (
        not name
        or "/" in name
        or "\\" in name
        or name.startswith(".")
        or ".." in name
    )


def load_skill(name: str, *, public_only: bool = False) -> str:
    """Load a skill body. When public_only, hide system/_*.md from callers."""
    if _invalid_skill_name(name) or (public_only and not _is_public_skill(name)):
        available = ", ".join(list_skills()) or "(none)"
        raise FileNotFoundError(f"No skill '{name}'. Available: {available}")
    path = skill_path(name)
    if path is None:
        available = ", ".join(list_skills()) or "(none)"
        raise FileNotFoundError(f"No skill '{name}'. Available: {available}")
    return path.read_text()


def skill_blurb(name: str) -> str:
    try:
        first = load_skill(name).strip().splitlines()[0]
        return first.lstrip("#").strip()[:80]
    except (FileNotFoundError, IndexError):
        return ""


def validate_skill_name(name: str) -> "str | None":
    """Return an error message if name is invalid, else None."""
    if not name or not SKILL_NAME_RE.match(name):
        return "skill name must be lowercase, start with a letter, and use only a-z 0-9 _ -"
    if name in RESERVED_SKILL_NAMES or name.startswith("_"):
        return f"skill name '{name}' is reserved"
    return None


def extract_skill_markdown(text: str) -> str:
    """Normalize model output into a skill markdown body."""
    body = (text or "").strip()
    if body.startswith("```"):
        lines = body.splitlines()
        # drop opening fence
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def save_user_skill(name: str, content: str) -> Path:
    """Write a confirmed skill into ~/.lmloop/skills/<name>.md."""
    err = validate_skill_name(name)
    if err:
        raise ValueError(err)
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_SKILLS_DIR / f"{name}.md"
    path.write_text(content if content.endswith("\n") else content + "\n")
    return path


def generate_skill_draft(cfg: dict, model: str, name: str, brief: str) -> str:
    """One-shot (no tools) generation of a new skill markdown body."""
    author = load_skill("_author")
    brief = brief.strip() or f"A reusable playbook named '{name}'."
    user = (
        f"Author a new lmloop skill named `{name}`.\n\n"
        f"User brief:\n{brief}\n\n"
        f"Existing skills (do not duplicate; complement them): "
        f"{', '.join(list_skills()) or '(none)'}\n"
    )
    messages = [
        {"role": "system", "content": author},
        {"role": "user", "content": user},
    ]
    msg, _usage = _chat(cfg, model, messages, tool_specs=None)
    return extract_skill_markdown(msg.get("content") or "")


def system_prompt(cfg: dict, workspace_root: "Path | None" = None) -> str:
    base = load_skill("system")
    names = tools.tool_names(cfg=cfg)
    tools_block = (
        "\n\n## Tools available\n\n"
        + ", ".join(f"`{n}`" for n in names)
        + ".\n"
    )
    # Inject after the first paragraph / before "## How to work" when present.
    marker = "\n## How to work"
    if marker in base:
        base = base.replace(marker, tools_block + marker, 1)
    else:
        base += tools_block
    base += "\n\n" + steer.clock_block()
    steering = steer.steering_block(workspace_root)
    if steering:
        base += "\n\n" + steering
    ctx = memory.context_block(cfg)
    if ctx:
        base += "\n\n## Context recovery (from project memory)\n\n" + ctx
    return base


# ---------------------------------------------------------------- chat call

def _chat_request(cfg: dict, model: str, messages: list, tool_specs: "list | None",
                  stream: bool) -> urllib.request.Request:
    payload = {
        "model": model,
        "messages": status_mod.api_messages(messages),
        "temperature": cfg.get("temperature", 0.7),
        "stream": stream,
    }
    if stream:
        # Ask the server for a final usage chunk (OpenAI-compatible; ignored if unsupported).
        payload["stream_options"] = {"include_usage": True}
    if tool_specs:
        payload["tools"] = tool_specs
    return urllib.request.Request(
        cfg["base_url"].rstrip("/") + API_CHAT_PATH,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"},
    )


def _raise_http_error(e: urllib.error.HTTPError, cfg: dict, body: "str | None" = None) -> None:
    if body is None:
        try:
            body = e.read().decode("utf-8", errors="replace")[:800]
        except OSError:
            body = ""
    lower = (body or "").lower()
    if e.code in (400, 413) and any(
        t in lower for t in ("context", "token", "length", "too long", "maximum")
    ):
        raise ServerError(status_mod.msg_context_overflow()) from e
    raise ServerError(f"LM Studio returned HTTP {e.code}: {(body or '')[:500]}") from e


def _chat_once(cfg: dict, model: str, messages: list, tool_specs: "list | None") -> "tuple[dict, dict]":
    """Non-streaming chat completion."""
    req = _chat_request(cfg, model, messages, tool_specs, stream=False)
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout_s", 600)) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        _raise_http_error(e, cfg)
    except OSError as e:
        raise ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e}")
    try:
        usage = data.get("usage") or {}
        return data["choices"][0]["message"], usage
    except (KeyError, IndexError):
        raise ServerError(f"Unexpected response shape: {json.dumps(data)[:500]}")


def _chat_stream(cfg: dict, model: str, messages: list, tool_specs: "list | None",
                 on_delta=None, on_activity=None, on_reasoning=None,
                 on_tools=None) -> "tuple[dict, dict]":
    """Streaming chat completion (SSE). Assembles message; optionally echoes content deltas."""
    req = _chat_request(cfg, model, messages, tool_specs, stream=True)

    def _open_and_read(request):
        with urllib.request.urlopen(request, timeout=cfg.get("timeout_s", 600)) as resp:
            return _read_sse(
                resp, on_delta=on_delta, on_activity=on_activity,
                on_reasoning=on_reasoning, on_tools=on_tools,
            )

    try:
        return _open_and_read(req)
    except StreamError as e:
        raise ServerError(str(e)) from e
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:800]
        except OSError:
            body = ""
        payload = json.loads(req.data.decode())
        # Older / stricter servers may reject stream_options — retry without it.
        if e.code == 400 and "stream_options" in payload:
            payload.pop("stream_options", None)
            retry = urllib.request.Request(
                req.full_url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            try:
                return _open_and_read(retry)
            except StreamError as e2:
                raise ServerError(str(e2)) from e2
            except urllib.error.HTTPError as e2:
                _raise_http_error(e2, cfg)
            except OSError as e2:
                raise ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e2}")
        _raise_http_error(e, cfg, body=body)
    except OSError as e:
        raise ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e}")


def _chat(cfg: dict, model: str, messages: list, tool_specs: "list | None",
          on_delta=None, on_activity=None, on_reasoning=None,
          on_tools=None) -> "tuple[dict, dict]":
    """Chat completion. Streams when cfg['stream'] is true (default)."""
    if cfg.get("stream", True):
        return _chat_stream(
            cfg, model, messages, tool_specs,
            on_delta=on_delta, on_activity=on_activity,
            on_reasoning=on_reasoning, on_tools=on_tools,
        )
    return _chat_once(cfg, model, messages, tool_specs)


def _accumulate_usage(stats: "dict | None", usage: dict) -> None:
    if not stats or not usage:
        return
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
    stats["total_tokens"] = stats.get("total_tokens", 0) + int(usage.get("total_tokens") or 0)
    stats["last_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    stats["last_completion_tokens"] = int(usage.get("completion_tokens") or 0)


# Soft-stop fallback: models lack a "meant to tool-call" signal, so we
# treat first-line / last-line prefixes as intent-only narration.
_INTENT_STARTS = (
    "let me",
    "i'll",
    "i will",
    "i'm going to",
    "i am going to",
    "i'm about to",
    "i am about to",
    "next,",
    "next:",
    "next ",
    "now i'll",
    "now i will",
    "now let me",
    "i need to",
    "looking at",
    "looking into",
    "diving",
    "exploring",
    "checking",
    "investigating",
)
_CODE_EXTS = frozenset({
    "py", "md", "go", "ts", "js", "tsx", "jsx", "rs", "java", "c", "h", "cpp",
})


def _line_has_intent(line: str) -> bool:
    s = (line or "").strip().lower()
    return any(s.startswith(p) for p in _INTENT_STARTS)


def _has_continue_intent(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if _line_has_intent(s):
        return True
    last_line = s.rsplit("\n", 1)[-1]
    if _line_has_intent(last_line):
        return True
    return _line_has_intent(s.rsplit(".", 1)[-1])


def _looks_grounded(text: str) -> bool:
    """True when the reply cites a path, URL, or inline code — not just a plan."""
    if not text:
        return False
    if "`" in text or "http://" in text or "https://" in text:
        return True
    for raw in text.replace(",", " ").replace("(", " ").replace(")", " ").split():
        token = raw.strip()
        if token.startswith("/") and "/" in token[1:]:
            return True
        if ":" not in token or "." not in token:
            continue
        path, _, rest = token.partition(":")
        if rest[:1].isdigit() and path.rsplit(".", 1)[-1].lower() in _CODE_EXTS:
            return True
    return False


def _should_nudge_continue(content: str, turn_tools: int, nudges: int,
                           max_nudges: int) -> bool:
    """True when the model soft-stopped mid-task with intent-only narration.

    An empty final message after tools is treated as done — many models end a
    successful tool turn with no trailing prose. Also nudges first-round
    intent-only narration (turn_tools == 0) — classic local-model hang.
    """
    if nudges >= max_nudges:
        return False
    text = (content or "").strip()
    if not text:
        return False
    if len(text) > CONTINUE_NUDGE_MAX_CHARS:
        return False
    if not _has_continue_intent(text):
        return False
    if _looks_grounded(text):
        return False
    # After tools, or first-round plan-only (no tools yet).
    return True


def _named_tool_calls(tool_calls: list) -> list:
    """Drop tool_calls with an empty function name (incomplete stream)."""
    out = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        name = (call.get("function") or {}).get("name") or ""
        if str(name).strip():
            out.append(call)
    return out


def _rollback_incomplete_messages(messages: list, start: int) -> None:
    """Pop trailing incomplete tool rounds / nudge prompts after abort.

    Keeps completed assistant+tool pairs and final text-only assistant replies.
    """
    while len(messages) > start:
        last = messages[-1]
        role = last.get("role")
        if role == "user" and status_mod.is_nudge_message(last):
            messages.pop()
            continue
        if role == "tool":
            i = len(messages) - 1
            while i >= start and messages[i].get("role") == "tool":
                i -= 1
            if i < start or messages[i].get("role") != "assistant":
                messages.pop()
                continue
            n_tools = len(messages) - 1 - i
            n_calls = len(messages[i].get("tool_calls") or [])
            if n_calls and n_tools < n_calls:
                del messages[i:]
                continue
            break
        if role == "assistant" and (last.get("tool_calls") or []):
            # Assistant requested tools but none (or not all) were recorded yet.
            messages.pop()
            continue
        break


# ---------------------------------------------------------------- act loop

def act(cfg: dict, model: str, messages: list, session_log: "Path | None" = None,
        confirm_gate=None, echo=print, echo_tool=None, echo_delta=None,
        echo_status=None, stats: "dict | None" = None, echo_round=None,
        on_thinking=None, context_limit: int = 0,
        context_reserve: int = 2048,
        workspace_root: "Path | None" = None,
        readonly: bool = False) -> list:
    """Run the multi-round tool loop. Mutates and returns `messages`.

    When streaming is enabled (config ``stream``, default True), the HTTP body
    is SSE and tokens are shown live:
    - ``echo_delta=None`` (default): rich.Live markdown when available, else plain
    - ``echo_delta=True``: live plain tokens
    - ``echo_delta=False``: spinner only; ``echo`` once when the round completes
    - callable: custom live writer
    Live modes do not also call ``echo`` (avoids plain+rich doubles).

    Optional callbacks:
    - ``echo_status(text)`` for lmloop status lines (defaults to ``echo``)
    - ``echo_round(round_idx, stats, messages)`` after each model round
    - ``on_thinking(text)`` when thinking is stored for Ctrl+O

    On interrupt or server error, display is cleaned up and any incomplete
    trailing tool round is rolled back; completed rounds in this turn are kept.
    """
    if echo_status is None:
        echo_status = echo
    if echo_tool is None:
        echo_tool = lambda name, preview, stats=None: echo(f"  ⚙ {name}({preview})")
    color = bool(cfg.get("color", True))
    tool_specs, impls = tools.build_tools(
        cfg, confirm_gate=confirm_gate, workspace_root=workspace_root,
        readonly=readonly,
    )
    use_stream = bool(cfg.get("stream", True))
    turn_rounds = 0
    turn_tools = 0
    nudge_count = 0
    max_nudges = max(0, int(cfg.get("max_continue_nudges", 2)))
    max_rounds = int(cfg.get("max_rounds", 60))
    checkpoint = len(messages)
    context_warned = False
    prev_session = memory.set_active_session(session_log)

    def _mark_interrupted() -> None:
        if stats is not None:
            stats["interrupted"] = True

    try:
        for round_idx in range(max_rounds):
            if context_limit and not context_warned:
                used = 0
                if stats is not None:
                    used = int(stats.get("last_prompt_tokens") or 0)
                if not used:
                    used = estimate_context_tokens(messages)
                effective = max(context_limit - max(context_reserve, 0), 1)
                ratio = min(used / effective, 1.0)
                if ratio >= CONTEXT_PRESSURE_RATIO:
                    echo_status(status_mod.msg_context_pressure(int(ratio * 100)))
                    context_warned = True

            printer, live_mode = _open_live_display(echo_delta, color=color)
            thinking = _ThinkingLive(color=color) if use_stream else None
            indicator = None
            if use_stream and live_mode in ("silent", "markdown"):
                indicator = _GeneratingIndicator()
            thinking_cleared = False
            printer_finished = False

            def _retire_thinking(*, keep: bool = False) -> str:
                nonlocal thinking_cleared
                if thinking_cleared or thinking is None:
                    return ""
                text = thinking.finish_keep() if keep else thinking.erase()
                thinking_cleared = True
                if text.strip() and on_thinking:
                    on_thinking(text)
                return text

            def on_delta(piece: str, _printer=printer, _ind=indicator, _mode=live_mode) -> None:
                _retire_thinking()
                if _ind is not None:
                    _ind.clear()
                was = _printer.visible
                _printer.feed(piece)
                if _ind is not None and _mode == "markdown" and _printer.visible and not was:
                    _ind.clear()

            def on_reasoning(piece: str, _think=thinking, _ind=indicator) -> None:
                if _think is None:
                    return
                if _ind is not None:
                    _ind.clear()
                _think.feed(piece)

            def on_tools(_ind=indicator) -> None:
                if _ind is not None:
                    _ind.clear()
                _retire_thinking()

            try:
                msg, usage = _chat(
                    cfg, model, messages, tool_specs,
                    on_delta=on_delta if use_stream else None,
                    on_activity=indicator.tick if indicator else None,
                    on_reasoning=on_reasoning if use_stream else None,
                    on_tools=on_tools if use_stream else None,
                )
                # Always retire the spinner before any post-stream prints.
                if indicator is not None:
                    indicator.clear()
                _accumulate_usage(stats, usage)
                turn_rounds += 1
                tool_calls = _named_tool_calls(msg.get("tool_calls") or [])
                content = (msg.get("content") or "").strip()
                reasoning_text = (msg.get("_reasoning") or "").strip()
                # Promote reasoning when it was the only usable reply (no
                # content and no named tools — including empty-name stubs).
                if not content and not tool_calls and reasoning_text:
                    content = reasoning_text

                if thinking is not None:
                    content, retired = thinking.retire_for_round(
                        tool_calls, content, reasoning_text,
                    )
                    if retired.strip() and on_thinking:
                        on_thinking(retired)

                store_content = content if content else ""
                assistant_entry = {"role": "assistant", "content": store_content}
                if tool_calls:
                    assistant_entry["tool_calls"] = tool_calls
                messages.append(assistant_entry)

                # Finish Live/stream display before any other stdout writes
                # (round breadcrumb, tool lines). Printing while rich.Live is
                # active bypasses its cursor control and corrupts the frame.
                if content:
                    _emit_assistant_content(
                        echo, content, printer if use_stream else None, live_mode,
                    )
                    printer_finished = True
                    if session_log:
                        memory.log_event(session_log, "assistant", content)
                elif use_stream:
                    printer.finish()
                    printer_finished = True

                if echo_round is not None:
                    echo_round(round_idx + 1, stats, messages, context_limit, context_reserve)

                if not tool_calls:
                    soft_stop = _should_nudge_continue(
                        content, turn_tools, nudge_count, max_nudges,
                    )
                    # Only inject a nudge when another model round can still run.
                    if soft_stop and round_idx + 1 < max_rounds:
                        nudge_count += 1
                        echo_status(status_mod.msg_paused_continuing())
                        messages.append(status_mod.nudge_message())
                        continue
                    # Nudges exhausted, or soft-stop on the final allowed round.
                    if soft_stop or (
                        nudge_count >= max_nudges
                        and content
                        and _has_continue_intent(content)
                        and not _looks_grounded(content)
                    ):
                        echo_status(status_mod.msg_stopped_unfinished())
                    if stats is not None:
                        stats["turns"] = stats.get("turns", 0) + 1
                        stats["rounds"] = stats.get("rounds", 0) + turn_rounds
                        stats["tool_calls"] = stats.get("tool_calls", 0) + turn_tools
                        stats["interrupted"] = False
                    return messages

                for call_idx, call in enumerate(tool_calls):
                    turn_tools += 1
                    fn = call.get("function", {})
                    name, args = fn.get("name", ""), fn.get("arguments", "")
                    arg_preview = tools.format_tool_preview(
                        name, args, workspace_root=workspace_root,
                    )
                    echo_tool(name, arg_preview, stats)
                    result = tools.dispatch(impls, name, args)
                    if session_log:
                        memory.log_event(
                            session_log, "tool",
                            f"{name}({arg_preview}) -> {result[:500]}",
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{round_idx}_{call_idx}",
                        "content": result,
                    })
            finally:
                if indicator is not None:
                    indicator.clear()
                if thinking is not None and thinking.active:
                    thinking.erase()
                if not printer_finished and printer is not None:
                    printer.finish()

        if stats is not None:
            stats["turns"] = stats.get("turns", 0) + 1
            stats["rounds"] = stats.get("rounds", 0) + turn_rounds
            stats["tool_calls"] = stats.get("tool_calls", 0) + turn_tools
            stats["interrupted"] = False
        echo_status(status_mod.msg_hit_max_rounds())
        return messages
    except (KeyboardInterrupt, Exception):
        _mark_interrupted()
        _rollback_incomplete_messages(messages, checkpoint)
        raise
    finally:
        memory.set_active_session(prev_session)
