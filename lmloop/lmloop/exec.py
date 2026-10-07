"""Execution backends for shell commands (local default; docker opt-in later)."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .tools import DEFAULT_SHELL_TIMEOUT_S, ShellCommand, _truncate


@dataclass
class ExecResult:
    output: str
    exit_code: int


class LocalBackend:
    """Host subprocess backend (today's run_shell behavior)."""

    def run(
        self,
        command: str,
        *,
        workspace_root: Path,
        timeout_s: int = DEFAULT_SHELL_TIMEOUT_S,
        shell_syntax: bool = False,
    ) -> ExecResult:
        try:
            argv = command if shell_syntax else shlex.split(command)
        except ValueError as e:
            return ExecResult(output=f"ERROR: {e}", exit_code=127)
        try:
            proc = subprocess.Popen(
                argv,
                shell=shell_syntax,
                cwd=str(workspace_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as e:
            return ExecResult(output=f"ERROR: {e}", exit_code=127)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return ExecResult(
                output=f"ERROR: command timed out after {timeout_s}s",
                exit_code=124,
            )
        out = stdout or ""
        if stderr:
            out += ("\n[stderr]\n" + stderr)
        code = int(proc.returncode or 0)
        out += f"\n[exit code: {code}]"
        return ExecResult(output=_truncate(out.strip()), exit_code=code)


def build_backend(cfg: dict, *, docker: bool = False) -> LocalBackend:
    """Return the shell backend for this process. Docker is not implemented yet."""
    _ = (cfg, docker)
    return LocalBackend()
