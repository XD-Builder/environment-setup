"""Live terminal display: spinner, token printers, thinking lines.

Close methods (clear / finish / erase / reset) must not raise — act() calls
them from ``finally`` during interrupt. Catch OSError at the write site.
"""

import sys

from .stream import _think_line_similar
from .ui import terminal_size

# Live markdown grows with the answer up to the terminal. Rich Live's
# ellipsis/crop keep the *start* of the document, so new tokens vanish behind
# a red "..." and leftover opening paragraphs stack in scrollback — we crop
# to the newest lines only once the render would overflow the screen.

# Live thinking prints as it streams; erased when tools/answer start.
# Ctrl+O opens prior-turn thinking (chronological), not a newest-first cycle.
THINK_LINE_PREFIX = "  ▎"
# Must cover a repeating plan block (often 8–16 lines), not just consecutive
# paraphrases. Stream halt is the real stop; this only skips painting.
_THINK_RECENT_LINES = 24

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


class _TailMarkdown:
    """Markdown that grows with the answer; tails only if it exceeds the viewport.

    Short replies occupy as many Live rows as they need. Once the render is
    taller than the terminal, keep the newest lines — Rich Live
    ``vertical_overflow="ellipsis"`` would keep the *start* and hide new
    tokens behind a red ``...``. ``visible`` overflow stacks frames in
    scrollback as markdown reflows.
    """

    def __init__(self, text: str):
        self.text = text

    def __rich_console__(self, console, options):
        from rich.markdown import Markdown
        from rich.segment import Segment
        cap = options.height or options.max_height or options.size.height
        render_options = options.update(height=None)
        lines = console.render_lines(
            Markdown(self.text), render_options, pad=False,
        )
        if cap and len(lines) > cap:
            lines = lines[-cap:]
        new_line = Segment.line()
        for i, line in enumerate(lines):
            yield from line
            if i < len(lines) - 1:
                yield new_line


def _live_preview_height() -> int:
    """Max Live rows: grow with content up to the terminal, not a 16-line box."""
    _, lines = terminal_size()
    return max(4, lines - 2)


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
        _Console, Live, _Markdown = rich
        from .markdown_view import make_console
        if self._console_override is not None:
            console = self._console_override
        else:
            console = make_console(
                self._color, force_terminal=True,
                height=_live_preview_height(),
            )
        # crop (not ellipsis/visible): _TailMarkdown already fits the
        # terminal. ellipsis kept the *start* of a tall answer and painted a
        # red "..." while new tokens arrived off-screen. visible overflow
        # stacks frames in scrollback as markdown reflows.
        # transient on a real TTY: clear the preview so finish() can reprint
        # the full answer with terminal wrap (reflows on resize).
        self._live = Live(
            _TailMarkdown(""),
            console=console,
            refresh_per_second=12,
            vertical_overflow="crop",
            auto_refresh=self._console_override is None,
            redirect_stdout=False,
            redirect_stderr=False,
            transient=self._console_override is None,
        )
        self._live.start()

    def _refresh(self) -> None:
        if self._live is None:
            return
        rich = _load_rich()
        if not rich:
            return
        self._live.update(
            _TailMarkdown("".join(self._parts)),
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
        body = "".join(self._parts)
        if not self._started:
            self.reset()
            return False
        if self._live is not None:
            self._refresh()
            self._stop_live()
        # Real TTY: Live was transient, so reprint with terminal wrap so a
        # later resize reflows. Tests that inject a console keep the Live frame.
        if body and self._console_override is None:
            from .markdown_view import print_markdown
            print_markdown(body, color=self._color)
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
        self._last_shown_blank = False

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
        if not stripped:
            if self._last_shown_blank:
                return
            self._last_shown_blank = True
        else:
            self._last_shown_blank = False
            self._recent_printed.append(stripped)
            if len(self._recent_printed) > _THINK_RECENT_LINES:
                del self._recent_printed[0]
        dim = "\033[2m" if self._color and self._use_tty() else ""
        reset = "\033[0m" if dim else ""
        prefixed = f"{THINK_LINE_PREFIX}{line}"
        _tty_write(f"{dim}{prefixed}{reset}\n")
        cols, _ = terminal_size()
        self._lines_shown += _visual_rows(prefixed, cols)

    def _flush_partial(self) -> None:
        if self._line_buf:
            self._write_line(self._line_buf.replace("\r", "").rstrip("\n"))
            self._line_buf = ""

    def _rewind_shown_lines(self) -> None:
        if self._lines_shown and self._use_tty():
            _, height = terminal_size()
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
