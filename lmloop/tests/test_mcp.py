"""MCP client: config scopes, policy, stdio and HTTP transports, tool rows."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lmloop.capabilities import tools
from lmloop.core.config import DEFAULTS
from lmloop.mcp import session as mcp_session
from lmloop.mcp.client import McpClient, McpError
from lmloop.mcp.config import (
    ACTION_ALLOW,
    ACTION_ASK,
    ACTION_DENY,
    SCOPE_LOCAL,
    SCOPE_PROJECT,
    SCOPE_USER,
    McpConfigError,
    McpPaths,
    McpPolicy,
    add_server,
    approve_servers,
    expand_vars,
    load_mcp_config,
    remove_server,
    resolve_entry,
    set_disabled,
    tool_is_read_only,
    validate_entry,
)
from lmloop.mcp.session import McpSessionManager
from lmloop.mcp.tooldefs import (
    MCP_CALL,
    MCP_FIND,
    content_text,
    eager_name,
    mcp_catalog_block,
    mcp_tool_defs,
    schema_summary,
)

ECHO_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def echo_entry(**env) -> dict:
    entry = {"command": sys.executable, "args": [str(ECHO_SERVER)]}
    if env:
        entry["env"] = env
    return entry


class McpSandbox(unittest.TestCase):
    """Temp workspace + state root, a fresh manager, and recorded usage events."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.workspace = base / "ws"
        self.workspace.mkdir()
        self.state_root = base / "state"
        self.manager = McpSessionManager()
        self.events: list = []
        patches = [
            patch("lmloop.core.config.STATE_ROOT", self.state_root),
            patch.object(mcp_session, "_MANAGER", self.manager),
            patch("lmloop.core.usage.record",
                  lambda feature, **detail: self.events.append((feature, detail))),
        ]
        for each in patches:
            each.start()
            self.addCleanup(each.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.manager.close_all)

    @property
    def paths(self) -> McpPaths:
        return McpPaths.for_workspace(self.workspace)

    def cfg(self, **over) -> dict:
        return {**DEFAULTS, **over}

    def add_echo(self, name="echo", scope=SCOPE_LOCAL, **env):
        add_server(self.paths, scope, name, echo_entry(**env))
        self.manager.invalidate()
        return self.manager.configure(self.workspace)

    def rows(self, gate=None, readonly=False, **cfg) -> dict:
        defs = mcp_tool_defs(self.cfg(**cfg), confirm_gate=gate,
                             workspace_root=self.workspace, readonly=readonly)
        return {d.name: d for d in defs}


