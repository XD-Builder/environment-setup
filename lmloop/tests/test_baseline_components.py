"""Baseline contracts and scripted-model probes, one scenario per component.

Direct tests lock pure modules (checks, exec, snapshot, steer, evals, CLI).
Agent tests drive ``lmloop`` against the local scripted server so a loop,
tool, or prompt regression fails before a holistic scenario does.

Set ``LMLOOP_SKIP_AGENT_E2E=1`` to skip the scripted probes (the unit CI job).
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
    write_config,
)


def _workspace(tmp: str, files: dict[str, str] | None = None) -> Path:
    root = Path(tmp) / "fixture"
    root.mkdir()
    for rel, text in (files or {"README.md": "fixture\n"}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class ComponentContractTests(unittest.TestCase):
    """No model server. These are the floors each leaf has to clear."""

    @baseline("cli.skills", "packaged skills are listed")
    def test_skills_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp)
            proc = run_lmloop(["skills"], home=home, workspace=workspace)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in ("investigate", "review", "qa", "ceo"):
            self.assertIn(name, proc.stdout)

    @baseline("cli.completion", "zsh completion script")
    def test_completion_zsh(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp)
            proc = run_lmloop(["completion", "zsh"], home=home, workspace=workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("#compdef lmloop", proc.stdout)
        self.assertIn("until", proc.stdout)
        self.assertIn("eval", proc.stdout)

    @baseline("cli.config", "set and get round-trip")
    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp)
            home.mkdir()
            write_config(home, {"color": False})
            set_proc = run_lmloop(
                ["config", "set", "context_length", "4096"],
                home=home, workspace=workspace,
            )
            get_proc = run_lmloop(
                ["config", "get", "context_length"],
                home=home, workspace=workspace,
            )
        self.assertEqual(set_proc.returncode, 0, set_proc.stderr)
        self.assertEqual(get_proc.returncode, 0, get_proc.stderr)
        self.assertIn("4096", get_proc.stdout)

    @baseline("cli.eval", "eval --json returns a report")
    def test_eval_empty_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = _workspace(tmp)
            proc = run_lmloop(["eval", "--json"], home=home, workspace=workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("gaps", payload)
        self.assertIn("flow", payload)
        self.assertGreaterEqual(payload["events"], 1)
        self.assertGreaterEqual(payload["features"].get("cli.command", 0), 1)

    @baseline("checks", "pytest.ini becomes an authoritative check")
    def test_infer_plan_from_pytest_ini(self):
        from lmloop.checks import infer_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pytest.ini").write_text("[pytest]\n")
            plan = infer_plan("ship the change", root)
        cmds = [item.cmd for item in plan.checks]
        self.assertTrue(any("pytest" in cmd for cmd in cmds), cmds)

    @baseline("exec.local", "cwd, stdout, and missing binary")
    def test_local_backend(self):
        from lmloop.exec import LocalBackend

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = LocalBackend()
            ok = backend.run(
                f"{sys.executable} -c \"open('marker.txt','w').write('4242')\"",
                cwd=root,
                timeout_s=30,
            )
            missing = backend.run(
                "lmloop-binary-not-installed",
                cwd=root,
                timeout_s=30,
            )
            self.assertEqual(ok.exit_code, 0, ok.stdout + ok.stderr)
            self.assertEqual(ok.backend, "local")
            self.assertEqual((root / "marker.txt").read_text(), "4242")
            self.assertEqual(missing.exit_code, 127)
            self.assertTrue(missing.spawn_error)

    @baseline("snapshot", "dirty tree is stored under refs/lmloop")
    def test_snapshot_ref(self):
        from lmloop.snapshot import take_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fixture"
            root.mkdir()
            (root / "a.txt").write_text("one\n")
            init_git(root)
            clean = take_snapshot(
                {"autonomous_snapshot": "git"}, root, run_label="base", step=1,
            )
            (root / "a.txt").write_text("two\n")
            dirty = take_snapshot(
                {"autonomous_snapshot": "git"}, root, run_label="base", step=2,
            )
        self.assertEqual(clean.ref, "HEAD", clean.note)
        self.assertTrue(dirty.ref.startswith("refs/lmloop/"), dirty)

    @baseline("steer.clock", "frozen clock block")
    def test_clock_block(self):
        from datetime import datetime, timezone

        from lmloop.steer import clock_block

        text = clock_block(datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc))
        self.assertIn("## Clock", text)
        self.assertIn("2026-10-11", text)

    @baseline("evals", "healthy fixture has no action gaps")
    def test_healthy_usage_fixture(self):
        from lmloop import evals

        path = Path(__file__).resolve().parent / "fixtures" / "usage_healthy.jsonl"
        events = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        gaps = evals.find_gaps(evals.aggregate(events))
        action = [gap.id for gap in gaps if gap.severity == "action"]
        self.assertEqual(action, [])


@unittest.skipIf(SKIP_AGENT_E2E, "LMLOOP_SKIP_AGENT_E2E=1")
class ComponentAgentTests(unittest.TestCase):
    """One scripted conversation per model-facing component."""

    def _run(self, args, steps, files=None, config=None, timeout=60):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        workspace = _workspace(tmp.name, files)
        run = run_agent(
            args, home=home, workspace=workspace, steps=steps,
            config=config, timeout=timeout,
        )
        assert_agent_ok(self, run)
        return run

    @baseline("chat", "one-shot reply and clock injection")
    def test_chat_reply_includes_clock(self):
        run = self._run(
            ["reply with the scripted sentence"],
            [text_step("hello from scripted model")],
        )
        self.assertIn("hello from scripted model", run.out)
        blob = run.script.blob()
        self.assertIn("## Clock", blob)
        self.assertIn("scripted-local", blob)

    @baseline("tools.read_file", "read_file returns file bytes to the model")
    def test_read_file(self):
        run = self._run(
            ["read seed.txt"],
            [
                tool_step("read_file", {"path": "seed.txt"}),
                text_step("seed contains SEED_TOKEN"),
            ],
            files={"seed.txt": "SEED_TOKEN\n"},
        )
        self.assertIn("SEED_TOKEN", run.script.blob())
        self.assertIn("seed contains SEED_TOKEN", run.out)

    @baseline("tools.write_file", "write_file creates a workspace file")
    def test_write_file(self):
        run = self._run(
            ["create out.txt"],
            [
                tool_step("write_file", {"path": "out.txt", "content": "hello-baseline"}),
                text_step("wrote out.txt"),
            ],
        )
        self.assertEqual((run.workspace / "out.txt").read_text(), "hello-baseline")
        self.assertIn("wrote out.txt", run.out)

    @baseline("tools.update_file", "update_file replaces an exact snippet")
    def test_update_file(self):
        run = self._run(
            ["edit note.txt"],
            [
                tool_step("update_file", {
                    "path": "note.txt",
                    "old_string": "beta",
                    "new_string": "gamma",
                }),
                text_step("updated note.txt"),
            ],
            files={"note.txt": "alpha\nbeta\n"},
        )
        self.assertEqual((run.workspace / "note.txt").read_text(), "alpha\ngamma\n")

    @baseline("tools.shell", "run_shell stdout returns to the model")
    def test_run_shell(self):
        cmd = f"{sys.executable} -c \"print(4242)\""
        run = self._run(
            ["run the printer"],
            [
                tool_step("run_shell", {"command": cmd}),
                text_step("shell printed 4242"),
            ],
        )
        self.assertIn("4242", run.script.blob())
        self.assertIn("[exit code: 0]", run.script.blob())
        self.assertIn("shell printed 4242", run.out)

    @baseline("tools.search_files", "search hits the workspace token")
    def test_search_files(self):
        run = self._run(
            ["search the tree"],
            [
                tool_step("search_files", {"pattern": "SEARCH_TOKEN_77"}),
                text_step("found SEARCH_TOKEN_77"),
            ],
            files={"hay.txt": "prefix SEARCH_TOKEN_77 suffix\n"},
        )
        self.assertIn("hay.txt", run.script.blob())
        self.assertIn("SEARCH_TOKEN_77", run.script.blob())

    @baseline("tools.find_files", "find_files returns the project path")
    def test_find_files(self):
        run = self._run(
            ["find the marker module"],
            [
                tool_step("find_files", {"pattern": "unique_marker"}),
                text_step("found unique_marker.py"),
            ],
            files={"unique_marker.py": "x = 1\n"},
        )
        self.assertIn("unique_marker.py", run.script.blob())

    @baseline("tools.current_time", "current_time returns a utc stamp")
    def test_current_time(self):
        run = self._run(
            ["what time is it"],
            [
                tool_step("current_time", {}),
                text_step("clock tool answered"),
            ],
        )
        self.assertIn("utc:", run.script.blob())

    @baseline("memory.remember", "remember writes learnings and graph nodes")
    def test_remember_and_graph(self):
        run = self._run(
            ["remember the port rule"],
            [
                tool_step("remember", {
                    "insight": "published ports bind to loopback",
                    "type": "operational",
                    "key": "loopback-ports",
                    "confidence": 8,
                }),
                text_step("Prior learning applied: loopback-ports"),
            ],
            config={"use_graph": True},
        )
        learnings = (project_dir(run.home, run.workspace) / "learnings.jsonl").read_text()
        nodes = (project_dir(run.home, run.workspace) / "graph_nodes.jsonl").read_text()
        self.assertIn("loopback-ports", learnings)
        self.assertIn("loopback-ports", nodes)
        self.assertIn("Prior learning applied: loopback-ports", run.out)

    @baseline("skills.review", "review playbook is the user turn")
    def test_skill_review(self):
        run = self._run(
            ["skill", "review", "look at the branch"],
            [text_step("No production bugs in the scoped diff.")],
        )
        self.assertIn("pre-landing code review", run.script.blob())
        self.assertIn("No production bugs in the scoped diff.", run.out)

    @baseline("web.fetch_url", "fetch_url reads the local fixture page")
    def test_fetch_url(self):
        def steps(server):
            return [
                tool_step("fetch_url", {"url": server.page_url}),
                text_step("fetched PAGE_TOKEN"),
            ]

        run = self._run(["fetch the fixture page"], steps)
        self.assertIn("PAGE_TOKEN", run.script.blob())
        self.assertIn("fetched PAGE_TOKEN", run.out)

    @baseline("until", "typed check goes fail then pass")
    def test_until_check(self):
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
            run = run_agent(
                ["until", "--check", check, "set bug.txt to good"],
                home=home, workspace=workspace, steps=steps, timeout=90,
            )
            assert_agent_ok(self, run)
            log = read_jsonl_tree(project_dir(home, workspace) / "until")
            self.assertEqual((workspace / "bug.txt").read_text().strip(), "good")
        self.assertIn('"role": "done"', log)
        self.assertIn('"status": "pass"', log)


if __name__ == "__main__":
    unittest.main()
