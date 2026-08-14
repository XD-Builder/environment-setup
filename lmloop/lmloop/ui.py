"""Portable terminal UI for lmloop.

Rich ANSI styling when stdout is a TTY and color is enabled; plain text otherwise.
Respects NO_COLOR and TERM=dumb. Never colors model-generated assistant content.
"""

import os
import re
import shutil
import sys
from pathlib import Path

from . import agent, tools


def _supports_color(enabled: bool = True) -> bool:
    if not enabled:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stdout.isatty()


class Theme:
    """ANSI escape sequences; empty strings when color is disabled."""

    def __init__(self, enabled: bool = True):
        self._on = _supports_color(enabled)
        if self._on:
            self.reset = "\033[0m"
            self.bold = "\033[1m"
            self.dim = "\033[2m"
            self.red = "\033[31m"
            self.green = "\033[32m"
            self.yellow = "\033[33m"
            self.cyan = "\033[36m"
            self.bold_yellow = "\033[1;33m"
            self.dim_cyan = "\033[2;36m"
        else:
            self.reset = self.bold = self.dim = ""
            self.red = self.green = self.yellow = self.cyan = ""
            self.bold_yellow = self.dim_cyan = ""

    def c(self, text: str, *codes: str) -> str:
        if not self._on or not codes:
            return text
        return "".join(codes) + text + self.reset


def terminal_size(fallback: tuple = (80, 24)) -> tuple:
    """Current TTY columns, lines. Never raises."""
    try:
        ts = shutil.get_terminal_size(fallback)
        cols = ts.columns or fallback[0]
        lines = ts.lines or fallback[1]
        return max(1, cols), max(1, lines)
    except OSError:
        return fallback


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def drain_tty_input() -> int:
    """Discard bytes waiting on stdin (keys typed while the model streamed).

    Without this, a mid-stream Ctrl+O sits in the TTY buffer and fires as soon
    as prompt_toolkit resumes — opening ``less`` and looking like the stream
    was interrupted.
    """
    if not sys.stdin.isatty():
        return 0
    import fcntl
    import os
    import select

    n = 0
    fd = sys.stdin.fileno()
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                n += len(chunk)
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except (OSError, ValueError):
        return n
    return n


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def fresh_stats() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "turns": 0,
        "rounds": 0,
        "tool_calls": 0,
        "last_prompt_tokens": 0,
        "last_completion_tokens": 0,
    }


def count_messages_by_role(messages: list) -> dict:
    counts = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    for m in messages:
        role = m.get("role", "?")
        if role in counts:
            counts[role] += 1
    return counts


def context_fill(stats: dict, messages: list, context_limit: int, reserve: int) -> "tuple[int, int, float]":
    """Return (used_tokens, effective_limit, fill_ratio). effective_limit = limit - reserve."""
    used = stats.get("last_prompt_tokens", 0)
    if not used and messages:
        used = agent.estimate_context_tokens(messages)
    effective = max((context_limit or 0) - max(reserve, 0), 1)
    ratio = min(used / effective, 1.0) if context_limit else 0.0
    return used, effective, ratio


def context_bar(used: int, limit: int, ratio: float, width: int = 16,
                compact: bool = False, theme: "Theme | None" = None) -> str:
    """Render a fill bar. ``limit`` should be the same denominator as ``ratio``
    (typically effective = context_limit - reserve)."""
    if limit <= 0:
        return ""
    filled = min(width, int(ratio * width))
    bar = "█" * filled + "░" * (width - filled)
    pct = int(ratio * 100)
    left = max(limit - used, 0)
    if compact:
        suffix = f" {format_tokens(used)}/{format_tokens(limit)} ({pct}%)"
    else:
        suffix = (
            f"  {format_tokens(used)}/{format_tokens(limit)} ({pct}%)"
            f"  ·  {format_tokens(left)} left"
        )
    if theme and theme._on:
        if ratio >= 0.9:
            bar = theme.c(bar, theme.red)
        elif ratio >= 0.7:
            bar = theme.c(bar, theme.yellow)
        else:
            bar = theme.c(bar, theme.green)
        suffix = theme.c(suffix, theme.dim)
    return f"[{bar}]{suffix}"


