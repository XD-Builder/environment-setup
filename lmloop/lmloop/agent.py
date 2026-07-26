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
USER_SKILLS_DIR = STATE_ROOT / "skills"
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
# Names that collide with CLI/REPL commands (not creatable as user skills).
# Packaged skill names like compact/retro stay allowed so users can override them.
RESERVED_SKILL_NAMES = frozenset({
    "system", "new", "names", "skill", "skills", "help", "quit", "exit", "q",
    "transcript", "stats", "model", "memory", "save", "undo", "history",
    "checkpoints", "restore", "decisions", "context", "continue",
})

# Stream assembly: some servers send cumulative snapshots instead of true deltas.
_REPEAT_WINDOW = 400
_REPEAT_TIMES = 3

# Cached optional rich imports: (Console, Live, Markdown) or False if unavailable.
_RICH = None


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

def _merge_tool_call_delta(acc: dict, deltas) -> None:
    """Accumulate streamed tool_call deltas keyed by index (OpenAI SSE shape).

    Skips malformed entries so one bad SSE chunk cannot kill the turn.
    """
    if not isinstance(deltas, list):
        return
    for tc in deltas:
        if not isinstance(tc, dict):
            continue
        try:
            idx = int(tc.get("index") or 0)
        except (TypeError, ValueError):
            continue
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
        if not isinstance(fn, dict):
            continue
        if fn.get("name"):
            entry["function"]["name"] += str(fn["name"])
        if "arguments" in fn and fn["arguments"] is not None:
            entry["function"]["arguments"] += str(fn["arguments"])


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


