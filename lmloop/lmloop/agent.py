"""The agent loop: OpenAI-compatible multi-round tool calling against LM Studio.

No SDK dependency — plain urllib against /v1/chat/completions with `tools`.
The loop mirrors LM Studio's .act() semantics: model responds, requested tools
run locally, results feed back, repeat until the model stops calling tools or
max_rounds is hit. Tool errors are reported back to the model as text so it
can self-correct instead of crashing the session.
"""

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import memory, tools
from .config import STATE_ROOT

SKILLS_DIR = Path(__file__).parent / "skills"
PROMPTS_DIR = SKILLS_DIR  # back-compat alias
USER_SKILLS_DIR = STATE_ROOT / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Names that collide with CLI/REPL commands (not creatable as user skills).
# Packaged skill names like compact/retro stay allowed so users can override them.
RESERVED_SKILL_NAMES = frozenset({
    "system", "new", "names", "skill", "skills", "help", "quit", "exit", "q",
    "transcript", "stats", "model", "memory", "save", "undo", "history",
    "checkpoints", "restore", "decisions", "context", "continue",
})


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
    if not body.startswith("#"):
        # keep as-is; caller may still save after user review
        pass
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


def load_prompt(name: str) -> str:
    """Alias for load_skill (older call sites)."""
    return load_skill(name)


def system_prompt(cfg: dict) -> str:
    base = load_skill("system")
    ctx = memory.context_block(cfg)
    if ctx:
        base += "\n\n## Context recovery (from project memory)\n\n" + ctx
    return base


# ---------------------------------------------------------------- chat call

def _merge_tool_call_delta(acc: dict, deltas: list) -> None:
    """Accumulate streamed tool_call deltas keyed by index (OpenAI SSE shape)."""
    for tc in deltas:
        idx = int(tc.get("index") or 0)
        if idx not in acc:
            acc[idx] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        entry = acc[idx]
        if tc.get("id"):
            entry["id"] = tc["id"]
        if tc.get("type"):
            entry["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            entry["function"]["name"] += fn["name"]
        if "arguments" in fn and fn["arguments"] is not None:
            entry["function"]["arguments"] += fn["arguments"]


def _chat_request(cfg: dict, model: str, messages: list, tool_specs: "list | None",
                  stream: bool) -> urllib.request.Request:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": cfg.get("temperature", 0.7),
        "stream": stream,
    }
    if stream:
        # Ask the server for a final usage chunk (OpenAI-compatible; ignored if unsupported).
        payload["stream_options"] = {"include_usage": True}
    if tool_specs:
        payload["tools"] = tool_specs
    return urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"},
    )


def _chat_once(cfg: dict, model: str, messages: list, tool_specs: "list | None") -> "tuple[dict, dict]":
    """Non-streaming chat completion."""
    req = _chat_request(cfg, model, messages, tool_specs, stream=False)
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


def _read_sse(resp, on_delta=None, on_activity=None) -> "tuple[dict, dict]":
    """Parse an OpenAI-style SSE body into (assistant message, usage)."""
    content_parts: list = []
    reasoning_parts: list = []
    tool_acc: dict = {}
    usage: dict = {}
    saw_data = False
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        saw_data = True
        if on_activity:
            on_activity()
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            content_parts.append(piece)
            if on_delta:
                on_delta(piece)
        # Qwen / some LM Studio builds stream chain-of-thought separately.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            reasoning_parts.append(reasoning)
        tc_deltas = delta.get("tool_calls")
        if tc_deltas:
            _merge_tool_call_delta(tool_acc, tc_deltas)

    if not saw_data:
        raise ServerError("Empty stream from LM Studio (no SSE data)")

    content = "".join(content_parts)
    if not content.strip() and not tool_acc and reasoning_parts:
        # Fallback when the server only streamed reasoning and no final content.
        content = "".join(reasoning_parts)
    msg: dict = {"role": "assistant", "content": content}
    if tool_acc:
        msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    return msg, usage


