"""Scripted-model harness for component baselines and holistic agent runs.

The fake server speaks the OpenAI-compatible slice lmloop actually calls
(``GET /v1/models``, ``POST /v1/chat/completions``). Scenarios are a queue of
assistant messages, so a regression that adds or drops a model round fails
the test instead of silently drifting.

No network beyond 127.0.0.1. Docker image checks live in
``test_docker_sandbox.py`` and stay opt-in (``LMLOOP_DOCKER_E2E=1``).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
SKIP_AGENT_E2E = os.environ.get("LMLOOP_SKIP_AGENT_E2E") == "1"
PAGE_BODY = "PAGE_TOKEN docker-local fixture\n"


def text_step(content: str) -> dict:
    return {"role": "assistant", "content": content}


def tool_step(name: str, arguments: dict, *, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


@dataclass
class Script:
    """Ordered assistant turns. Extra calls increment ``overflow``."""

    steps: list = field(default_factory=list)
    index: int = 0
    requests: list = field(default_factory=list)
    overflow: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_message(self, record: dict) -> dict:
        with self._lock:
            self.requests.append(record)
            if self.index >= len(self.steps):
                self.overflow += 1
                return text_step("SCRIPT EXHAUSTED")
            step = self.steps[self.index]
            self.index += 1
            return step

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, len(self.steps) - self.index)

    def blob(self) -> str:
        with self._lock:
            return json.dumps(self.requests)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/v1/models":
            self._send_json({
                "object": "list",
                "data": [{"id": "scripted-local"}],
            })
            return
        if path == "/fixture-page":
            body = PAGE_BODY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        if path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        record = {
            "path": path,
            "authorization": self.headers.get("Authorization") or "",
            "http_referer": self.headers.get("HTTP-Referer") or "",
            "x_title": self.headers.get("X-Title") or "",
            "body": body,
        }
        message = self.server.script.next_message(record)  # type: ignore[attr-defined]
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        self._send_json({
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "total_tokens": 18,
            },
        })

    def _send_json(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ScriptedServer:
    def __init__(self, script: Script):
        self.script = script
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.script = script  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.port = int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def page_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/fixture-page"

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("scripted model server did not accept connections")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def default_config(base_url: str, **overrides) -> dict:
    cfg = {
        "base_url": base_url,
        "model": "scripted-local",
        "auto_start_server": False,
        "stream": False,
        "color": False,
        "confirm_shell": False,
        "confirm_destructive": False,
        "until_mine": False,
        "graph_mine": False,
        "mine_on_exit": False,
        "autonomous_snapshot": "off",
        "context_length": 8192,
        "vision": "false",
        "temperature": 0,
        "max_rounds": 8,
        "timeout_s": 30,
        "use_graph": False,
    }
    cfg.update(overrides)
    return cfg


def write_config(home: Path, cfg: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")


def child_env(home: Path, extra: "dict | None" = None) -> dict:
    env = os.environ.copy()
    for key in (
        "OPENROUTER_API_KEY",
        "LMLOOP_API_KEY",
        "OPENROUTER_HTTP_REFERER",
        "OPENROUTER_X_TITLE",
    ):
        env.pop(key, None)
    env["LMLOOP_HOME"] = str(home)
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["NO_COLOR"] = "1"
    env["LMLOOP_USAGE"] = "1"
    if extra:
        env.update(extra)
    return env


def run_lmloop(
    args: list[str],
    *,
    home: Path,
    workspace: Path,
    env_extra: "dict | None" = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "lmloop", *args],
        cwd=workspace,
        env=child_env(home, env_extra),
        input="",
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass
class AgentRun:
    code: int
    out: str
    err: str
    home: Path
    workspace: Path
    script: Script

    def transcript(self) -> str:
        return self.out + "\n" + self.err + "\n" + self.script.blob()


def isolate_git(workspace: Path) -> None:
    """Give the fixture its own repo so git-aware tools do not see a parent checkout."""
    if (workspace / ".git").exists():
        return
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def run_agent(
    args: list[str],
    *,
    home: Path,
    workspace: Path,
    steps: "list | Callable[[ScriptedServer], list]",
    config: "dict | None" = None,
    env_extra: "dict | None" = None,
    timeout: int = 60,
) -> AgentRun:
    isolate_git(workspace)
    script = Script()
    server = ScriptedServer(script)
    server.start()
    try:
        planned = steps(server) if callable(steps) else steps
        script.steps.extend(planned)
        write_config(home, default_config(server.base_url, **(config or {})))
        proc = run_lmloop(
            args, home=home, workspace=workspace,
            env_extra=env_extra, timeout=timeout,
        )
        return AgentRun(
            code=proc.returncode,
            out=proc.stdout,
            err=proc.stderr,
            home=home,
            workspace=workspace,
            script=script,
        )
    finally:
        server.stop()


def project_dir(home: Path, workspace: Path) -> Path:
    return home / "projects" / workspace.name


def read_jsonl_tree(directory: Path) -> str:
    if not directory.is_dir():
        return ""
    parts = []
    for path in sorted(directory.rglob("*.jsonl")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def init_git(root: Path) -> None:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "ci",
        "GIT_AUTHOR_EMAIL": "ci@example.com",
        "GIT_COMMITTER_NAME": "ci",
        "GIT_COMMITTER_EMAIL": "ci@example.com",
    })
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=root, check=True,
        capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ci"],
        cwd=root, check=True, capture_output=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=root, check=True,
        capture_output=True, env=env,
    )


def append_report(row: dict) -> None:
    path = os.environ.get("LMLOOP_BASELINE_REPORT", "").strip()
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def baseline(component: str, scenario: str, *, kind: str = "baseline"):
    """Record a scorecard row around a unittest method."""

    def deco(method):
        def wrapper(self):
            import unittest
            started = time.perf_counter()
            status = "pass"
            error = ""
            try:
                method(self)
            except unittest.SkipTest:
                status = "skip"
                raise
            except Exception as exc:
                status = "fail"
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                runtime = "docker-local" if os.environ.get("LMLOOP_DOCKER_LOCAL") == "1" else "host"
                append_report({
                    "component": component,
                    "scenario": scenario,
                    "kind": kind,
                    "status": status,
                    "passed": status == "pass",
                    "seconds": round(time.perf_counter() - started, 3),
                    "runtime": runtime,
                    "error": error,
                })

        wrapper.__name__ = method.__name__
        wrapper.__doc__ = method.__doc__
        return wrapper

    return deco


def summarize_report(path: str) -> int:
    """Print a scorecard. Exit 1 when any row failed or nothing passed."""
    target = Path(path)
    if not target.is_file():
        print(f"missing baseline report: {target}", file=sys.stderr)
        return 1
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    fails = [row for row in rows if row.get("status") == "fail"]
    passes = [row for row in rows if row.get("status") == "pass"]
    skips = [row for row in rows if row.get("status") == "skip"]
    print(f"baselines: {len(passes)} passed, {len(fails)} failed, {len(skips)} skipped")
    for row in rows:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}.get(row.get("status"), "?")
        print(
            f"  {mark}  {row.get('component')} :: {row.get('scenario')}"
            f"  ({row.get('seconds')}s, {row.get('runtime')})"
        )
    if fails or not passes:
        return 1
    return 0


def assert_agent_ok(case, run: AgentRun) -> None:
    case.assertEqual(run.code, 0, run.transcript())
    case.assertEqual(run.script.overflow, 0, "unexpected extra model call\n" + run.transcript())
    case.assertEqual(run.script.remaining, 0, "scripted turn was not consumed\n" + run.transcript())
    case.assertNotIn("SCRIPT EXHAUSTED", run.out + run.err)
