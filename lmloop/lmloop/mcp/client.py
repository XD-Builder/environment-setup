"""JSON-RPC 2.0 MCP client over stdio (newline-delimited) or streamable HTTP.

``McpClient`` speaks protocol ``PROTOCOL_VERSION``: initialize, paginated
``tools/list``, ``tools/call``, and the two server requests lmloop answers
(``ping``, ``roots/list``). Transports only move JSON objects; they do not
know MCP method names.
"""

import json
import os
import queue
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .. import __version__
from .config import TRANSPORT_HTTP, ServerSpec

PROTOCOL_VERSION = "2025-06-18"
CONNECT_TIMEOUT_S = 30
CALL_TIMEOUT_S = 120
CLOSE_GRACE_S = 2
MAX_TOOL_PAGES = 20
_JSON_TYPES = ("application/json",)
_SSE_TYPE = "text/event-stream"

RequestHandler = Callable[[dict], "dict | None"]


class McpError(RuntimeError):
    """A server that cannot start, answer, or complete a request."""


class StdioTransport:
    """Child process; one JSON object per line on stdin/stdout. Stderr goes to a log."""

    def __init__(self, spec: ServerSpec, workspace_root: Path, log_path: Path,
                 on_request: RequestHandler):
        env = {**os.environ, **dict(spec.env), "LMLOOP_PROJECT_DIR": str(workspace_root)}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("ab")
        self._on_request = on_request
        try:
            self.proc = subprocess.Popen(
                [spec.command, *spec.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._log, cwd=str(workspace_root), env=env, start_new_session=True,
            )
        except OSError as exc:
            self._log.close()
            raise McpError(f"could not start {spec.command}: {exc}") from exc
        self._lines: "queue.Queue[bytes | None]" = queue.Queue()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        with self.proc.stdout:
            for line in self.proc.stdout:
                self._lines.put(line)
        self._lines.put(None)

    def send(self, message: dict) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise McpError(f"server closed its input: {exc}") from exc

    def exchange(self, message: dict, timeout_s: float) -> dict:
        """Send a request and wait for the response with the same id."""
        self.send(message)
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise McpError(f"no response to {message.get('method')} in {timeout_s:.0f}s")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise McpError(f"server exited (code {self.proc.poll()}); see {self._log.name}")
            try:
                reply = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray log line on stdout
            if not isinstance(reply, dict):
                continue
            if "method" in reply:
                answer = self._on_request(reply)
                if answer is not None:
                    self.send(answer)
                continue
            if reply.get("id") == message.get("id"):
                return reply

    def close(self) -> None:
        """Close stdin (the MCP shutdown signal), then TERM / KILL the group."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                try:
                    os.killpg(self.proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
            try:
                self.proc.wait(timeout=CLOSE_GRACE_S)
                break
            except subprocess.TimeoutExpired:
                continue
        self._reader.join(timeout=CLOSE_GRACE_S)
        self._log.close()


def _sse_messages(lines) -> "list[dict]":
    """JSON objects from an SSE body (``data:`` lines joined per event)."""
    out, data = [], []

    def flush():
        if data:
            try:
                parsed = json.loads("\n".join(data))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                out.append(parsed)
            data.clear()

    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            flush()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    flush()
    return out


class HttpTransport:
    """Streamable HTTP: POST each message; the reply is JSON or an SSE stream."""

    def __init__(self, spec: ServerSpec, on_request: RequestHandler):
        self.url = spec.url
        self.headers = dict(spec.headers)
        self.session_id = ""
        self._on_request = on_request

    def _post(self, message: dict, timeout_s: float) -> "list[dict]":
        headers = {
            "Content-Type": "application/json",
            "Accept": f"{_JSON_TYPES[0]}, {_SSE_TYPE}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                if kind == _SSE_TYPE:
                    return _sse_messages(resp)
                body = resp.read()
        except urllib.error.HTTPError as exc:
            with exc:
                hint = " (set an Authorization header)" if exc.code in (401, 403) else ""
                raise McpError(f"HTTP {exc.code} from {self.url}{hint}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(f"could not reach {self.url}: {exc}") from exc
        if not body.strip():
            return []
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise McpError(f"non-JSON reply from {self.url}") from exc
        return parsed if isinstance(parsed, list) else [parsed]

    def send(self, message: dict) -> None:
        self._post(message, CONNECT_TIMEOUT_S)

    def exchange(self, message: dict, timeout_s: float) -> dict:
        for reply in self._post(message, timeout_s):
            if "method" in reply:
                answer = self._on_request(reply)
                if answer is not None:
                    self.send(answer)
            elif reply.get("id") == message.get("id"):
                return reply
        raise McpError(f"no response to {message.get('method')} from {self.url}")

    def close(self) -> None:
        if not self.session_id:
            return
        req = urllib.request.Request(
            self.url, method="DELETE",
            headers={"Mcp-Session-Id": self.session_id, **self.headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=CLOSE_GRACE_S):
                pass
        except (urllib.error.URLError, OSError):
            pass


class McpClient:
    """One initialized session with one server."""

    def __init__(self, spec: ServerSpec, transport, roots: Callable[[], list]):
        self.spec = spec
        self.transport = transport
        self._roots = roots
        self._next_id = 0
        self._lock = threading.Lock()
        self.server_info: dict = {}

    @classmethod
    def connect(cls, spec: ServerSpec, workspace_root: Path, log_dir: Path,
                roots: "Callable[[], list] | None" = None) -> "McpClient":
        roots = roots or (lambda: [workspace_root])
        holder: dict = {}

        def on_request(message: dict) -> "dict | None":
            return holder["client"].answer(message)

        if spec.transport == TRANSPORT_HTTP:
            transport = HttpTransport(spec, on_request)
        else:
            transport = StdioTransport(spec, workspace_root, log_dir / f"{spec.name}.log", on_request)
        client = cls(spec, transport, roots)
        holder["client"] = client
        try:
            client.initialize()
        except McpError:
            transport.close()
            raise
        return client

    def answer(self, message: dict) -> "dict | None":
        """Reply to a server-initiated request; notifications get no reply."""
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "ping":
            result: "dict | None" = {}
        elif method == "roots/list":
            result = {"roots": [
                {"uri": Path(p).resolve().as_uri(), "name": Path(p).name} for p in self._roots()
            ]}
        else:
            return {"jsonrpc": "2.0", "id": message["id"],
                    "error": {"code": -32601, "message": f"unsupported: {method}"}}
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def request(self, method: str, params: "dict | None" = None,
                timeout_s: float = CALL_TIMEOUT_S) -> dict:
        with self._lock:
            self._next_id += 1
            message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
            if params is not None:
                message["params"] = params
            reply = self.transport.exchange(message, timeout_s)
        if "error" in reply:
            err = reply["error"] or {}
            raise McpError(f"{method}: {err.get('message') or err}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def initialize(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "lmloop", "version": __version__},
        }, timeout_s=CONNECT_TIMEOUT_S)
        self.server_info = result.get("serverInfo") or {}
        self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> "list[dict]":
        tools: list = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        self.transport.close()