class ConfigTests(McpSandbox):
    def test_expand_vars_defaults_and_missing(self):
        self.assertEqual(expand_vars("a${X}b", {"X": "1"}), ("a1b", []))
        self.assertEqual(expand_vars("${X:-dflt}", {}), ("dflt", []))
        self.assertEqual(expand_vars("${X:-dflt}", {"X": ""}), ("dflt", []))
        self.assertEqual(expand_vars("t ${NOPE}", {}), ("t ", ["NOPE"]))
        self.assertEqual(expand_vars("plain $HOME", {}), ("plain $HOME", []))

    def test_validate_entry_shapes(self):
        cases = [
            ("ok", {"command": "x"}, None),
            ("bad name!", {"command": "x"}, "server name"),
            ("workspace", {"command": "x"}, "reserved"),
            ("s", {"type": "sse", "url": "http://x"}, "SSE"),
            ("s", {"url": "http://x"}, '"type": "http"'),
            ("s", {"type": "http", "url": "http://x", "headers": {"a": 1}}, "headers"),
            ("s", {"args": ["a"]}, "command"),
            ("s", {"command": "x", "args": "a"}, "args"),
        ]
        for name, entry, want in cases:
            with self.subTest(name=name, entry=entry):
                err = validate_entry(name, entry)
                if want is None:
                    self.assertIsNone(err)
                else:
                    self.assertIn(want, err or "")

    def test_resolve_skips_server_with_missing_secret(self):
        spec, reason = resolve_entry(
            "gh", {"type": "http", "url": "https://mcp.example.com/mcp",
                   "headers": {"Authorization": "Bearer ${TOKEN}"}}, SCOPE_USER, env={})
        self.assertIsNone(spec)
        self.assertIn("TOKEN", reason)
        spec, _ = resolve_entry(
            "gh", {"type": "http", "url": "https://mcp.example.com/mcp",
                   "headers": {"Authorization": "Bearer ${TOKEN}"}}, SCOPE_USER,
            env={"TOKEN": "t"})
        self.assertEqual(dict(spec.headers)["Authorization"], "Bearer t")
        self.assertNotIn("Bearer", spec.describe())

    def test_scope_precedence_local_over_project_over_user(self):
        add_server(self.paths, SCOPE_USER, "a", {"command": "user"})
        add_server(self.paths, SCOPE_PROJECT, "a", {"command": "project"})
        add_server(self.paths, SCOPE_LOCAL, "a", {"command": "local"})
        cfg = load_mcp_config(self.paths)
        self.assertEqual(cfg.servers["a"].command, "local")
        self.assertEqual(cfg.servers["a"].scope, SCOPE_LOCAL)

    def test_project_servers_need_approval_per_fingerprint(self):
        add_server(self.paths, SCOPE_PROJECT, "p", {"command": "one"})
        cfg = load_mcp_config(self.paths)
        self.assertNotIn("p", cfg.servers)
        self.assertEqual([s.name for s in cfg.unapproved], ["p"])
        approve_servers(self.paths, cfg.unapproved)
        self.assertIn("p", load_mcp_config(self.paths).servers)
        add_server(self.paths, SCOPE_PROJECT, "p", {"command": "two"})
        self.assertNotIn("p", load_mcp_config(self.paths).servers)

    def test_disable_and_remove(self):
        add_server(self.paths, SCOPE_USER, "a", {"command": "x"})
        set_disabled(self.paths, "a", True)
        cfg = load_mcp_config(self.paths)
        self.assertEqual(cfg.disabled, ["a"])
        set_disabled(self.paths, "a", False)
        self.assertIn("a", load_mcp_config(self.paths).servers)
        self.assertEqual(len(remove_server(self.paths, "a")), 1)
        self.assertEqual(load_mcp_config(self.paths).servers, {})

    def test_bad_scope_file_is_reported_not_raised(self):
        self.paths.project.write_text("{nope", encoding="utf-8")
        cfg = load_mcp_config(self.paths)
        self.assertEqual(cfg.skipped[0][0], SCOPE_PROJECT)
        with self.assertRaises(McpConfigError):
            add_server(self.paths, SCOPE_LOCAL, "bad name", {"command": "x"})

    def test_manager_reloads_on_same_tick_rewrite(self):
        self.add_echo("a")
        self.assertEqual(list(self.manager.config.servers), ["a"])
        add_server(self.paths, SCOPE_LOCAL, "bb", {"command": "y"})
        self.assertEqual(sorted(self.manager.configure(self.workspace).servers), ["a", "bb"])


