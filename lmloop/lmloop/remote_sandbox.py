"""Remote repository sandbox over HTTP (CS2680-style task container API).

Leaf module: urllib + json only. ``HttpSandboxBackend`` implements the same
``run`` / ``preflight`` / ``describe`` / ``close`` contract as ``LocalBackend``.
``TaskEvalSession`` adds patch / evaluate / continue / done for coding loops.

Expected routes on the task side::

    GET  /health
    POST /exec   {"command", "workdir"?, "timeout_s"?} -> stdout, stderr, exit_code
    POST /read   {"path"} -> content (UTF-8 text)
    POST /write  {"path", "content"} -> ok

Paths in ``/read`` and ``/write`` are relative to ``workdir`` unless absolute.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WORKDIR = "/workspace"
HEALTH_PATH = "/health"
EXEC_PATH = "/exec"
READ_PATH = "/read"
WRITE_PATH = "/write"
DEFAULT_TIMEOUT_S = 120


class RemoteSandboxError(RuntimeError):
    """HTTP or protocol failure talking to the task sandbox."""


class TaskEvalError(RuntimeError):
    """Invalid state transition on ``TaskEvalSession``."""


@dataclass(frozen=True)
class RemoteExecResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    spawn_error: bool = False


@dataclass(frozen=True)
class EvalOutcome:
    tests_failed: int
    tests_total: int
    attempt: int
    exit_code: int
    timed_out: bool = False
    spawn_error: bool = False


def resolve_sandbox_url(cfg: dict, cli_url: str = "") -> str:
    """CLI flag, then ``LMLOOP_SANDBOX_URL``, then ``config.json`` sandbox_url."""
    for raw in (
        (cli_url or "").strip(),
        __import__("os").environ.get("LMLOOP_SANDBOX_URL", "").strip(),
        str(cfg.get("sandbox_url") or "").strip(),
    ):
        if raw:
            return raw.rstrip("/")
    return ""


def resolve_sandbox_workdir(cfg: dict, cli_workdir: str = "") -> str:
    for raw in (
        (cli_workdir or "").strip(),
        __import__("os").environ.get("LMLOOP_SANDBOX_WORKDIR", "").strip(),
        str(cfg.get("sandbox_workdir") or "").strip(),
    ):
        if raw:
            return raw
    return DEFAULT_WORKDIR


class SandboxClient:
    """Minimal HTTP client for the task-container sandbox server."""

    def __init__(self, base_url: str, *, workdir: str = DEFAULT_WORKDIR, timeout_s: int = 30):
        self.base_url = base_url.rstrip("/")
        self.workdir = workdir
        self.timeout_s = timeout_s

    def _request(
        self,
        method: str,
        path: str,
        body: "dict | None" = None,
        *,
        timeout_s: "int | None" = None,
    ) -> dict:
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RemoteSandboxError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RemoteSandboxError(f"{method} {path}: {exc.reason}") from exc
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RemoteSandboxError(f"{method} {path}: invalid JSON: {raw[:200]}") from exc
        if not isinstance(parsed, dict):
            raise RemoteSandboxError(f"{method} {path}: expected JSON object")
        return parsed

    def health(self) -> None:
        req = urllib.request.Request(self.base_url + HEALTH_PATH, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                if resp.status >= 400:
                    raise RemoteSandboxError(f"health: HTTP {resp.status}")
        except urllib.error.URLError as exc:
            raise RemoteSandboxError(f"health: {exc.reason}") from exc

    def exec_command(self, command: str, *, timeout_s: int) -> RemoteExecResult:
        payload = {"command": command, "workdir": self.workdir, "timeout_s": timeout_s}
        data = self._request("POST", EXEC_PATH, payload, timeout_s=timeout_s + 5)
        return RemoteExecResult(
            stdout=str(data.get("stdout") or ""),
            stderr=str(data.get("stderr") or ""),
            exit_code=int(data.get("exit_code", data.get("returncode", 1))),
            timed_out=bool(data.get("timed_out")),
            spawn_error=bool(data.get("spawn_error")),
        )

    def read_text(self, rel_path: str) -> str:
        data = self._request("POST", READ_PATH, {"path": rel_path})
        if "error" in data:
            return f"ERROR: {data['error']}"
        return str(data.get("content") or "")

    def write_text(self, rel_path: str, content: str) -> "str | None":
        data = self._request("POST", WRITE_PATH, {"path": rel_path, "content": content})
        if "error" in data:
            return f"ERROR: {data['error']}"
        return None


def workspace_rel_path(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path outside workspace: {path}") from exc
    return rel.as_posix()


class TaskEvalSession:
    """Patch / evaluate / continue / done loop over a remote task checkout."""

    def __init__(
        self,
        backend: object,
        *,
        check_cmd: str,
        patch_cmd: str = "git diff",
    ):
        if not check_cmd.strip():
            raise ValueError("check_cmd is required")
        self.backend = backend
        self.check_cmd = check_cmd.strip()
        self.patch_cmd = patch_cmd.strip() or "git diff"
        self.attempt = 0
        self._finalized = False
        self._last_outcome: EvalOutcome | None = None

    def extract_patch(self) -> str:
        if self._finalized:
            raise TaskEvalError("task already finalized")
        root = self.backend.local_root  # type: ignore[attr-defined]
        result = self.backend.run(  # type: ignore[attr-defined]
            self.patch_cmd,
            timeout_s=DEFAULT_TIMEOUT_S,
            cwd=root,
            shell_syntax=True,
        )
        if result.spawn_error:
            raise TaskEvalError(result.stdout or "patch extraction failed")
        return result.stdout

    def evaluate(self) -> EvalOutcome:
        if self._finalized:
            raise TaskEvalError("task already finalized")
        self.attempt += 1
        root = self.backend.local_root  # type: ignore[attr-defined]
        result = self.backend.run(  # type: ignore[attr-defined]
            self.check_cmd,
            timeout_s=DEFAULT_TIMEOUT_S,
            cwd=root,
            shell_syntax=True,
        )
        failed = 1
        if not result.spawn_error and result.exit_code == 0 and not result.timed_out:
            failed = 0
        outcome = EvalOutcome(
            tests_failed=failed,
            tests_total=1,
            attempt=self.attempt,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            spawn_error=result.spawn_error,
        )
        self._last_outcome = outcome
        return outcome

    def continue_task(self) -> None:
        if self._finalized:
            raise TaskEvalError("task already finalized")
        self._last_outcome = None

    def done(self, reason: str, iterations: int) -> EvalOutcome:
        del reason, iterations
        if self._finalized:
            raise TaskEvalError("task already finalized")
        self._finalized = True
        if self._last_outcome is not None:
            return self._last_outcome
        return EvalOutcome(
            tests_failed=1,
            tests_total=1,
            attempt=max(self.attempt, 1),
            exit_code=1,
        )
