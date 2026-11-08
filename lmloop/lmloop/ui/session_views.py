"""REPL session controls that need no model: rewind, ``!`` shell context, views.

``repl.py`` owns the slash handlers; this module owns their state and text so
the logic is testable without a prompt session.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..capabilities import tools
from ..core import config

REWIND_MAX = 10
DENIALS_MAX = 10
SHELL_CONTEXT_MAX = 8000
SHELL_CONTEXT_HEADER = "Shell commands I ran (output for context):"
PLAN_MODE_NOTE = (
    "Plan mode is on: tools that change files or run shell commands are off. "
    "Read, investigate, and propose a plan; the user turns plan mode off to apply it."
)
_GIT_TIMEOUT_S = 10


@dataclass
class RewindEntry:
    label: str
    messages: list
    session_log: Path


@dataclass
class RewindStack:
    """Threads replaced by /new, /restore, /compact, or a rewind (newest last)."""

    entries: list = field(default_factory=list)

    def push(self, label: str, messages: list, session_log: Path) -> None:
        if not any(m.get("role") == "user" for m in messages):
            return
        self.entries.append(RewindEntry(label, list(messages), session_log))
        del self.entries[:-REWIND_MAX]

    def pop(self, n: int = 1) -> "RewindEntry | None":
        if n < 1 or n > len(self.entries):
            return None
        entry = self.entries[-n]
        del self.entries[-n:]
        return entry

    def lines(self) -> "list[str]":
        if not self.entries:
            return ["(nothing to rewind — /new, /restore, and /compact save the thread here)"]
        out = []
        for i, entry in enumerate(reversed(self.entries), 1):
            turns = sum(1 for m in entry.messages if m.get("role") == "user")
            out.append(f"  {i}. before {entry.label} — {turns} turn(s)")
        out.append("  bring one back with: /rewind <n>")
        return out


@dataclass
class ShellRuns:
    """``!cmd`` output waiting to ride along with the next model turn."""

    pending: list = field(default_factory=list)

    def add(self, command: str, output: str) -> None:
        self.pending.append(f"$ {command}\n{output.rstrip()}")

    def take_context(self) -> str:
        if not self.pending:
            return ""
        body = "\n\n".join(self.pending)
        self.pending = []
        if len(body) > SHELL_CONTEXT_MAX:
            body = "…" + body[-(SHELL_CONTEXT_MAX - 1):]
        return f"{SHELL_CONTEXT_HEADER}\n\n{body}"


def run_user_shell(command: str, workspace_root: Path, cfg: config.Config) -> str:
    """Run a command the user typed. No gate: the user is the one asking."""
    return tools.run_shell(
        command, confirm_gate=None, confirm_destructive=False,
        timeout_s=config.cfg_int(cfg, "shell_timeout_s") or tools.DEFAULT_SHELL_TIMEOUT_S,
        workspace_root=workspace_root,
    )


def _git(args: list, root: Path) -> "tuple[int, str]":
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).rstrip()


def diff_lines(root: Path) -> "list[str]":
    """git status plus a diff stat against HEAD (untracked files listed by status)."""
    code, status = _git(["status", "--short"], root)
    if code != 0:
        return [f"(not a git repository: {root})"]
    if not status:
        return ["(no changes — the working tree matches HEAD)"]
    lines = ["git status --short:", *("  " + line for line in status.splitlines())]
    code, stat = _git(["diff", "--stat", "HEAD"], root)
    if code == 0 and stat:
        lines += ["", "git diff --stat HEAD:", *("  " + line for line in stat.splitlines())]
    lines.append("full diff: !git diff")
    return lines


@dataclass
class DenialLog:
    """Recent gate answers of no, newest last, for /permissions."""

    rows: list = field(default_factory=list)

    def wrap(self, gate):
        def recording_gate(command: str) -> bool:
            allowed = gate(command)
            if not allowed:
                self.rows.append(command)
                del self.rows[:-DENIALS_MAX]
            return allowed
        return recording_gate


def permissions_lines(cfg: config.Config, workspace_root: Path, *, plan_mode: bool,
                      extra_dirs: list, denials: DenialLog) -> "list[str]":
    from ..mcp.config import McpPolicy
    from ..verify.hooks import HookRunner

    def on(key: str) -> str:
        return "on" if config.cfg_bool(cfg, key) else "off"

    lines = [
        f"Plan mode: {'on — edits and shell are off' if plan_mode else 'off'} (/readonly)",
        f"Confirm shell commands: {on('confirm_shell')} · destructive: "
        f"{on('confirm_destructive')}",
        "Always asks before:",
        *(f"  {prefix} — {label} ({tier})" for prefix, label, tier in tools.gate_kind_rows()),
        f"Readable roots: {workspace_root}"
        + "".join(f", {d}" for d in extra_dirs) + "  (/add-dir adds one)",
        *McpPolicy.load(workspace_root).describe_lines(),
        *HookRunner.load(workspace_root).describe_lines(),
    ]
    if denials.rows:
        lines.append("Recently declined:")
        lines.extend(f"  {row[:160]}" for row in reversed(denials.rows))
    return lines