class Console:
    """Unified human-facing output for REPL and CLI subcommands."""

    def __init__(self, color: bool = True):
        self.t = Theme(color)

    def banner(self, project: str) -> None:
        t = self.t
        print(
            f"{t.c('project=', t.dim)}{t.c(project, t.cyan)} · "
            f"{t.c('/help', t.green)} for commands · "
            f"{t.c('Tab', t.green)} /cmds · "
            f"{t.c('@file', t.green)} · "
            f"{t.c('/transcript', t.green)}"
        )

    def print_markdown(self, text: str) -> None:
        """Render assistant markdown to the terminal."""
        from .markdown_view import print_markdown
        print_markdown(text, color=self.t._on)

    def status_line(self, stats: dict, user_turns: int, context_limit: int = 0,
                    messages: "list | None" = None, reserve: int = 2048,
                    inline: bool = False) -> "str | None":
        inner = self._status_parts(stats, user_turns, context_limit, messages or [], reserve)
        if not inner:
            return None
        if inline:
            return inner
        t = self.t
        return t.c("[", t.dim) + inner + t.c("]", t.dim)

    def _status_parts(self, stats: dict, user_turns: int, context_limit: int,
                      messages: list, reserve: int) -> "str | None":
        total = stats.get("total_tokens", 0)
        used, effective, ratio = context_fill(stats, messages, context_limit, reserve)
        has_ctx = context_limit > 0 and (used > 0 or user_turns > 0)
        if user_turns <= 0 and not total and not has_ctx:
            return None
        t = self.t
        parts = []
        if has_ctx:
            parts.append(context_bar(used, effective, ratio, compact=True, theme=t))
        if total:
            parts.append(t.c(f"{format_tokens(total)} session", t.yellow))
        if user_turns > 0:
            word = "turn" if user_turns == 1 else "turns"
            parts.append(t.c(f"{user_turns} {word}", t.yellow))
        return t.c(" · ", t.dim).join(parts)

    def read_prompt(self, stats: dict, user_turns: int, context_limit: int = 0,
                    messages: "list | None" = None, reserve: int = 2048) -> str:
        """Read a line; show compact status right-aligned when it fits.

        Status is painted on the right, then readline owns the ``› `` prompt on
        the left — never ``input(\"\")`` with a hand-drawn prompt (breaks Ctrl+W).
        """
        left = format_input_prompt()
        status = self.status_line(
            stats, user_turns, context_limit, messages, reserve, inline=True,
        )
        if status and sys.stdin.isatty() and sys.stdout.isatty():
            try:
                cols, _ = terminal_size()
                min_input = 24
                max_status = cols - len(left) - min_input
                if max_status >= 16:
                    status = self._truncate_status(status, max_status)
                    pad = cols - visible_len(status) - len(left)
                    if pad >= 0:
                        sys.stdout.write(" " * pad + status + "\r")
                        sys.stdout.flush()
                        return input(left).strip()
            except OSError:
                pass
        block = self.status_line(
            stats, user_turns, context_limit, messages, reserve, inline=False,
        )
        if block:
            print(block)
        return input(left).strip()

    def _truncate_status(self, status: str, max_width: int) -> str:
        if visible_len(status) <= max_width:
            return status
        plain = strip_ansi(status)
        if len(plain) <= max_width:
            return status
        # Prefer keeping the context bar (starts with '['); drop tail stats first.
        if " · " in plain:
            bar, _, _rest = plain.partition(" · ")
            if len(bar) + 4 <= max_width:
                return bar + " · …"
        return plain[: max(0, max_width - 1)] + "…"

    def stats_footer(self, stats: dict, context_limit: int = 0,
                     messages: "list | None" = None, reserve: int = 2048) -> "str | None":
        total = stats.get("total_tokens", 0)
        if not total and not stats.get("turns"):
            return None
        t = self.t
        last_in = stats.get("last_prompt_tokens", 0)
        last_out = stats.get("last_completion_tokens", 0)
        parts = []
        if context_limit and messages is not None:
            used, effective, ratio = context_fill(stats, messages, context_limit, reserve)
            if used:
                parts.append(context_bar(used, effective, ratio, width=20, theme=t))
        if total:
            parts.append(t.c(
                f"{format_tokens(total)} session"
                f" ({format_tokens(stats.get('prompt_tokens', 0))} in + "
                f"{format_tokens(stats.get('completion_tokens', 0))} out)",
                t.yellow,
            ))
        if last_in or last_out:
            parts.append(t.c(
                f"last turn {format_tokens(last_in)} in + {format_tokens(last_out)} out",
                t.dim,
            ))
        parts.append(t.c(f"{stats.get('rounds', 0)} rounds", t.dim))
        parts.append(t.c(f"{stats.get('tool_calls', 0)} tools", t.dim))
        parts.append(t.c(f"{stats.get('turns', 0)} turns", t.dim))
        body = t.c(" · ", t.dim).join(parts)
        rule = t.c("──", t.dim)
        return f"  {rule} {body} {rule}"

    def stats_detail(self, stats: dict, model: str, messages: list,
                     session_log: Path, learnings: int, decisions: int,
                     context_limit: int = 0, reserve: int = 2048) -> str:
        t = self.t
        counts = count_messages_by_role(messages)
        ctx_parts = [f"{counts[r]} {r}" for r in ("system", "user", "assistant", "tool") if counts[r]]
        ctx_breakdown = " · ".join(ctx_parts) if ctx_parts else "empty"

        def row(label: str, value: str, style: str = "") -> str:
            lbl = t.c(f"{label}:", t.dim)
            val = t.c(value, style) if style else value
            return f"  {lbl:<18} {val}"

        lines = [
            t.c("Session stats:", t.bold),
            row("model", model, t.cyan),
            row("session log", session_log.name),
            row("context", f"{len(messages)} messages ({ctx_breakdown})"),
        ]
        if context_limit:
            used, effective, ratio = context_fill(stats, messages, context_limit, reserve)
            est = " (estimated)" if not stats.get("last_prompt_tokens") and used else ""
            lines.append(row(
                "context fill",
                context_bar(used, effective, ratio, width=24, theme=t) + est,
            ))
            lines.append(row(
                "window",
                f"{format_tokens(effective)} usable · {format_tokens(context_limit)} total · "
                f"{format_tokens(reserve)} reserved for reply",
                t.dim,
            ))
        elif stats.get("last_prompt_tokens"):
            lines.append(row(
                "context used",
                f"{format_tokens(stats['last_prompt_tokens'])} prompt tokens (limit unknown)",
                t.yellow,
            ))
        else:
            lines.append(row(
                "context",
                "limit unknown — set via `lmloop config set context_length <n>`",
                t.dim,
            ))
        lines.extend([
            row("user turns", str(stats.get("turns", 0)), t.yellow),
            row("api rounds", str(stats.get("rounds", 0))),
            row("tool calls", str(stats.get("tool_calls", 0))),
        ])
        total = stats.get("total_tokens", 0)
        if total:
            lines.append(row(
                "tokens",
                f"{format_tokens(total)} total "
                f"({format_tokens(stats.get('prompt_tokens', 0))} prompt + "
                f"{format_tokens(stats.get('completion_tokens', 0))} completion)",
                t.yellow,
            ))
        else:
            lines.append(row("tokens", "(server did not report usage)", t.dim))
        lines.append(row("memory", f"{learnings} learnings, {decisions} active decisions"))
        lines.append(row("cwd", str(Path.cwd()), t.dim))
        return "\n".join(lines)

    def help_text(self, commands: list) -> str:
        t = self.t
        lines = [t.c("commands:", t.bold)]
        for cmd in commands:
            if cmd.exits:
                continue
            name = t.c(cmd.name, t.green)
            arg = f" {cmd.arg_hint}" if cmd.arg_hint else ""
            pad = max(1, 22 - len(cmd.name) - len(arg))
            lines.append(f"  {name}{arg}{' ' * pad}{cmd.desc}")
        lines.append(f"  {t.c('/quit', t.green)}{' ' * 16}exit")
        lines.append("")
        lines.append(f"  {t.c('Tab', t.green)} completes /commands, /skill names, and @files")
        lines.append(
            f"  {t.c('Ctrl-C', t.green)} dismisses / @ completion, then twice to exit"
        )
        lines.append(
            f"  {t.c('Ctrl-O', t.green)} opens prior-turn thinking "
            f"(oldest first; live thinking already streamed)"
        )
        return "\n".join(lines)

    def round_usage(self, round_idx: int, stats: "dict | None", messages: list,
                    context_limit: int = 0, reserve: int = 2048) -> None:
        """Dim breadcrumb after each model round (in/out + optional context)."""
        if not stats:
            return
        t = self.t
        last_in = stats.get("last_prompt_tokens", 0)
        last_out = stats.get("last_completion_tokens", 0)
        parts = [f"↳ round {round_idx}"]
        if last_in or last_out:
            parts.append(
                f"{format_tokens(last_in)} in + {format_tokens(last_out)} out"
            )
        if context_limit and messages is not None:
            used, _, _ratio = context_fill(stats, messages, context_limit, reserve)
            if used:
                parts.append(f"ctx {format_tokens(used)}/{format_tokens(context_limit)}")
        print(t.c(" · ".join(parts), t.dim))

    def tool_call(self, name: str, args_preview: str, stats: "dict | None" = None) -> None:
        t = self.t
        line = f"  ⚙ {name}({args_preview})"
        if stats and stats.get("total_tokens"):
            line += f"  ·  {format_tokens(stats['total_tokens'])} session"
        print(t.c(line, t.dim_cyan))

    def warn(self, msg: str) -> None:
        print(self.t.c(msg, self.t.bold_yellow))

    def error(self, msg: str, file=None) -> None:
        print(self.t.c(msg, self.t.red), file=file or sys.stderr)

    def hint(self, msg: str) -> None:
        print(self.t.c(msg, self.t.dim))

    def info(self, msg: str) -> None:
        print(msg)

    def prompt(self, model: str = "") -> str:
        """Plain readline-safe prompt — decorative only, never sent to the model."""
        return format_input_prompt(model)

    def completion_menu(self, matches: list, descriptions: dict,
                        skill_mode: bool = False, redisplay: bool = True) -> None:
        print()
        t = self.t
        for m in matches:
            if skill_mode:
                print(f"  {t.c('/skill ' + m, t.green)}")
            elif m in descriptions:
                print(f"  {t.c(m, t.green)}  {t.c('—', t.dim)} {descriptions[m]}")
            else:
                print(f"  {m}")
        if redisplay:
            try:
                import readline
                readline.redisplay()
            except ImportError:
                pass

    def confirm_prompt(self, command: str = "") -> str:
        """y/N prompt. Optionally embed a short command preview in the line."""
        if not command:
            return "  run it? [y/N] "
        one_line = " ".join(command.split())
        if len(one_line) > 72:
            one_line = one_line[:71] + "…"
        return f"  run it? ({one_line}) [y/N] "

    def suggest_command(self, suggestion: str) -> None:
        cmd = self.t.c(suggestion, self.t.green)
        self.hint(f"  unknown command — did you mean {cmd}? (Tab to complete)")

    def unknown_command(self) -> None:
        help_cmd = self.t.c("/help", self.t.green)
        self.hint(f"  unknown command — try {help_cmd} (Tab to complete /commands)")


