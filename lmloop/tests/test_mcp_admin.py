"""``lmloop mcp`` / ``/mcp`` verbs and the startup approval offer."""

import json
import unittest
from unittest.mock import patch

from test_mcp import ECHO_SERVER, McpSandbox

from lmloop.entry import cli
from lmloop.entry.completion import zsh_completion_script
from lmloop.mcp.admin import run_mcp_words
from lmloop.mcp.config import SCOPE_PROJECT, add_server, read_scope_file
from lmloop.ui import Console


class AdminTests(McpSandbox):
    def mcp(self, *words):
        return run_mcp_words(list(words), self.workspace, self.manager)

    def test_empty_list_explains_how_to_add(self):
        report = self.mcp()
        self.assertEqual(report.code, 0)
        self.assertIn("No MCP servers", report.out[0])

    def test_add_stdio_then_list_tools_and_get(self):
        report = self.mcp("add", "-e", "ECHO_TAG=z", "echo", "--", "python3", str(ECHO_SERVER))
        self.assertEqual(report.code, 0, report.err)
        entry = read_scope_file(self.paths.local)["mcpServers"]["echo"]
        self.assertEqual(entry, {"command": "python3", "args": [str(ECHO_SERVER)],
                                 "env": {"ECHO_TAG": "z"}})
        listed = "\n".join(self.mcp("list").out)
        self.assertIn("echo [local · stdio] not connected", listed)
        tools_out = "\n".join(self.mcp("tools", "echo").out)
        self.assertIn("create_note [asks]", tools_out)
        self.assertIn("connected · 5 tools", "\n".join(self.mcp("list").out))
        got = "\n".join(self.mcp("get", "echo").out)
        self.assertIn("••••", got)
        self.assertNotIn('"z"', got)

    def test_add_http_with_header_redacts_secrets_but_shows_var_refs(self):
        report = self.mcp("add", "-s", "user", "-H", "Authorization: Bearer ${TOK}",
                          "-H", "X-Key: raw", "web", "https://mcp.example.com/mcp")
        self.assertEqual(report.code, 0, report.err)
        entry = read_scope_file(self.paths.user)["mcpServers"]["web"]
        self.assertEqual(entry["type"], "http")
        got = json.loads("\n".join(self.mcp("get", "web").out[1:]))
        self.assertEqual(got["headers"]["X-Key"], "••••")
        self.assertEqual(got["headers"]["Authorization"], "••••")
        self.mcp("add-json", "-s", "user", "w2",
                 json.dumps({"type": "http", "url": "https://mcp.example.com/mcp",
                             "headers": {"Authorization": "${TOK}"}}))
        got = json.loads("\n".join(self.mcp("get", "w2").out[1:]))
        self.assertEqual(got["headers"]["Authorization"], "${TOK}")
        skipped = "\n".join(self.mcp("list").out)
        self.assertIn("missing environment variable(s): TOK", skipped)

    def test_add_errors(self):
        cases = [
            ("add",),
            ("add", "x"),
            ("add", "-s", "galaxy", "x", "--", "cmd"),
            ("add", "-e", "NOEQUALS", "x", "--", "cmd"),
            ("add", "bad name", "--", "cmd"),
            ("add-json", "x", "{nope"),
            ("frobnicate",),
            ("get", "nobody"),
            ("remove", "nobody"),
        ]
        for words in cases:
            with self.subTest(words=words):
                self.assertEqual(self.mcp(*words).code, 1)

    def test_project_add_is_preapproved_and_approve_flow(self):
        self.mcp("add", "-s", "project", "mine", "--", "python3", str(ECHO_SERVER))
        self.assertIn("mine", self.manager.config.servers)
        add_server(self.paths, SCOPE_PROJECT, "theirs", {"command": "python3"})
        self.manager.invalidate()
        listed = "\n".join(self.mcp("list").out)
        self.assertIn("Waiting for approval", listed)
        self.assertIn("pass names or --all", "\n".join(self.mcp("approve").out))
        self.assertEqual(self.mcp("approve", "ghost").code, 1)
        self.assertEqual(self.mcp("approve", "theirs").out, ["Approved: theirs"])
        self.assertIn("theirs", self.manager.config.servers)
        self.assertEqual(self.mcp("approve", "--all").out, ["Nothing waiting for approval."])

    def test_disable_enable_remove_reconnect_policy(self):
        self.mcp("add", "echo", "--", "python3", str(ECHO_SERVER))
        self.mcp("disable", "echo")
        self.assertIn("echo [disabled]", "\n".join(self.mcp("list").out))
        self.mcp("enable", "echo")
        self.assertIn("echo", self.manager.config.servers)
        self.assertIn("echo", self.mcp("reconnect").out[0])
        self.assertIn("MCP policy", self.mcp("policy").out[0])
        self.assertIn("Removed echo", self.mcp("rm", "echo").out[0])
        self.assertEqual(self.manager.config.servers, {})

    def test_cli_handler_and_completion(self):
        console = Console(False)
        with patch("lmloop.entry.cli.Path.cwd", return_value=self.workspace), \
             patch("lmloop.mcp.admin.session_manager", return_value=self.manager), \
             patch.object(console, "info") as info:
            code = cli.cmd_mcp({}, ["list"], console)
        self.assertEqual(code, 0)
        self.assertIn("No MCP servers", info.call_args[0][0])
        self.assertIn("mcp)", zsh_completion_script())
        self.assertIn("add-json", zsh_completion_script())


if __name__ == "__main__":
    unittest.main()
