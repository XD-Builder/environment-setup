"""Parse ``until`` CLI/REPL flags and graph ``until`` nodes (leaf)."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

_UNTIL_USAGE = "usage: until [--check <cmd>] [--keep <cmd>] <goal>"


@dataclass(frozen=True)
class UntilArgs:
    """Parsed ``until`` invocation. Empty checks and keeps means infer a plan."""

    goal: str = ""
    checks: tuple = ()
    keeps: tuple = ()
    err: "str | None" = None

    @property
    def check_cmd(self) -> "str | None":
        return self.checks[0] if self.checks else None


def split_check_flags(words: list) -> "tuple[tuple, tuple, list, str | None]":
    """Pull repeatable ``--check`` / ``--keep`` off ``words``. Fail closed."""
    checks: list[str] = []
    keeps: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("--check", "--keep"):
            if i + 1 >= len(words) or str(words[i + 1]).startswith("--"):
                return (), (), [], _UNTIL_USAGE
            cmd = str(words[i + 1]).strip()
            if not cmd:
                return (), (), [], _UNTIL_USAGE
            (checks if word == "--check" else keeps).append(cmd)
            i += 2
            continue
        rest.append(word)
        i += 1
    return tuple(checks), tuple(keeps), rest, None


def parse_until_args(words: list) -> UntilArgs:
    """Parse an until goal. ``err`` is set when the line is invalid."""
    checks, keeps, rest, err = split_check_flags(words)
    if err:
        return UntilArgs(err=err)
    goal = " ".join(rest).strip()
    if not goal:
        return UntilArgs(checks=checks, keeps=keeps, err=_UNTIL_USAGE)
    return UntilArgs(goal=goal, checks=checks, keeps=keeps)


def parse_until_arg_line(arg: str) -> UntilArgs:
    """Parse a REPL /until argument line with shlex (quoted --check)."""
    try:
        words = shlex.split(arg or "")
    except ValueError as e:
        return UntilArgs(err=str(e))
    return parse_until_args(words)