def short_path(path: "Path | None" = None, max_len: int = 40) -> str:
    """Home-relative cwd for display; truncates from the left when long."""
    p = (path or Path.cwd()).resolve()
    try:
        display = "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        display = str(p)
    if len(display) > max_len:
        display = "…" + display[-(max_len - 1):]
    return display


def short_model_name(model: str, max_len: int = 28) -> str:
    """Last path segment of a model id, truncated for the prompt."""
    if not model:
        return ""
    name = model.rsplit("/", 1)[-1]
    if len(name) > max_len:
        return name[: max_len - 1] + "…"
    return name


def format_input_prompt(model: str = "", cwd: "Path | None" = None) -> str:
    """Decorative REPL prompt text (cwd · model ›). Not LLM input."""
    parts = [short_path(cwd)]
    name = short_model_name(model)
    if name:
        parts.append(name)
    return " · ".join(parts) + " › "


def ask_yes_no(message: str) -> bool:
    """Read a y/N answer via plain input().

    Do not nest PromptSession.prompt() here during an agent turn: after
    streaming, prompt_toolkit redraws the bottom toolbar and can hide the
    command warning, leaving a bare ``run it? [y/N]`` that looks stuck.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        answer = input(message)
        return (answer or "").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def make_confirm_gate(console: "Console"):
    """y/N gate for destructive / shell-syntax commands (plain input)."""
    def confirm_gate(command: str) -> bool:
        label = "potentially destructive"
        if tools.needs_shell(command) and not tools.is_destructive(command):
            label = "shell-syntax (pipes/redirections)"
        console.warn(f"\n⚠ {label} command requested:\n    {command}")
        return ask_yes_no(console.confirm_prompt(command))
    return confirm_gate
