"""Live terminal display: spinner, token printers, thinking lines.

Close methods (clear / finish / erase / reset) must not raise — act() calls
them from ``finally`` during interrupt. Catch OSError at the write site.
"""

import shutil
import sys

from .stream import _think_line_similar

# Live thinking prints as it streams; erased when tools/answer start.
# Ctrl+O opens prior-turn thinking (chronological), not a newest-first cycle.
THINK_LINE_PREFIX = "  ▎"

# Cached optional rich imports: (Console, Live, Markdown) or False if unavailable.
_RICH = None


def _visual_rows(text: str, cols: int) -> int:
    """Terminal rows taken by ``text`` when the TTY wraps at ``cols``."""
    if cols <= 0:
        return 1
    n = len(text)
    if n <= 0:
        return 1
    return (n + cols - 1) // cols


def _tty_write(text: str) -> None:
    if not text:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except OSError:
        pass


def _take_visible_start(leading: str, piece: str) -> "tuple[str, str | None]":
    """Skip leading whitespace. Returns (leading, start_text or None)."""
    leading += piece
    if not leading.strip():
        return leading, None
    return "", leading.lstrip("\r\n")


def _default_echo_delta(piece: str) -> None:
    _tty_write(piece)


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
        _tty_write(f"\r  {frame} generating…")
        self._shown = True

    def clear(self) -> None:
        self._stopped = True
        if not self._shown:
            return
        _tty_write("\r\033[K")
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
            self._leading, text = _take_visible_start(self._leading, piece)
            if text is None:
                return
            self._started = True
            if text:
                self._emit(text)
            return
        self._emit(piece)

    def _out(self, text: str) -> None:
        if text and self._write is not None:
            try:
                self._write(text)
            except OSError:
                pass

    def _emit(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        i = len(text)
        while i > 0 and text[i - 1] == "\n":
            i -= 1
        body, nl = text[:i], text[i:]
        if body:
            if self._trailing_nl:
                self._out(self._trailing_nl)
                self._trailing_nl = ""
            self._out(body)
        if nl:
            self._trailing_nl += nl

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

    def __init__(self, color: bool = True, *, console=None):
        self._parts: list = []
        self._leading = ""
        self._started = False
        self._live = None
        self._color = color
        self._console_override = console

    @property
    def visible(self) -> bool:
        return self._started

    def feed(self, piece: str) -> None:
        if not piece:
            return
        if not self._started:
            self._leading, text = _take_visible_start(self._leading, piece)
            if text is None:
                return
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
        _Console, Live, Markdown = rich
        from .markdown_view import make_console
        console = self._console_override or make_console(
            self._color, force_terminal=True,
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
            auto_refresh=self._console_override is None,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start()

    def _refresh(self) -> None:
        if self._live is None:
            return
        rich = _load_rich()
        if not rich:
            return
        _Console, _Live, Markdown = rich
        self._live.update(
            Markdown("".join(self._parts)),
            refresh=not self._live.auto_refresh,
        )

    def _stop_live(self) -> None:
        if self._live is None:
            return
        try:
            self._live.stop()
        except (OSError, RuntimeError):
            pass
        self._live = None

    def reset(self) -> None:
        self._parts = []
        self._leading = ""
        self._started = False
        self._stop_live()

    def finish(self) -> bool:
        self._leading = ""
        if not self._started:
            self.reset()
            return False
        if self._live is not None:
            self._refresh()
            self._stop_live()
        started = self._started
        self._parts = []
        self._started = False
        return started


class _ThinkingLive:
    """Stream thinking lines live; erase when tools/answer arrive.

    Full text is kept for Ctrl+O (prior-turn history). Mid-stream Ctrl+O is
    not handled here — keys are drained before the next prompt.

    SSE already halt-loops with ``_repeated_near_trailing_lines``; this layer
    skips painting near-duplicate lines that still arrived as distinct tokens.
    """

    def __init__(self, color: bool = True):
        self._color = color
        self._parts: list = []
        self._line_buf = ""
        self._lines_shown = 0
        self._active = False
        self._erased = False
        self._recent_printed: list = []

    @property
    def active(self) -> bool:
        return self._active and not self._erased

    def text(self) -> str:
        return "".join(self._parts)

    def feed(self, piece: str) -> None:
        if not piece or self._erased:
            return
        piece = piece.replace("\r\n", "\n")
        self._active = True
        self._parts.append(piece.replace("\r", ""))
        self._line_buf += piece
        while self._line_buf:
            cr = self._line_buf.find("\r")
            nl = self._line_buf.find("\n")
            if cr != -1 and (nl == -1 or cr < nl):
                self._line_buf = self._line_buf[cr + 1:]
                continue
            if nl == -1:
                break
            line, self._line_buf = self._line_buf.split("\n", 1)
            self._write_line(line.replace("\r", ""))

    def _use_tty(self) -> bool:
        return sys.stdout.isatty()

    def _write_line(self, line: str) -> None:
        stripped = line.strip()
        if stripped and any(
            _think_line_similar(stripped, prev) for prev in self._recent_printed
        ):
            return
        if stripped:
            self._recent_printed.append(stripped)
            if len(self._recent_printed) > 4:
                del self._recent_printed[0]
        dim = "\033[2m" if self._color and self._use_tty() else ""
        reset = "\033[0m" if dim else ""
        prefixed = f"{THINK_LINE_PREFIX}{line}"
        _tty_write(f"{dim}{prefixed}{reset}\n")
        cols = shutil.get_terminal_size((80, 24)).columns or 80
        self._lines_shown += _visual_rows(prefixed, cols)

    def _flush_partial(self) -> None:
        if self._line_buf:
            self._write_line(self._line_buf.replace("\r", "").rstrip("\n"))
            self._line_buf = ""

    def _rewind_shown_lines(self) -> None:
        if self._lines_shown and self._use_tty():
            height = shutil.get_terminal_size((80, 24)).lines or 24
            n = min(self._lines_shown, max(height - 1, 1))
            for _ in range(n):
                _tty_write("\033[1A\033[2K")
        self._lines_shown = 0

    def erase(self) -> str:
        """Remove thinking lines from the TTY; return full text for history."""
        if not self._active and not self._parts:
            return ""
        self._flush_partial()
        text = self.text()
        self._rewind_shown_lines()
        self._active = False
        self._erased = True
        return text

    def finish_keep(self) -> str:
        """Flush and rewind gray lines (answer is echoed as normal markdown)."""
        self._flush_partial()
        text = self.text()
        self._rewind_shown_lines()
        self._active = False
        return text


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