class PolicyTests(unittest.TestCase):
    def test_read_only_detection(self):
        cases = [
            ({"name": "get_issue"}, True),
            ({"name": "list-repos"}, True),
            ({"name": "create_issue"}, False),
            ({"name": "delete", "annotations": {"readOnlyHint": True}}, True),
            ({"name": "get", "annotations": {"readOnlyHint": False}}, False),
            ({"name": "get", "annotations": {"destructiveHint": True}}, False),
        ]
        for tool, want in cases:
            with self.subTest(tool=tool):
                self.assertEqual(tool_is_read_only(tool), want)

    def test_rules_first_match_wins_then_default(self):
        policy = McpPolicy.from_dict({
            "default": "deny",
            "rules": [
                {"server": "gh", "tools": ["create_*"], "action": "allow"},
                {"server": "*", "tools": "get_secret", "action": "deny"},
            ],
        })
        self.assertEqual(policy.decide("gh", "create_issue", False), ACTION_ALLOW)
        self.assertEqual(policy.decide("x", "get_secret", True), ACTION_DENY)
        self.assertEqual(policy.decide("x", "get_thing", True), ACTION_ALLOW)
        self.assertEqual(policy.decide("x", "update", False), ACTION_DENY)

    def test_broken_policy_asks_for_mutations(self):
        policy = McpPolicy.from_dict({"default": "maybe"})
        self.assertTrue(policy.error)
        self.assertEqual(policy.decide("s", "write", False), ACTION_ASK)
        self.assertIn("unreadable", policy.describe_lines()[0])
        self.assertIn("none", McpPolicy().describe_lines()[0])


class StdioClientTests(McpSandbox):
    def connect(self, **env) -> McpClient:
        self.add_echo(**env)
        return self.manager.client("echo")

    def test_initialize_list_and_call(self):
        client = self.connect(ECHO_TAG="t9")
        self.assertEqual(client.server_info.get("name"), "echo")
        names = [t["name"] for t in client.list_tools()]
        self.assertIn("get_echo", names)
        result = client.call_tool("get_echo", {"text": "hi"})
        self.assertEqual(content_text(result)[0], "t9:hi")

    def test_paginated_tools_list(self):
        client = self.connect(MCP_ECHO_TOOL_COUNT="7", MCP_ECHO_PAGE="3")
        self.assertEqual(len(client.list_tools()), 12)

    def test_server_request_roots_is_answered(self):
        client = self.connect()
        self.manager.extra_roots = [self.state_root]
        text, _ = content_text(client.call_tool("roots", {}))
        self.assertIn(self.workspace.resolve().as_uri(), text)
        self.assertIn(self.state_root.resolve().as_uri(), text)

    def test_unknown_tool_is_mcp_error(self):
        client = self.connect()
        with self.assertRaises(McpError):
            client.call_tool("nope", {})

    def test_bad_command_reports_error_state(self):
        add_server(self.paths, SCOPE_LOCAL, "dead", {"command": sys.executable,
                                                     "args": ["-c", "pass"]})
        self.manager.invalidate()
        self.manager.configure(self.workspace)
        with self.assertRaises(McpError):
            self.manager.client("dead")
        row = next(r for r in self.manager.status_rows() if r.name == "dead")
        self.assertTrue(row.state.startswith("error:"))

    def test_entry_change_drops_live_session(self):
        first = self.connect(ECHO_TAG="a")
        self.add_echo(ECHO_TAG="b")
        second = self.manager.client("echo")
        self.assertIsNot(first, second)
        self.assertEqual(content_text(second.call_tool("get_echo", {"text": "x"}))[0], "b:x")


