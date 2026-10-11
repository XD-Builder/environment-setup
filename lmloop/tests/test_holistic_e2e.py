"""Holistic agent paths: several components in one user-facing invocation.

These are the regression locks for flows a unit test of one module cannot
see: until plus snapshot plus usage eval, memory across processes, a graph
run, remote-shaped auth headers, and ``@path`` inlining.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from e2e_harness import (
    SKIP_AGENT_E2E,
    assert_agent_ok,
    baseline,
    init_git,
    project_dir,
    read_jsonl_tree,
    run_agent,
    run_lmloop,
    text_step,
    tool_step,
)


def _workspace(tmp: str, files: dict[str, str]) -> Path:
    root = Path(tmp) / "fixture"
    root.mkdir()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@unittest.skipIf(SKIP_AGENT_E2E, "LMLOOP_SKIP_AGENT_E2E=1")
class HolisticAgentTests(unittest.TestCase):
    @baseline("holistic.until", "fix a file, snapshot, and eval the run", kind="holistic")
    def test_until_fix_snapshot_and_eval(self):
        check = (
            f"{sys.executable} -c \"raise SystemExit(0 if "
            "open('bug.txt').read().strip()=='good' else 1)\""
        )

        def steps(_server):
            return [
                tool_step("update_file", {
                    "path": "bug.txt",
                    "old_string": "bad",
                    "new_string": "good",
                }),
                text_step("Set bug.txt to good."),
            ]

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp, {"bug.txt": "bad\n"})
            init_git(workspace)
            run = run_agent(
                ["until", "--check", check, "set bug.txt to good"],
                home=home,
                workspace=workspace,
                steps=steps,
                config={"autonomous_snapshot": "git"},
                timeout=90,
            )
            assert_agent_ok(self, run)
            log = read_jsonl_tree(project_dir(home, workspace) / "until")
            eval_proc = run_lmloop(["eval", "--json"], home=home, workspace=workspace)
            self.assertEqual((workspace / "bug.txt").read_text().strip(), "good")
        self.assertIn('"snapshot_ref":', log)
        self.assertIn('"role": "done"', log)
        self.assertEqual(eval_proc.returncode, 0, eval_proc.stderr)
        payload = json.loads(eval_proc.stdout)
        self.assertGreaterEqual(payload["features"].get("until.finish", 0), 1)
        self.assertGreaterEqual(payload["features"].get("agent.act", 0), 1)
        self.assertEqual(payload["until_outcomes"].get("pass"), 1)
        action = [gap["id"] for gap in payload["gaps"] if gap["severity"] == "action"]
        self.assertNotIn("tool-error-rate", action)

    @baseline("holistic.memory", "recall a learning in a later process", kind="holistic")
    def test_memory_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp, {"README.md": "fixture\n"})
            first = run_agent(
                ["save the port rule"],
                home=home,
                workspace=workspace,
                steps=[
                    tool_step("remember", {
                        "insight": "published ports bind to loopback",
                        "type": "operational",
                        "key": "loopback-ports",
                        "confidence": 8,
                    }),
                    text_step("Prior learning applied: loopback-ports"),
                ],
            )
            assert_agent_ok(self, first)
            second = run_agent(
                ["what do we know about ports"],
                home=home,
                workspace=workspace,
                steps=[
                    tool_step("recall_memory", {"query": "loopback"}),
                    text_step("Prior learning applied: loopback-ports"),
                ],
            )
            assert_agent_ok(self, second)
        self.assertIn("published ports bind to loopback", second.script.blob())
        self.assertIn("loopback-ports", second.out)

    @baseline("holistic.graph", "single skill node reaches done", kind="holistic")
    def test_graph_skill_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            graphs = home / "graphs"
            graphs.mkdir(parents=True)
            (graphs / "note.md").write_text("node note skill ceo\n")
            workspace = _workspace(tmp, {"README.md": "fixture\n"})
            run = run_agent(
                ["graph", "note"],
                home=home,
                workspace=workspace,
                steps=[
                    text_step("The plan holds. No file changes."),
                    text_step("The handoff cites no file changes.\nSTATUS: pass"),
                ],
                timeout=90,
            )
            assert_agent_ok(self, run)
            log = read_jsonl_tree(project_dir(home, workspace) / "graphs")
        self.assertIn("founder mode", run.script.blob())
        self.assertIn("The plan holds. No file changes.", run.out)
        self.assertIn('"role": "done"', log)
        self.assertIn('"status": "pass"', log)

    @baseline("holistic.openrouter", "bearer and ranking headers on chat", kind="holistic")
    def test_openrouter_shaped_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp, {"README.md": "fixture\n"})
            run = run_agent(
                ["say hello in one sentence"],
                home=home,
                workspace=workspace,
                steps=[text_step("Hello from the scripted endpoint.")],
                config={"api_key": "sk-or-test-key"},
                env_extra={
                    "OPENROUTER_HTTP_REFERER": "https://example.test",
                    "OPENROUTER_X_TITLE": "lmloop-ci",
                },
            )
            assert_agent_ok(self, run)
        record = run.script.requests[0]
        self.assertEqual(record["authorization"], "Bearer sk-or-test-key")
        self.assertEqual(record["http_referer"], "https://example.test")
        self.assertEqual(record["x_title"], "lmloop-ci")
        self.assertNotIn("sk-or-test-key", run.out)

    @baseline("holistic.at_path", "@path resolves and read_file returns the bytes", kind="holistic")
    def test_at_path_then_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp, {"notes.txt": "ATPATH_TOKEN_99\n"})
            run = run_agent(
                ["summarize @notes.txt"],
                home=home,
                workspace=workspace,
                steps=[
                    tool_step("read_file", {"path": "notes.txt"}),
                    text_step("The note says ATPATH_TOKEN_99."),
                ],
            )
            assert_agent_ok(self, run)
        blob = run.script.blob()
        self.assertIn("Referenced files", blob)
        self.assertIn("notes.txt", blob)
        self.assertIn("ATPATH_TOKEN_99", blob)
        self.assertIn("The note says ATPATH_TOKEN_99.", run.out)


if __name__ == "__main__":
    unittest.main()
