"""REPL session controls: /rewind, /readonly, /diff, /permissions, /add-dir, !cmd, /mcp."""

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lmloop.core.config import DEFAULTS
from lmloop.entry import repl
from lmloop.entry.repl import SessionState
from lmloop.mcp import session as mcp_session
from lmloop.mcp.config import SCOPE_PROJECT, McpPaths, add_server
from lmloop.mcp.session import McpSessionManager
from lmloop.ui import Console, fresh_stats
from lmloop.ui.session_views import (
    DENIALS_MAX,
    PLAN_MODE_NOTE,
    REWIND_MAX,
    SHELL_CONTEXT_HEADER,
    SHELL_CONTEXT_MAX,
    DenialLog,
    RewindStack,
    ShellRuns,
    diff_lines,
)


def thread(*users: str) -> list:
    out = [{"role": "system", "content": "s"}]
    for text in users:
        out += [{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}]
    return out


class ViewTests(unittest.TestCase):
    def test_rewind_stack_skips_empty_threads_and_caps(self):
        stack = RewindStack()
        stack.push("/new", thread(), Path("a"))
        self.assertEqual(stack.entries, [])
        for i in range(REWIND_MAX + 3):
            stack.push("/new", thread(f"t{i}"), Path(f"s{i}"))
        self.assertEqual(len(stack.entries), REWIND_MAX)
        self.assertIn("1. before /new", stack.lines()[0])
        self.assertIsNone(stack.pop(REWIND_MAX + 1))
        self.assertEqual(stack.pop(2).messages[1]["content"], f"t{REWIND_MAX + 1}")
        self.assertEqual(len(stack.entries), REWIND_MAX - 2)

    def test_shell_runs_flush_once_and_clip(self):
        runs = ShellRuns()
        self.assertEqual(runs.take_context(), "")
        runs.add("ls", "a\nb\n")
        text = runs.take_context()
        self.assertTrue(text.startswith(SHELL_CONTEXT_HEADER))
        self.assertIn("$ ls\na\nb", text)
        self.assertEqual(runs.take_context(), "")
        runs.add("big", "x" * (SHELL_CONTEXT_MAX * 2))
        self.assertLessEqual(len(runs.take_context()),
                             SHELL_CONTEXT_MAX + len(SHELL_CONTEXT_HEADER) + 2)

    def test_denial_log_records_only_declines(self):
        log = DenialLog()
        gate = log.wrap(lambda command: command == "yes")
        self.assertTrue(gate("yes"))
        for i in range(DENIALS_MAX + 2):
            self.assertFalse(gate(f"no {i}"))
        self.assertEqual(len(log.rows), DENIALS_MAX)
        self.assertEqual(log.rows[-1], f"no {DENIALS_MAX + 1}")

    def test_diff_lines(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIn("not a git repository", diff_lines(root)[0])
            git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run([*git, "init", "-q"], cwd=root, check=True)
            (root / "a.txt").write_text("1\n")
            subprocess.run([*git, "add", "."], cwd=root, check=True)
            subprocess.run([*git, "commit", "-qm", "init"], cwd=root, check=True)
            self.assertIn("no changes", diff_lines(root)[0])
            (root / "a.txt").write_text("2\n")
            (root / "new.txt").write_text("n\n")
            text = "\n".join(diff_lines(root))
            self.assertIn("M a.txt", text)
            self.assertIn("?? new.txt", text)
            self.assertIn("a.txt | 2", text)


class ReplControlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.manager = McpSessionManager()
        for each in (
            patch("lmloop.core.config.STATE_ROOT", self.root / "state"),
            patch.object(mcp_session, "_MANAGER", self.manager),
            patch("lmloop.core.usage.record", lambda *a, **k: None),
            patch("lmloop.entry.repl.memory.new_session_log",
                  side_effect=lambda: self.root / "new.jsonl"),
            patch("lmloop.entry.repl._fresh_messages",
                  side_effect=lambda *a, **k: [{"role": "system", "content": "fresh"}]),
        ):
            each.start()
            self.addCleanup(each.stop)
        self.state = SessionState(
            cfg={**DEFAULTS}, model="m", messages=thread("first"),
            session_log=self.root / "s.jsonl", stats=fresh_stats(),
            console=Console(color=False), workspace_root=self.root,
        )

    def run_cmd(self, handler, arg="") -> str:
        with redirect_stdout(io.StringIO()) as out, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            handler(self.state, arg)
        return out.getvalue() + err.getvalue()

    def test_new_then_rewind_restores_thread(self):
        repl._cmd_new(self.state, "")
        self.assertEqual(self.state.user_turns, 0)
        out = self.run_cmd(repl._cmd_rewind)
        self.assertIn("before /new", out)
        out = self.run_cmd(repl._cmd_rewind, "1")
        self.assertIn("rewound to before /new", out)
        self.assertEqual(self.state.messages[1]["content"], "first")
        self.assertEqual(self.state.session_log, self.root / "s.jsonl")
        self.assertIn("no rewind point", self.run_cmd(repl._cmd_rewind, "3"))

    def test_rewind_of_rewind_swaps_back(self):
        repl._adopt_thread(self.state, thread("second"), log=None, replay=False)
        self.run_cmd(repl._cmd_rewind, "1")
        self.assertEqual(self.state.messages[1]["content"], "first")
        self.run_cmd(repl._cmd_rewind, "1")
        self.assertEqual(self.state.messages[1]["content"], "second")

    def test_readonly_toggles_and_turn_passes_readonly_and_note(self):
        self.run_cmd(repl._cmd_readonly)
        self.assertTrue(self.state.plan_mode)
        self.assertIn("usage", self.run_cmd(repl._cmd_readonly, "maybe"))
        captured = {}

        def fake_act(cfg, model, messages, **kwargs):
            captured.update(kwargs, content=messages[-1]["content"])
            return messages

        self.state.shell.add("ls", "a.txt")
        self.state.extra_dirs.append(self.root / "other")
        self.state.session_log.write_text("")
        with patch("lmloop.entry.repl.agent.act", side_effect=fake_act), \
             redirect_stdout(io.StringIO()):
            repl._run_turn(self.state, "plan it", lambda _c: False)
        self.assertTrue(captured["readonly"])
        self.assertTrue(captured["content"].startswith(SHELL_CONTEXT_HEADER))
        self.assertTrue(captured["content"].endswith(PLAN_MODE_NOTE))
        self.assertIn(self.root / "other", captured["extra_readable"])
        self.run_cmd(repl._cmd_readonly, "off")
        self.assertFalse(self.state.plan_mode)

    def test_bang_runs_and_queues_output(self):
        self.state.session_log.write_text("")
        with redirect_stdout(io.StringIO()) as out:
            repl._run_bang(self.state, "!echo hello-bang")
        self.assertIn("hello-bang", out.getvalue())
        self.assertIn("$ echo hello-bang", self.state.shell.take_context())

    def test_add_dir_and_permissions(self):
        other = self.root / "other"
        other.mkdir()
        self.assertIn("not a directory", self.run_cmd(repl._cmd_add_dir, "missing"))
        self.run_cmd(repl._cmd_add_dir, str(other))
        self.assertEqual(self.state.extra_dirs, [other])
        self.assertEqual(self.manager.extra_roots, [other])
        self.state.denials.rows.append("rm -rf build")
        out = self.run_cmd(repl._cmd_permissions)
        for want in ("Plan mode: off", "mcp_call", "MCP policy", "Hooks:",
                     str(other), "Recently declined", "rm -rf build"):
            self.assertIn(want, out)

    def test_mcp_command_refreshes_fresh_system_prompt(self):
        self.state.messages = [{"role": "system", "content": "old"}]
        out = self.run_cmd(repl._cmd_mcp, "list")
        self.assertIn("No MCP servers", out)
        self.assertEqual(self.state.messages[0]["content"], "fresh")
        self.assertIn("No closing quotation", self.run_cmd(repl._cmd_mcp, "add 'x"))

    def test_offer_mcp_approval(self):
        add_server(McpPaths.for_workspace(self.root), SCOPE_PROJECT, "p", {"command": "x"})
        with redirect_stdout(io.StringIO()) as out:
            repl.offer_mcp_approval(self.state, interactive=False)
        self.assertIn("lmloop mcp approve --all", out.getvalue())
        self.assertNotIn("p", self.manager.config.servers)
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            repl.offer_mcp_approval(self.state, interactive=True, ask=lambda _p: True)
        self.assertIn("p", self.manager.config.servers)


if __name__ == "__main__":
    unittest.main()