class _FakeHttp(BaseHTTPRequestHandler):
    mode = "json"
    seen: list = []

    def log_message(self, *args):
        pass

    def do_DELETE(self):
        self.seen.append(("DELETE", self.headers.get("Mcp-Session-Id")))
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append((body.get("method"), self.headers.get("Mcp-Session-Id"),
                          self.headers.get("Authorization")))
        if self.mode == "401":
            self.send_response(401)
            self.end_headers()
            return
        if "id" not in body:
            self.send_response(202)
            self.end_headers()
            return
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "get_weather", "description": "Weather",
                                 "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "sunny"}]}
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.mode == "sse":
            payload = (": keepalive\n\n"
                       f"event: message\ndata: {json.dumps(reply)}\n\n").encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps(reply).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Mcp-Session-Id", "sess-1")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class HttpClientTests(McpSandbox):
    def serve(self, mode: str) -> str:
        handler = type("H", (_FakeHttp,), {"mode": mode, "seen": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.handler = handler
        return f"http://127.0.0.1:{server.server_address[1]}/mcp"

    def add_http(self, url: str):
        add_server(self.paths, SCOPE_LOCAL, "web", {
            "type": "http", "url": url, "headers": {"Authorization": "Bearer ${MCP_T:-tok}"},
        })
        self.manager.invalidate()
        self.manager.configure(self.workspace)

    def test_json_and_sse_replies(self):
        for mode in ("json", "sse"):
            with self.subTest(mode=mode):
                self.add_http(self.serve(mode))
                self.manager.close_all()
                self.assertEqual([t["name"] for t in self.manager.tools("web", refresh=True)],
                                 ["get_weather"])
                result = self.manager.call("web", "get_weather", {})
                self.assertEqual(content_text(result)[0], "sunny")
                methods = [row[0] for row in self.handler.seen]
                self.assertEqual(methods[:2], ["initialize", "notifications/initialized"])
                self.assertEqual(self.handler.seen[1][1], "sess-1")
                self.assertEqual(self.handler.seen[0][2], "Bearer tok")
                self.manager.close("web")
                self.assertEqual(self.handler.seen[-1], ("DELETE", "sess-1"))

    def test_unauthorized_hint(self):
        self.add_http(self.serve("401"))
        with self.assertRaises(McpError) as ctx:
            self.manager.client("web")
        self.assertIn("Authorization", str(ctx.exception))


class ToolRowTests(McpSandbox):
    def test_no_rows_without_servers_or_when_disabled(self):
        self.assertEqual(self.rows(), {})
        self.add_echo()
        self.assertEqual(self.rows(mcp_enabled=False), {})
        self.assertEqual(set(self.rows()), {MCP_FIND, MCP_CALL})

    def test_find_lists_tools_and_marks_mutating(self):
        self.add_echo()
        out = self.rows()[MCP_FIND].impl(query="note")
        self.assertIn("echo / create_note", out)
        self.assertIn("[asks before running]", out)
        self.assertIn("unknown MCP server", self.rows()[MCP_FIND].impl(query="", server="zz"))

    def test_read_only_call_runs_without_gate_and_is_fenced(self):
        self.add_echo(ECHO_TAG="r")
        out = self.rows()[MCP_CALL].impl(server="echo", tool="get_echo",
                                         arguments={"text": "q"})
        self.assertIn("r:q", out)
        self.assertIn("MCP", out.split("\n")[0])
        self.assertEqual(self.events[-1][0], "mcp.call")
        self.assertTrue(self.events[-1][1]["ok"])

    def test_mutating_call_asks_gate(self):
        self.add_echo()
        asked: list = []
        row = self.rows(gate=lambda prompt: asked.append(prompt) or False)[MCP_CALL]
        out = row.impl(server="echo", tool="create_note", arguments={"title": "t"})
        self.assertIn("declined", out)
        self.assertTrue(asked[0].startswith(tools.GATE_MCP + "echo/create_note"))
        self.assertEqual(self.events[-1], ("mcp.denied", {"server": "echo", "reason": "user"}))
        row = self.rows(gate=lambda prompt: True)[MCP_CALL]
        self.assertIn("created t", row.impl(server="echo", tool="create_note",
                                            arguments={"title": "t"}))

    def test_no_gate_denies_and_points_at_policy(self):
        self.add_echo()
        out = self.rows()[MCP_CALL].impl(server="echo", tool="create_note", arguments={})
        self.assertIn("mcp.policy.json", out)

    def test_policy_allow_and_deny(self):
        self.add_echo()
        policy = self.workspace / ".lmloop" / "mcp.policy.json"
        policy.parent.mkdir()
        policy.write_text(json.dumps({"rules": [
            {"server": "echo", "tools": ["create_*"], "action": "allow"},
            {"server": "echo", "tools": ["get_echo"], "action": "deny"},
        ]}), encoding="utf-8")
        row = self.rows()[MCP_CALL]
        self.assertIn("created", row.impl(server="echo", tool="create_note", arguments={}))
        self.assertIn("policy blocks", row.impl(server="echo", tool="get_echo",
                                                arguments={"text": "x"}))

    def test_readonly_turn_refuses_mutations(self):
        self.add_echo()
        out = self.rows(readonly=True)[MCP_CALL].impl(server="echo", tool="create_note",
                                                      arguments={})
        self.assertTrue(out.startswith("ERROR"))

    def test_error_result_and_unknown_tool(self):
        self.add_echo()
        row = self.rows()[MCP_CALL]
        self.assertIn("reported a failure", row.impl(server="echo", tool="fail", arguments={}))
        self.assertIn("has no tool", row.impl(server="echo", tool="nope", arguments={}))
        self.assertIn("unknown MCP server", row.impl(server="zz", tool="x", arguments={}))

    def test_long_result_spills_to_file(self):
        self.add_echo()
        with patch("lmloop.mcp.tooldefs.spill_dir", return_value=self.state_root / "spill"):
            out = self.rows(max_tool_output=1000)[MCP_CALL].impl(
                server="echo", tool="big", arguments={"size": 9000})
        self.assertIn("result is 9000 chars", out)
        spilled = list((self.state_root / "spill").iterdir())
        self.assertEqual(len(spilled[0].read_text()), 9000)

    def test_eager_mode_adds_cached_tools(self):
        self.add_echo()
        self.assertEqual(set(self.rows(mcp_tool_mode="eager")), {MCP_FIND, MCP_CALL})
        self.manager.tools("echo")
        rows = self.rows(mcp_tool_mode="eager")
        name = eager_name("echo", "get_echo")
        self.assertIn(name, rows)
        self.assertIn("v1:z", rows[name].impl(text="z"))

    def test_catalog_is_cache_only(self):
        self.add_echo()
        block = mcp_catalog_block(self.cfg(), self.workspace)
        self.assertIn("tools listed on first search", block)
        self.assertEqual(self.manager.status_rows()[0].state, "not connected")
        self.manager.tools("echo")
        self.assertIn("`get_echo`", mcp_catalog_block(self.cfg(), self.workspace))
        self.assertEqual(mcp_catalog_block(self.cfg(mcp_enabled=False), self.workspace), "")

    def test_build_tools_includes_mcp_rows_and_dispatch_passes_kwargs(self):
        self.add_echo(ECHO_TAG="d")
        specs, impls = tools.build_tools(self.cfg(mcp_tool_mode="eager"),
                                         workspace_root=self.workspace)
        names = {s["function"]["name"] for s in specs}
        self.assertIn(MCP_CALL, names)
        self.manager.tools("echo")
        specs, impls = tools.build_tools(self.cfg(mcp_tool_mode="eager"),
                                         workspace_root=self.workspace)
        out = tools.dispatch(impls, eager_name("echo", "get_echo"), json.dumps({"text": "k"}))
        self.assertIn("d:k", str(out))


class HelperTests(unittest.TestCase):
    def test_schema_summary_and_eager_name(self):
        self.assertEqual(
            schema_summary({"properties": {"a": {}, "b": {}}, "required": ["a"]}), "(a*, b)")
        self.assertEqual(eager_name("my.srv", "do it"), "mcp__my_srv__do_it")

    def test_content_text_kinds(self):
        text, media = content_text({"content": [
            {"type": "text", "text": "t"},
            {"type": "resource", "resource": {"uri": "file:///x", "text": "body"}},
            {"type": "resource_link", "uri": "file:///y", "name": "y"},
            {"type": "image", "data": "aGk=", "mimeType": "image/png"},
        ], "structuredContent": {"k": 1}})
        self.assertIn("t", text)
        self.assertIn("body", text)
        self.assertIn("file:///y", text)
        self.assertEqual(len(media), 1)


if __name__ == "__main__":
    unittest.main()
