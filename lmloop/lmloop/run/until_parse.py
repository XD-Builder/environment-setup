"""Parse ``until`` CLI/REPL flags and graph ``until`` nodes (leaf)."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .run_artifacts import normalize_until_control

_UNTIL_USAGE = (
    "usage: until [--check <cmd>] [--keep <cmd>] [--control off|budget|meta] <goal>"
)


@dataclass(frozen=True)
class UntilArgs:
    """Parsed ``until`` invocation. Empty checks and keeps means infer a plan."""

    goal: str = ""
    checks: tuple = ()
    keeps: tuple = ()
    control: "str | None" = None
    err: "str | None" = None

    @property
    def check_cmd(self) -> "str | None":
        return self.checks[0] if self.checks else None


def split_check_flags(words: list) -> "tuple[tuple, tuple, str | None, list, str | None]":
    """Pull ``--check`` / ``--keep`` / ``--control`` off ``words``. Fail closed."""
    checks: list[str] = []
    keeps: list[str] = []
    control: "str | None" = None
    rest: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--control":
            if i + 1 >= len(words) or str(words[i + 1]).startswith("--"):
                return (), (), None, [], _UNTIL_USAGE
            control = normalize_until_control(str(words[i + 1]))
            i += 2
            continue
        if word in ("--check", "--keep"):
            if i + 1 >= len(words) or str(words[i + 1]).startswith("--"):
                return (), (), None, [], _UNTIL_USAGE
            cmd = str(words[i + 1]).strip()
            if not cmd:
                return (), (), None, [], _UNTIL_USAGE
            (checks if word == "--check" else keeps).append(cmd)
            i += 2
            continue
        rest.append(word)
        i += 1
    return tuple(checks), tuple(keeps), control, rest, None


def parse_until_args(words: list) -> UntilArgs:
    checks, keeps, control, rest, err = split_check_flags(words)
    if err:
        return UntilArgs(err=err)
    goal = " ".join(rest).strip()
    if not goal:
        return UntilArgs(checks=checks, keeps=keeps, control=control, err=_UNTIL_USAGE)
    return UntilArgs(goal=goal, checks=checks, keeps=keeps, control=control)


def parse_until_arg_line(arg: str) -> UntilArgs:
    try:
        words = shlex.split(arg or "")
    except ValueError as e:
        return UntilArgs(err=str(e))
    return parse_until_args(words)