def _ingest_text_delta(parts: list, piece: str) -> str:
    """Append a stream text piece; normalize cumulative snapshots to a true suffix.

    Returns the incremental text that was newly added (may be empty).
    Cumulative servers re-send the full text so far; only the new suffix is kept.
    An exact replay (piece == so_far) is ignored as no-new-tokens.
    A near-complete shorter prefix is treated as a stale cumulative snapshot;
    small chunks that merely happen to be prefixes (common with incremental
    streams) are appended normally.
    """
    if not piece:
        return ""
    so_far = "".join(parts)
    if so_far and piece.startswith(so_far):
        suffix = piece[len(so_far):]
        if suffix:
            parts.append(suffix)
        return suffix
    # Stale cumulative snapshot: long prefix of so_far, not a small delta.
    if (
        so_far
        and len(piece) < len(so_far)
        and so_far.startswith(piece)
        and len(piece) >= max(64, len(so_far) * 4 // 5)
    ):
        return ""
    parts.append(piece)
    return piece


def _tail_repeats(text: str, window: int = _REPEAT_WINDOW, times: int = _REPEAT_TIMES) -> bool:
    """True when the last `window` chars repeat `times` times consecutively."""
    need = window * times
    if len(text) < need:
        return False
    tail = text[-need:]
    unit = tail[:window]
    return unit * times == tail


def _read_sse(resp, on_delta=None, on_activity=None, on_reasoning=None,
              on_tools=None) -> "tuple[dict, dict]":
    """Parse an OpenAI-style SSE body into (assistant message, usage)."""
    content_parts: list = []
    reasoning_parts: list = []
    tool_acc: dict = {}
    usage: dict = {}
    saw_data = False
    content_halted = False
    tools_started = False
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
        if piece and not content_halted:
            incr = _ingest_text_delta(content_parts, piece)
            if incr:
                if on_delta:
                    on_delta(incr)
                if _tail_repeats("".join(content_parts)):
                    content_halted = True
        # Qwen / some LM Studio builds stream chain-of-thought separately.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            incr_r = _ingest_text_delta(reasoning_parts, reasoning)
            if incr_r and on_reasoning:
                on_reasoning(incr_r)
        tc_deltas = delta.get("tool_calls")
        if tc_deltas:
            try:
                if not tools_started:
                    tools_started = True
                    if on_tools:
                        on_tools()
                _merge_tool_call_delta(tool_acc, tc_deltas)
            except Exception:
                # Never let a hostile/malformed tool chunk abort the stream.
                continue

    if not saw_data:
        raise ServerError("Empty stream from LM Studio (no SSE data)")

    content = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    has_named_tools = any(
        ((tool_acc[i].get("function") or {}).get("name") or "").strip()
        for i in tool_acc
    )
    if not content.strip() and not has_named_tools and reasoning_text:
        # Fallback when the server only streamed reasoning (or empty-name
        # tool stubs) and no final content.
        content = reasoning_text
    msg: dict = {"role": "assistant", "content": content}
    if tool_acc:
        msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    if reasoning_text:
        msg["_reasoning"] = reasoning_text
    return msg, usage


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


def _default_echo_delta(piece: str) -> None:
    sys.stdout.write(piece)
    sys.stdout.flush()


class _GeneratingIndicator:
    """TTY spinner while waiting for the first streamed token.

    Once cleared (content/reasoning/tools arrived), further ``tick`` calls are
    no-ops — later SSE chunks (usage, trailing tool deltas) must not resurrect
    the spinner onto the round/tool lines.
    """

    _FRAMES = "⠋⠙⠹⠸⠴⠦⠧⠇⠏"

    def __init__(self):
        self._n = 0
        self._shown = False
        self._stopped = False

    def tick(self) -> None:
        if self._stopped or not sys.stdout.isatty():
            return
        frame = self._FRAMES[self._n % len(self._FRAMES)]
        self._n += 1
        sys.stdout.write(f"\r  {frame} generating…")
        sys.stdout.flush()
        self._shown = True

    def clear(self) -> None:
        self._stopped = True
        if not self._shown:
            return
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        self._shown = False


def _load_rich():
    """Load rich Console/Live/Markdown once. Returns the tuple or False."""
    global _RICH
    if _RICH is not None:
        return _RICH
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.markdown import Markdown
        _RICH = (Console, Live, Markdown)
    except ImportError:
        _RICH = False
    return _RICH


def _rich_live_available() -> bool:
    if not sys.stdout.isatty():
        return False
    return _load_rich() is not False


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

    def reset(self) -> None:
        self._leading = ""
        self._trailing_nl = ""
        self._started = False

    def finish(self) -> bool:
        self._leading = ""
        if not self._started:
            self._trailing_nl = ""
            self.reset()
            return False
        self._out("\n")
        self._trailing_nl = ""
        started = self._started
        self.reset()
        return started


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
        rich = _load_rich()
        if not rich:
            return
        Console, Live, Markdown = rich
        console = Console(
            highlight=False,
            soft_wrap=True,
            color_system="auto" if self._color else None,
            force_terminal=True,
        )
        # ellipsis (not visible): Live can only reliably redraw within the
        # viewport. vertical_overflow="visible" leaves prior frames in
        # scrollback as the markdown grows, which looks like repeated lines.
        # Live.stop() switches to visible for a single final full render.
        self._live = Live(
            Markdown(""),
            console=console,
            refresh_per_second=12,
            vertical_overflow="ellipsis",
        )
        self._live.start()

    def _refresh(self) -> None:
        if self._live is None:
            return
        rich = _load_rich()
        if not rich:
            return
        _Console, _Live, Markdown = rich
        self._live.update(Markdown("".join(self._parts)))

    def reset(self) -> None:
        self._parts = []
        self._leading = ""
        self._started = False
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def finish(self) -> bool:
        self._leading = ""
        if not self._started:
            self.reset()
            return False
        if self._live is not None:
            self._refresh()
            self._live.stop()
            self._live = None
        started = self._started
        self._parts = []
        self._started = False
        return started


class _ThinkingLive:
    """Dim gray thinking stream that can be erased from the TTY when done."""

    def __init__(self, color: bool = True):
        use = color and sys.stdout.isatty()
        self._dim = "\033[2m" if use else ""
        self._reset = "\033[0m" if use else ""
        self._parts: list = []
        self._line_buf = ""
        self._lines_shown = 0
        self._active = False
        self._erased = False

    @property
    def active(self) -> bool:
        return self._active and not self._erased

    def text(self) -> str:
        return "".join(self._parts)

    def feed(self, piece: str) -> None:
        if not piece or self._erased:
            return
        self._active = True
        self._parts.append(piece)
        self._line_buf += piece
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._write_line(line)

    def _write_line(self, line: str) -> None:
        sys.stdout.write(f"{self._dim}  ▎{line}{self._reset}\n")
        sys.stdout.flush()
        self._lines_shown += 1

    def _flush_partial(self) -> None:
        if self._line_buf:
            self._write_line(self._line_buf.rstrip("\r\n"))
            self._line_buf = ""

    def erase(self) -> str:
        """Remove thinking lines from the TTY; return full text for Ctrl+O."""
        if not self._active and not self._parts:
            return ""
        self._flush_partial()
        text = self.text()
        if self._lines_shown and sys.stdout.isatty():
            for _ in range(self._lines_shown):
                sys.stdout.write("\033[1A\033[2K")
            sys.stdout.flush()
        self._lines_shown = 0
        self._active = False
        self._erased = True
        return text

    def finish_keep(self) -> str:
        """Flush remaining line without erasing (reasoning-only final reply)."""
        self._flush_partial()
        self._active = False
        return self.text()


def _emit_assistant_content(echo, content: str, printer, live_mode: str) -> None:
    """Finish live display; echo only when nothing was streamed on-screen."""
    streamed = False
    if printer is not None:
        streamed = bool(printer.finish())
    if not content:
        return
    # Live modes already painted tokens; skip only when something was shown.
    # Reasoning→content fallback never hits the printer — must echo.
    if live_mode in ("plain", "markdown") and streamed:
        return
    echo(content)


# Narration that signals more work without actually calling tools (common on
# small local models). Keep tight: short, intent-only lines after tool use.
_CONTINUE_INTENT_RE = re.compile(
    r"(?is)^\s*(?:"
    r"let me\b|i(?:'ll| will)\b|i(?:'m| am) (?:going to|about to)\b|"
    r"next[,:]?\s|now (?:i(?:'ll| will)|let me)\b|"
    r"(?:i need to|looking (?:at|into)|diving|exploring|checking|investigating)\b"
    r").{0,400}$"
)

_NUDGE_CONTINUE = (
    "[lmloop] You described a next step but did not call any tools and did not "
    "finish the user's request. Continue now: either call the tools you need, "
    "or give the final answer."
)


def _should_nudge_continue(content: str, turn_tools: int, nudges: int,
                           max_nudges: int) -> bool:
    """True when the model soft-stopped mid-task with intent-only narration.

    An empty final message after tools is treated as done — many models end a
    successful tool turn with no trailing prose.
    """
    if nudges >= max_nudges or turn_tools <= 0:
        return False
    text = (content or "").strip()
    if not text:
        return False
    return len(text) <= 400 and bool(_CONTINUE_INTENT_RE.search(text))


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


def _call_echo_tool(echo_tool, name: str, preview: str, stats: "dict | None") -> None:
    try:
        echo_tool(name, preview, stats)
    except TypeError:
        echo_tool(name, preview)


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
        if role == "user" and (last.get("content") or "") == _NUDGE_CONTINUE:
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


def _safe_clear_indicator(indicator) -> None:
    if indicator is None:
        return
    try:
        indicator.clear()
    except Exception:
        pass


def _safe_finish_printer(printer) -> None:
    if printer is None:
        return
    try:
        printer.finish()
    except Exception:
        try:
            printer.reset()
        except Exception:
            pass


def _safe_close_thinking(thinking, *, keep: bool = False) -> None:
    if thinking is None:
        return
    try:
        if keep and thinking.active:
            thinking.finish_keep()
        elif thinking.active:
            thinking.erase()
    except Exception:
        pass


# ---------------------------------------------------------------- act loop

def act(cfg: dict, model: str, messages: list, session_log: "Path | None" = None,
        confirm_gate=None, echo=print, echo_tool=None, echo_delta=None,
        stats: "dict | None" = None, echo_round=None, on_collapse=None,
        context_limit: int = 0, context_reserve: int = 2048) -> list:
    """Run the multi-round tool loop. Mutates and returns `messages`.

    When streaming is enabled (config ``stream``, default True), the HTTP body
    is SSE and tokens are shown live:
    - ``echo_delta=None`` (default): rich.Live markdown when available, else plain
    - ``echo_delta=True``: live plain tokens
    - ``echo_delta=False``: spinner only; ``echo`` once when the round completes
    - callable: custom live writer
    Live modes do not also call ``echo`` (avoids plain+rich doubles).

    Optional callbacks:
    - ``echo_round(round_idx, stats, messages)`` after each model round
    - ``on_collapse(kind, text)`` when thinking/tool detail is stored for Ctrl+O
      (kind is ``\"thinking\"`` or ``\"tool\"``)

    On interrupt or server error, display is cleaned up and any incomplete
    trailing tool round is rolled back; completed rounds in this turn are kept.
    """
    if echo_tool is None:
        echo_tool = lambda name, preview, *_a: echo(f"  ⚙ {name}({preview})")
    color = bool(cfg.get("color", True))
    tool_specs, impls = tools.build_tools(cfg, confirm_gate=confirm_gate)
    use_stream = bool(cfg.get("stream", True))
    turn_rounds = 0
    turn_tools = 0
    nudge_count = 0
    max_nudges = max(0, int(cfg.get("max_continue_nudges", 2)))
    max_rounds = int(cfg.get("max_rounds", 25))
    checkpoint = len(messages)

    def _mark_interrupted() -> None:
        if stats is not None:
            stats["interrupted"] = True

    try:
        for round_idx in range(max_rounds):
            printer, live_mode = _open_live_display(echo_delta, color=color)
            thinking = _ThinkingLive(color=color) if use_stream else None
            indicator = None
            if use_stream and live_mode in ("silent", "markdown"):
                indicator = _GeneratingIndicator()
            thinking_cleared = False
            keep_thinking = False
            printer_finished = False

            def _clear_thinking_for_output():
                nonlocal thinking_cleared
                if thinking_cleared or thinking is None:
                    return
                text = thinking.erase()
                thinking_cleared = True
                if text.strip() and on_collapse:
                    on_collapse("thinking", text)

            def on_delta(piece: str, _printer=printer, _ind=indicator, _mode=live_mode) -> None:
                _clear_thinking_for_output()
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
                _clear_thinking_for_output()

            try:
                msg, usage = _chat(
                    cfg, model, messages, tool_specs,
                    on_delta=on_delta if use_stream else None,
                    on_activity=indicator.tick if indicator else None,
                    on_reasoning=on_reasoning if use_stream else None,
                    on_tools=on_tools if use_stream else None,
                )
                # Always retire the spinner before any post-stream prints.
                _safe_clear_indicator(indicator)
                _accumulate_usage(stats, usage)
                turn_rounds += 1
                tool_calls = _named_tool_calls(msg.get("tool_calls") or [])
                content = (msg.get("content") or "").strip()
                reasoning_text = (msg.get("_reasoning") or "").strip()
                # Promote reasoning when it was the only usable reply (no
                # content and no named tools — including empty-name stubs).
                if not content and not tool_calls and reasoning_text:
                    content = reasoning_text

                if tool_calls:
                    _clear_thinking_for_output()
                elif (
                    thinking is not None and thinking.active
                    and not tool_calls
                    and content
                    and content == reasoning_text
                ):
                    # Reasoning-only final already shown in gray — keep it.
                    keep_thinking = True
                    thinking.finish_keep()
                    thinking_cleared = True
                    if on_collapse:
                        on_collapse("thinking", content)
                elif content and thinking is not None and thinking.active:
                    _clear_thinking_for_output()
                elif thinking is not None and thinking.active and not content and not tool_calls:
                    keep_thinking = True
                    kept = thinking.finish_keep()
                    thinking_cleared = True
                    if kept.strip():
                        content = kept.strip()
                        if on_collapse:
                            on_collapse("thinking", kept)

                store_content = content if content else ""
                assistant_entry = {"role": "assistant", "content": store_content}
                if tool_calls:
                    assistant_entry["tool_calls"] = tool_calls
                messages.append(assistant_entry)

                # Finish Live/stream display before any other stdout writes
                # (round breadcrumb, tool lines). Printing while rich.Live is
                # active bypasses its cursor control and corrupts the frame.
                # Skip re-echo when reasoning is already kept on screen.
                if content and not keep_thinking:
                    _emit_assistant_content(
                        echo, content, printer if use_stream else None, live_mode,
                    )
                    printer_finished = True
                    if session_log:
                        memory.log_event(session_log, "assistant", content)
                elif use_stream:
                    printer.finish()
                    printer_finished = True
                    if content and keep_thinking and session_log:
                        memory.log_event(session_log, "assistant", content)

                if echo_round is not None:
                    echo_round(round_idx + 1, stats, messages, context_limit, context_reserve)

                if not tool_calls:
                    soft_stop = _should_nudge_continue(
                        content, turn_tools, nudge_count, max_nudges,
                    )
                    # Only inject a nudge when another model round can still run.
                    if soft_stop and round_idx + 1 < max_rounds:
                        nudge_count += 1
                        echo("[lmloop] model paused mid-task — continuing…")
                        messages.append({"role": "user", "content": _NUDGE_CONTINUE})
                        continue
                    # Nudges exhausted, or soft-stop on the final allowed round.
                    if soft_stop or (
                        turn_tools > 0
                        and nudge_count >= max_nudges
                        and content
                        and _CONTINUE_INTENT_RE.search(content)
                    ):
                        echo(
                            "[lmloop] model stopped without finishing — "
                            "type /continue to resume"
                        )
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
                    arg_preview = args if len(args) <= 160 else args[:160] + "..."
                    _call_echo_tool(echo_tool, name, arg_preview, stats)
                    result = tools.dispatch(impls, name, args)
                    detail = f"{name}({arg_preview})\n\n{result}"
                    if on_collapse:
                        on_collapse("tool", detail)
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
                _safe_clear_indicator(indicator)
                if not keep_thinking:
                    _safe_close_thinking(thinking, keep=False)
                if not printer_finished:
                    _safe_finish_printer(printer)

        if stats is not None:
            stats["turns"] = stats.get("turns", 0) + 1
            stats["rounds"] = stats.get("rounds", 0) + turn_rounds
            stats["tool_calls"] = stats.get("tool_calls", 0) + turn_tools
            stats["interrupted"] = False
        echo("[lmloop] hit max_rounds — stopping. Say 'continue' to keep going.")
        return messages
    except KeyboardInterrupt:
        _mark_interrupted()
        _rollback_incomplete_messages(messages, checkpoint)
        raise
    except ServerError:
        _mark_interrupted()
        _rollback_incomplete_messages(messages, checkpoint)
        raise
    except Exception as e:
        _mark_interrupted()
        _rollback_incomplete_messages(messages, checkpoint)
        raise ServerError(f"agent loop failed: {type(e).__name__}: {e}") from e
