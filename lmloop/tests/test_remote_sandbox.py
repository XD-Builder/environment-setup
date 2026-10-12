"""HTTP remote sandbox backend and task eval session."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lmloop.exec import HttpSandboxBackend, build_backend, install_backend, reset_active_backend
from lmloop.remote_sandbox import TaskEvalSession
from lmloop.tools import read_file, run_shell, update_file, write_file


class _SandboxState:
    root: Path
    workdir: str = "/workspace"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        data = self._read_json()
        root = _SandboxState.root
        if self.path == "/exec":
            cmd = str(data.get("command") or "")
            timeout_s = int(data.get("timeout_s") or 30)
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                self._send_json(
                    200,
                    {
                        "stdout": proc.stdout,
                        "stderr": proc.stderr,
                        "exit_code": proc.returncode,
                    },
                )
            except subprocess.TimeoutExpired:
                self._send_json(200, {"stdout": "", "stderr": "", "exit_code": 124, "timed_out": True})
            return
        if self.path == "/read":
            rel = str(data.get("path") or "")
            target = root / rel
            if not target.is_file():
                self._send_json(200, {"error": "not found"})
                return
            self._send_json(200, {"content": target.read_text()})
            return
        if self.path == "/write":
            rel = str(data.get("path") or "")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(data.get("content") or ""))
            self._send_json(200, {"ok": True})
            return
        self.send_response(404)
        self.end_headers()


def _start_server(root: Path) -> ThreadingHTTPServer:
    _SandboxState.root = root
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class RemoteSandboxTests(unittest.TestCase):
    def setUp(self):
        reset_active_backend()
        self.tmp = tempfile.TemporaryDirectory()
        self.host_root = Path(self.tmp.name)
        (self.host_root / "a.txt").write_text("hello\n")
        self.server = _start_server(self.host_root)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.backend = HttpSandboxBackend(
            url=self.base_url,
            workdir="/workspace",
            local_root=self.host_root,
        )
        install_backend(self.backend)

    def tearDown(self):
        reset_active_backend()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_run_shell_via_http(self):
        out = run_shell(
            "echo REMOTE_OK",
            confirm_gate=None,
            workspace_root=self.host_root,
        )
        self.assertIn("REMOTE_OK", out)

    def test_read_and_update_remote_file(self):
        text = read_file("a.txt")
        self.assertIn("hello", text)
        msg = update_file("a.txt", "hello", "world")
        self.assertIn("Updated", msg)
        self.assertEqual((self.host_root / "a.txt").read_text(), "world\n")

    def test_write_remote_file(self):
        msg = write_file("b.txt", "new\n", confirm_gate=None, confirm_overwrite=False)
        self.assertIn("Wrote", msg)
        self.assertEqual((self.host_root / "b.txt").read_text(), "new\n")

    def test_task_eval_session(self):
        session = TaskEvalSession(self.backend, check_cmd="test -f a.txt")
        patch = session.extract_patch()
        self.assertIsInstance(patch, str)
        first = session.evaluate()
        self.assertEqual(first.tests_failed, 0)
        session.continue_task()
        (self.host_root / "a.txt").write_text("broken\n")
        second = session.evaluate()
        self.assertEqual(second.tests_failed, 0)  # file still exists
        final = session.done("done", 2)
        self.assertEqual(final.attempt, 2)

    def test_build_backend_from_url(self):
        reset_active_backend()
        backend = build_backend({}, self.host_root, sandbox_url=self.base_url)
        self.assertIsInstance(backend, HttpSandboxBackend)
        self.assertTrue(backend.preflight().ok)


if __name__ == "__main__":
    unittest.main()
