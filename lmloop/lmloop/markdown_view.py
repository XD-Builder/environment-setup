"""Render markdown for the terminal (rich) and page it through less."""

import shutil
import subprocess
import sys
from io import StringIO
from typing import Iterable


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
    """Rich Console. Live uses ``soft_wrap=False`` so long lines wrap in the
    viewport instead of cropping. Final TTY prints use ``soft_wrap=True`` so
    the terminal (not inserted newlines) wraps — those lines reflow on resize.
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


def print_markdown(text: str, color: bool = True) -> None:
    """Print markdown rendered for the terminal. Falls back to plain text.

    On a TTY, rich does not hard-wrap: the terminal wraps long lines and
    reflows them when the window is resized.
    """
    if not text:
        return
    if not _rich_available() or not sys.stdout.isatty():
        print(text)
        return
    from rich.markdown import Markdown
    make_console(color, soft_wrap=True).print(Markdown(text))


def render_markdown_ansi(text: str, width: "int | None" = None, color: bool = True) -> str:
    """Return ANSI-rendered markdown suitable for ``less -R``."""
    if not text:
        return ""
    if not _rich_available():
        return text if text.endswith("\n") else text + "\n"
    from rich.markdown import Markdown
    from .ui import terminal_size

    cols = width or terminal_size((100, 24))[0]
    buf = StringIO()
    console = make_console(
        color, file=buf, width=max(40, cols),
        force_terminal=True, truecolor=True,
    )
    console.print(Markdown(text))
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
        content = (m.get("content") or "").strip()
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