def _chat_stream(cfg: dict, model: str, messages: list, tool_specs: "list | None",
                 on_delta=None, on_activity=None) -> "tuple[dict, dict]":
    """Streaming chat completion (SSE). Assembles message; optionally echoes content deltas."""
    req = _chat_request(cfg, model, messages, tool_specs, stream=True)

    def _open_and_read(request):
        with urllib.request.urlopen(request, timeout=cfg.get("timeout_s", 600)) as resp:
            return _read_sse(resp, on_delta=on_delta, on_activity=on_activity)

    try:
        return _open_and_read(req)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
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
            except urllib.error.HTTPError as e2:
                raise ServerError(f"LM Studio returned HTTP {e2.code}: {e2.read().decode()[:500]}")
            except OSError as e2:
                raise ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e2}")
        raise ServerError(f"LM Studio returned HTTP {e.code}: {body}")
    except OSError as e:
        raise ServerError(f"Cannot reach LM Studio at {cfg['base_url']}: {e}")


def _chat(cfg: dict, model: str, messages: list, tool_specs: "list | None",
          on_delta=None, on_activity=None) -> "tuple[dict, dict]":
    """Chat completion. Streams when cfg['stream'] is true (default)."""
    if cfg.get("stream", True):
        return _chat_stream(
            cfg, model, messages, tool_specs,
            on_delta=on_delta, on_activity=on_activity,
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


def _default_echo_delta(piece: str) -> None:
    sys.stdout.write(piece)
    sys.stdout.flush()


class _GeneratingIndicator:
    """TTY spinner while waiting for the first streamed token."""

    _FRAMES = "⠋⠙⠹⠸⠴⠦⠧⠇⠏"

    def __init__(self):
        self._n = 0
        self._shown = False

    def tick(self) -> None:
        if not sys.stdout.isatty():
            return
        frame = self._FRAMES[self._n % len(self._FRAMES)]
        self._n += 1
        sys.stdout.write(f"\r  {frame} generating…")
        sys.stdout.flush()
        self._shown = True

    def clear(self) -> None:
        if not self._shown:
            return
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        self._shown = False


def _rich_live_available() -> bool:
    if not sys.stdout.isatty():
        return False
    try:
        import rich.live  # noqa: F401
        import rich.markdown  # noqa: F401
        return True
    except ImportError:
        return False


class _StreamPrinter:
    """Live plain-token writer with blank-line suppression (or silent tracker)."""

    def __init__(self, write=None):
        self._write = write
        self._leading = ""
        self._trailing_nl = ""
        self._started = False

    @property
    def visible(self) -> bool:
        return self._started

    def feed(self, piece: str) -> None:
        if not piece:
            return
        if not self._started:
            self._leading += piece
            if not self._leading.strip():
                return
            text = self._leading.lstrip("\r\n")
            self._leading = ""
            self._started = True
            if text:
                self._emit(text)
            return
        self._emit(piece)

    def _out(self, text: str) -> None:
        if text and self._write is not None:
            self._write(text)

    def _emit(self, text: str) -> None:
        i = len(text)
        while i > 0 and text[i - 1] in "\r\n":
            i -= 1
        body, nl = text[:i], text[i:]
        if body:
            if self._trailing_nl:
                self._out("\n")
                self._trailing_nl = ""
            self._out(body)
        if nl:
            self._trailing_nl = "\n"

    def finish(self) -> bool:
        self._leading = ""
        if not self._started:
            self._trailing_nl = ""
            return False
        self._out("\n")
        self._trailing_nl = ""
        return True


class _MarkdownLivePrinter:
    """Stream tokens into a rich.Live Markdown view (live + prettied)."""

    def __init__(self, color: bool = True):
        self._parts: list = []
        self._leading = ""
        self._started = False
        self._live = None
        self._color = color

    @property
    def visible(self) -> bool:
        return self._started

    def feed(self, piece: str) -> None:
        if not piece:
            return
        if not self._started:
            self._leading += piece
            if not self._leading.strip():
                return
            text = self._leading.lstrip("\r\n")
            self._leading = ""
            self._started = True
            self._ensure_live()
            if text:
                self._parts.append(text)
                self._refresh()
            return
        self._parts.append(piece)
        self._refresh()

    def _ensure_live(self) -> None:
        if self._live is not None:
            return
        from rich.console import Console
        from rich.live import Live
        from rich.markdown import Markdown

        console = Console(
            highlight=False,
            soft_wrap=True,
            color_system="auto" if self._color else None,
            force_terminal=True,
        )
        self._live = Live(
            Markdown(""),
            console=console,
            refresh_per_second=12,
            vertical_overflow="visible",
        )
        self._live.start()

    def _refresh(self) -> None:
        if self._live is None:
            return
        from rich.markdown import Markdown
        self._live.update(Markdown("".join(self._parts)))

    def finish(self) -> bool:
        self._leading = ""
        if not self._started:
            return False
        if self._live is not None:
            self._refresh()
            self._live.stop()
            self._live = None
        return True


def _emit_assistant_content(echo, content: str, printer, live_mode: str) -> None:
    """Finish live display; echo only when nothing was streamed on-screen."""
    if printer is not None:
        printer.finish()
    if not content or live_mode in ("plain", "markdown"):
        return
    echo(content)


def _open_live_display(echo_delta, color: bool = True):
    """Return (printer, mode) where mode is silent|plain|markdown."""
    if echo_delta is False:
        return _StreamPrinter(None), "silent"
    if echo_delta is True:
        return _StreamPrinter(_default_echo_delta), "plain"
    if callable(echo_delta):
        return _StreamPrinter(echo_delta), "plain"
    # None = auto: stream markdown live when possible, else plain tokens.
    if _rich_live_available():
        return _MarkdownLivePrinter(color=color), "markdown"
    return _StreamPrinter(_default_echo_delta), "plain"


def _resolve_echo_delta(echo_delta):
    """Back-compat helper for tests: False/None silent writer mapping.

    Prefer ``_open_live_display`` for real display selection.
    """
    if echo_delta is None or echo_delta is False:
        return None
    if echo_delta is True:
        return _default_echo_delta
    return echo_delta


# ---------------------------------------------------------------- act loop

def act(cfg: dict, model: str, messages: list, session_log: "Path | None" = None,
        confirm_gate=None, echo=print, echo_tool=None, echo_delta=None,
        stats: "dict | None" = None) -> list:
    """Run the multi-round tool loop. Mutates and returns `messages`.

    When streaming is enabled (config ``stream``, default True), the HTTP body
    is SSE and tokens are shown live:
    - ``echo_delta=None`` (default): rich.Live markdown when available, else plain
    - ``echo_delta=True``: live plain tokens
    - ``echo_delta=False``: spinner only; ``echo`` once when the round completes
    - callable: custom live writer
    Live modes do not also call ``echo`` (avoids plain+rich doubles).
    """
    if echo_tool is None:
        echo_tool = lambda name, preview: echo(f"  ⚙ {name}({preview})")
    color = bool(cfg.get("color", True))
    tool_specs, impls = tools.build_tools(cfg, confirm_gate=confirm_gate)
    use_stream = bool(cfg.get("stream", True))
    turn_rounds = 0
    turn_tools = 0
    for round_idx in range(cfg.get("max_rounds", 25)):
        printer, live_mode = _open_live_display(echo_delta, color=color)
        # Spinner for silent rounds; for markdown, until the first visible token.
        indicator = None
        if use_stream and live_mode in ("silent", "markdown"):
            indicator = _GeneratingIndicator()

        def on_delta(piece: str, _printer=printer, _ind=indicator, _mode=live_mode) -> None:
            was = _printer.visible
            _printer.feed(piece)
            if _ind is not None and _mode == "markdown" and _printer.visible and not was:
                _ind.clear()

        msg, usage = _chat(
            cfg, model, messages, tool_specs,
            on_delta=on_delta if use_stream else None,
            on_activity=indicator.tick if indicator else None,
        )
        if indicator:
            indicator.clear()
        _accumulate_usage(stats, usage)
        turn_rounds += 1
        tool_calls = msg.get("tool_calls") or []
        raw_content = msg.get("content") or ""
        content = raw_content.strip()

        assistant_entry = {"role": "assistant", "content": raw_content}
        if tool_calls:
            assistant_entry["tool_calls"] = tool_calls
        messages.append(assistant_entry)

        if content:
            _emit_assistant_content(
                echo, content, printer if use_stream else None, live_mode,
            )
            if session_log:
                memory.log_event(session_log, "assistant", content)
        elif use_stream:
            printer.finish()

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
