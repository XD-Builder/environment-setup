"""The ``task`` tool: a nested act() whose answer returns as one tool result."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import agent
from lmloop.capabilities import tools
from lmloop.core.config import DEFAULTS

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        (self.workspace / "notes.txt").write_text("secret plan\n", encoding="utf-8")
        self.cfg = {**DEFAULTS, "stream": False, "confirm_shell": False, "max_rounds": 6,
                    "mcp_enabled": False}
        self.child_tools: list = []
        for each in (
            mock.patch("lmloop.model.display._rich_live_available", return_value=False),
            mock.patch("lmloop.core.config.STATE_ROOT", self.workspace / "state"),
        ):
            each.start()
            self.addCleanup(each.stop)

    def fake_chat(self, _cfg, _model, messages, tool_specs, **_kwargs):
        names = {s["function"]["name"] for s in tool_specs or []}
        is_child = messages[-1].get("content") == "find the plan" or (
            len(messages) > 1 and messages[1].get("content") == "find the plan")
        if is_child:
            self.child_tools.append(names)
            if messages[-1]["role"] == "user":
                return call("read_file", {"path": "notes.txt"}, "k1"), USAGE
            return {"role": "assistant", "content": "the plan is secret"}, USAGE
        if messages[-1]["role"] == "user":
            return call(tools.TOOL_TASK, {"description": "look", "prompt": "find the plan",
                                          "tools": "all"}), USAGE
        return {"role": "assistant", "content": "parent done"}, USAGE

    def run_parent(self, **kwargs):
        stats: dict = {}
        statuses: list = []
        tool_lines: list = []
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        with mock.patch.object(agent, "_chat", side_effect=self.fake_chat):
            agent.act(self.cfg, "m", messages, echo=lambda *_a: None, echo_delta=False,
                      echo_tool=lambda name, *_a, **_k: tool_lines.append(name),
                      echo_status=statuses.append, stats=stats,
                      workspace_root=self.workspace, **kwargs)
        return messages, stats, statuses, tool_lines

    def test_child_answer_returns_and_tokens_roll_up(self):
        messages, stats, statuses, tool_lines = self.run_parent()
        result = next(m for m in messages if m.get("role") == "tool")
        self.assertIn("the plan is secret", result["content"])
        self.assertEqual(messages[-1]["content"], "parent done")
        self.assertEqual(stats["prompt_tokens"], 40)
        self.assertIn("↳ read_file", tool_lines)
        self.assertTrue(any("task" in s and "look" in s for s in statuses))

    def test_child_has_no_task_tool_and_inherits_read_only(self):
        self.run_parent(readonly=True)
        self.assertTrue(self.child_tools)
        for names in self.child_tools:
            self.assertNotIn(tools.TOOL_TASK, names)
            self.assertNotIn("write_file", names)

    def test_task_row_absent_without_runner(self):
        specs, _ = tools.build_tools(self.cfg, workspace_root=self.workspace)
        self.assertNotIn(tools.TOOL_TASK, {s["function"]["name"] for s in specs})
        specs, _ = tools.build_tools(self.cfg, workspace_root=self.workspace,
                                     subagent=lambda *_a: "x")
        self.assertIn(tools.TOOL_TASK, {s["function"]["name"] for s in specs})


class AccumulateUsageTests(unittest.TestCase):
    def test_empty_dict_accumulates(self):
        stats: dict = {}
        agent._accumulate_usage(stats, USAGE)
        self.assertEqual(stats["total_tokens"], 15)
        agent._accumulate_usage(None, USAGE)


if __name__ == "__main__":
    unittest.main()
