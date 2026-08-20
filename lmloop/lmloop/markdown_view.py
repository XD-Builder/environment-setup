"""Render markdown for the terminal (rich) and page it through less."""

import shutil
import subprocess
import sys
from io import StringIO
from typing import Iterable

from .extract import flatten_content

# Fold at the console width. Rich's default crop silently drops the tail of
# list items, tables, and other laid-out renderables.
_MARKDOWN_PRINT = dict(overflow="fold", crop=False, no_wrap=False)


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


def make_console(
    color: bool = True,
    *,
    file=None,
    width: "int | None" = None,
    height: "int | None" = None,
    force_terminal: "bool | None" = None,
    truecolor: bool = False,
    soft_wrap: bool = False,
):
    """Rich Console. ``soft_wrap=False`` (default) wraps at width — required
    so markdown lists fold instead of cropping. Do not use ``soft_wrap=True``
    for assistant markdown: list/table renderables are cropped before the
    terminal ever sees the line.
    """
    from rich.console import Console
    if force_terminal is None:
        force_terminal = bool(color and sys.stdout.isatty())
    kwargs = {
        "highlight": False,
        "soft_wrap": soft_wrap,
        "color_system": (
            ("truecolor" if color else None) if truecolor
            else ("auto" if color else None)
        ),
        "force_terminal": force_terminal,
    }
    if file is not None:
        kwargs["file"] = file
    if width is not None:
        kwargs["width"] = width
    if height is not None:
        kwargs["height"] = height
    return Console(**kwargs)


def _print_markdown(console, text: str) -> None:
    from rich.markdown import Markdown
    console.print(Markdown(text), **_MARKDOWN_PRINT)


def _fold_overwide_lines(lines, max_width: int) -> list:
    """Split any rendered line wider than ``max_width``. Defense in depth
    when a markdown list/table ignores overflow=fold."""
    if max_width <= 0:
        return lines
    from rich.segment import Segment
    folded = []
    for line in lines:
        length = Segment.get_line_length(line)
        if length <= max_width:
            folded.append(line)
            continue
        cuts = list(range(max_width, length, max_width)) + [length]
        folded.extend(Segment.divide(line, cuts))
    return folded


def render_markdown_lines(console, text: str, options) -> list:
    """Markdown → segment lines. Fold horizontally; never crop a line.

    Do not use ``Console.render_lines``: this Rich version always crops via
    ``Segment.split_and_crop_lines``.
    """
    from rich.markdown import Markdown
    from rich.segment import Segment
    render_options = options.update(height=None, overflow="fold", no_wrap=False)
    rendered = console.render(Markdown(text), render_options)
    lines = list(Segment.split_lines(rendered))
    width = options.max_width or (options.size.width if options.size else 0)
    return _fold_overwide_lines(lines, width)


def print_markdown(text: str, color: bool = True) -> None:
    """Print markdown rendered for the terminal. Falls back to plain text.

    Wraps at the current terminal width (including list items). Resize
    reflow is secondary to never silently dropping the tail of a line.
    """
    if not text:
        return
    if not _rich_available() or not sys.stdout.isatty():
        print(text)
        return
    _print_markdown(make_console(color, soft_wrap=False), text)


def render_markdown_ansi(text: str, width: "int | None" = None, color: bool = True) -> str:
    """Return ANSI-rendered markdown suitable for ``less -R``."""
    if not text:
        return ""
    if not _rich_available():
        return text if text.endswith("\n") else text + "\n"
    from .ui import terminal_size

    cols = width or terminal_size((100, 24))[0]
    buf = StringIO()
    console = make_console(
        color, file=buf, width=max(40, cols),
        force_terminal=True, truecolor=True, soft_wrap=False,
    )
    _print_markdown(console, text)
    return buf.getvalue()


def page_ansi(rendered: str) -> int:
    """Show ANSI text in ``less -R``. Returns less exit code (0 if skipped)."""
    if not rendered.strip():
        return 0
    less = shutil.which("less")
    if not less or not sys.stdin.isatty() or not sys.stdout.isatty():
        # No pager available — print and return
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    try:
        proc = subprocess.run(
            [less, "-R", "-F"],
            input=rendered,
            text=True,
            check=False,
        )
        return proc.returncode
    except OSError:
        sys.stdout.write(rendered)
        return 0


def messages_to_markdown(messages: Iterable[dict]) -> str:
    """Build a readable markdown transcript from chat messages."""
    parts = ["# Session transcript", ""]
    for m in messages:
        role = m.get("role") or "?"
        content = flatten_content(m.get("content")).strip()
        if role == "system":
            continue
        if role == "tool":
            if not content:
                continue
            parts.append("### Tool")
            parts.append("")
            parts.append("```")
            parts.append(content[:4000])
            parts.append("```")
            parts.append("")
            continue
        if role == "assistant" and m.get("tool_calls") and not content:
            # tool-call-only turn — skip body
            continue
        title = {"user": "User", "assistant": "Assistant"}.get(role, role.title())
        parts.append(f"## {title}")
        parts.append("")
        parts.append(content or "_(empty)_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def page_transcript(messages: list, color: bool = True) -> int:
    """Render the session as markdown and open it in less."""
    md = messages_to_markdown(messages)
    if md.strip() in ("# Session transcript", "# Session transcript\n"):
        print("(empty transcript)")
        return 0
    return page_ansi(render_markdown_ansi(md, color=color))
