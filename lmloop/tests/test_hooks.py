"""PreToolUse / PostToolUse hooks from the user's state dir (never the repo)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.capabilities import tools
from lmloop.capabilities.tools import ToolDef, ToolResult
from lmloop.core.config import DEFAULTS
from lmloop.verify.hooks import HookRunner, hook_files, parse_hooks


def script(body: str) -> str:
    return f"{sys.executable} -c {json.dumps(body)}"


class HookSandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.workspace = base / "ws"
        self.workspace.mkdir()
        self.state_root = base / "state"
        self.events: list = []
        for each in (
            patch("lmloop.core.config.STATE_ROOT", self.state_root),
            patch("lmloop.core.usage.record",
                  lambda feature, **detail: self.events.append((feature, detail))),
        ):
            each.start()
            self.addCleanup(each.stop)

    def write_hooks(self, data: dict, project: bool = False) -> Path:
        path = hook_files(self.workspace)[1 if project else 0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def runner(self) -> HookRunner:
        return HookRunner.load(self.workspace)


class HookTests(HookSandbox):
    def test_files_live_in_state_root_only(self):
        files = hook_files(self.workspace)
        self.assertTrue(all(str(p).startswith(str(self.state_root)) for p in files))
        (self.workspace / "hooks.json").write_text(
            json.dumps({"PreToolUse": [{"command": "false"}]}), encoding="utf-8")
        self.assertEqual(self.runner().hooks, [])
        self.assertIn("Hooks: none", self.runner().describe_lines()[0])

    def test_parse_reports_bad_rows(self):
        hooks, errors = parse_hooks({"PreToolUse": [{"matcher": "x"}, {"command": "true"}]}, "f")
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].matcher, "*")
        self.assertIn("needs a command", errors[0])
        self.assertEqual(parse_hooks([], "f")[1], ["f: expected a JSON object"])

    def test_pre_hook_blocks_with_message_and_records(self):
        self.write_hooks({"PreToolUse": [{
            "matcher": "run_*",
            "command": script("import json,sys; d=json.load(sys.stdin); "
                              "print(json.dumps({'continue': False, "
                              "'message': 'no ' + d['args']['command']}))"),
        }]})
        row = self.runner().wrap(ToolDef("run_shell", "", {}, [], lambda command: "ran"))
        self.assertEqual(row.impl(command="ls"), "DENIED: a PreToolUse hook blocked run_shell: no ls")
        self.assertEqual(self.events[-1], ("hook.blocked", {"tool": "run_shell"}))

    def test_nonzero_exit_blocks_and_silent_hook_continues(self):
        self.write_hooks({"PreToolUse": [
            {"matcher": "read_file", "command": script("pass")},
            {"matcher": "write_file", "command": script("import sys; sys.exit(3)")},
        ]}, project=True)
        runner = self.runner()
        self.assertEqual(runner.wrap(ToolDef("read_file", "", {}, [], lambda: "ok")).impl(), "ok")
        out = runner.wrap(ToolDef("write_file", "", {}, [], lambda: "ok")).impl()
        self.assertIn("exited 3", out)

    def test_post_hook_annotates_text_and_media_results(self):
        self.write_hooks({"PostToolUse": [{
            "matcher": "*",
            "command": script("import json,sys; d=json.load(sys.stdin); "
                              "print(json.dumps({'message': 'saw ' + d['result']}))"),
        }]})
        runner = self.runner()
        self.assertEqual(runner.wrap(ToolDef("t", "", {}, [], lambda: "r")).impl(), "r\n[hook] saw r")
        media = runner.wrap(ToolDef("m", "", {}, [], lambda: ToolResult("img", []))).impl()
        self.assertEqual(media.text, "img\n[hook] saw img")

    def test_unmatched_row_is_unchanged_and_mcp_call_matches_eager_name(self):
        self.write_hooks({"PreToolUse": [{"matcher": "mcp__gh__create_*",
                                          "command": script("import sys; sys.exit(1)")}]})
        runner = self.runner()
        plain = ToolDef("list_dir", "", {}, [], lambda: "ok")
        self.assertIs(runner.wrap(plain), plain)
        call = runner.wrap(ToolDef("mcp_call", "", {}, [], lambda **kw: "called"))
        self.assertIn("DENIED", call.impl(server="gh", tool="create_issue", arguments={}))
        self.assertEqual(call.impl(server="gh", tool="get_issue", arguments={}), "called")

    def test_build_tools_wraps_rows(self):
        self.write_hooks({"PreToolUse": [{"matcher": "list_dir",
                                          "command": script("import sys; sys.exit(2)")}]})
        _specs, impls = tools.build_tools({**DEFAULTS}, workspace_root=self.workspace)
        out = tools.dispatch(impls, "list_dir", "{}")
        self.assertIn("PreToolUse hook blocked list_dir", str(out))


if __name__ == "__main__":
    unittest.main()
