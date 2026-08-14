"""Tests for authored workflow graphs: parse, validate, runner, persist."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.graph import (
    GraphError,
    GraphRun,
    load_graph,
    parse_graph,
    run_graph,
)
from lmloop.loop import UntilRun


def _cfg(**kwargs) -> dict:
    out = {
        "graph_max_steps": 24,
        "graph_mine": False,
        "until_max_steps": 12,
        "until_mine": False,
        "confirm_shell": False,
        "shell_timeout_s": 5,
        "context_reserve": 2048,
    }
    out.update(kwargs)
    return out


COMPANY = """
# company — specialized roles with gates

node ceo    skill ceo
node build  until --check 'pytest -q' implement the agreed change
node qa     skill qa
node mine   mine

edge ceo -> build
edge build -> qa
edge qa -> mine   on pass
edge qa -> build  on fail
edge qa -> ceo    on blocked
"""


class ParseGraphTests(unittest.TestCase):
    def test_company_nodes_and_edges(self):
        defn = parse_graph(COMPANY, "company")
        self.assertEqual(defn.start, "ceo")
        self.assertEqual([n.name for n in defn.nodes], ["ceo", "build", "qa", "mine"])
        self.assertEqual(defn.node("ceo").kind, "skill")
        self.assertEqual(defn.node("ceo").skill, "ceo")
        self.assertEqual(defn.node("build").kind, "until")
        self.assertEqual(defn.node("build").check_cmd, "pytest -q")
        self.assertEqual(defn.node("build").goal, "implement the agreed change")
        self.assertEqual(defn.node("mine").kind, "mine")
        self.assertEqual(defn.edge_for("qa", "pass").dst, "mine")
        self.assertEqual(defn.edge_for("qa", "fail").dst, "build")
        self.assertEqual(defn.edge_for("qa", "blocked").dst, "ceo")
        self.assertEqual(defn.edge_for("ceo", "pass").dst, "build")

    def test_quoted_check_on_skill(self):
        text = "node qa skill qa --check 'pytest -q' review the diff"
        defn = parse_graph(text, "t")
        node = defn.node("qa")
        self.assertEqual(node.skill, "qa")
        self.assertEqual(node.check_cmd, "pytest -q")
        self.assertEqual(node.task, "review the diff")

    def test_unknown_on_token(self):
        with self.assertRaises(GraphError) as ctx:
            parse_graph("node a skill ceo\nedge a -> a on maybe", "t")
        self.assertIn("unknown edge on", str(ctx.exception))

    def test_unknown_node_endpoint(self):
        with self.assertRaises(GraphError) as ctx:
            parse_graph("node a skill ceo\nedge a -> missing", "t")
        self.assertIn("missing", str(ctx.exception))

    def test_duplicate_name(self):
        with self.assertRaises(GraphError) as ctx:
            parse_graph("node a skill ceo\nnode a skill qa", "t")
        self.assertIn("duplicate", str(ctx.exception))

    def test_duplicate_edge_on_same_outcome(self):
        with self.assertRaises(GraphError) as ctx:
            parse_graph(
                "node a skill ceo\nedge a -> a\nedge a -> a on pass", "t",
            )
        self.assertIn("duplicate", str(ctx.exception))

    def test_empty_file(self):
        with self.assertRaises(GraphError) as ctx:
            parse_graph("# just a heading\n\n", "t")
        self.assertIn("at least one node", str(ctx.exception))

    def test_packaged_company_loads(self):
        defn = load_graph("company")
        self.assertEqual(defn.name, "company")
        self.assertEqual(defn.start, "ceo")


class GraphRunTests(unittest.TestCase):
    def test_next_step_start_and_edges(self):
        defn = parse_graph(COMPANY, "company")
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.graph.project_dir", return_value=Path(d)):
                run = GraphRun.create("company")
                self.assertEqual(run.next_step(defn), ("run", "ceo"))
                run.append("node", "pass", node="ceo")
                self.assertEqual(run.next_step(defn), ("run", "build"))
                run.append("node", "fail", node="qa")
                self.assertEqual(run.next_step(defn), ("run", "build"))
                run.append("node", "blocked", node="qa")
                self.assertEqual(run.next_step(defn), ("run", "ceo"))

    def test_missing_fail_edge_is_gate(self):
        defn = parse_graph("node a skill ceo\nedge a -> a on pass", "t")
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.graph.project_dir", return_value=Path(d)):
                run = GraphRun.create("t")
                run.append("node", "fail", node="a")
                self.assertEqual(run.next_step(defn), ("gate", "a"))
                run.append("gate", "no", node="a")
                self.assertTrue(run.is_done())
                self.assertEqual(run.next_step(defn), (None, None))


class GraphRunnerTests(unittest.TestCase):
    def _act_log(self, root: Path) -> Path:
        log = root / "sess.jsonl"
        log.write_text("")
        return log

    def _pass_act(self, root: Path):
        log = self._act_log(root)

        def fake_act(cfg, model, user_text, **kwargs):
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": user_text},
            ]
            if "independent checker" in user_text:
                messages.append({
                    "role": "assistant",
                    "content": "ok\nSTATUS: pass",
                })
            else:
                messages.append({"role": "assistant", "content": "did the skill"})
            return messages, log

        return fake_act

    def _until_pass(self):
        def fake_until(cfg, model, *, run, **kwargs):
            run.append("maker", "next", handoff="implemented")
            run.append("check", "pass", handoff="[exit code: 0]")
            run.append("done", "pass")
            return run
        return fake_until

    def test_company_pass_path(self):
        defn = parse_graph(COMPANY, "company")
        mined = {"n": 0}

        def mine(paths):
            mined["n"] += 1

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=self._pass_act(root)), \
                 patch("lmloop.graph.run_until", side_effect=self._until_pass()), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("company")
                run_graph(
                    _cfg(graph_mine=True), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    mine=mine,
                )
            self.assertTrue(run.is_done())
            nodes = [e for e in run.events if e.get("role") == "node"]
            self.assertEqual([e.get("node") for e in nodes], ["ceo", "build", "qa"])
            self.assertEqual(nodes[0]["handoff"], "did the skill")
            self.assertEqual(nodes[2]["handoff"], "did the skill")
            self.assertTrue(any(e.get("role") == "mine" for e in run.events))
            self.assertEqual(mined["n"], 1)

    def test_until_node_handoff_is_maker_summary(self):
        def fake_until(cfg, model, *, run, **kwargs):
            run.append("maker", "next", handoff="implemented the feature")
            run.append("eval", "pass", handoff="looks good\nSTATUS: pass")
            run.append("done", "pass")
            return run

        text = (
            "node build until implement it\n"
            "node qa skill qa\n"
            "edge build -> qa"
        )
        defn = parse_graph(text, "t")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=self._pass_act(root)), \
                 patch("lmloop.graph.run_until", side_effect=fake_until), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("t")
                run_graph(
                    _cfg(), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                )
            build = [e for e in run.events if e.get("node") == "build"][0]
            self.assertEqual(build["handoff"], "implemented the feature")
            self.assertNotIn("STATUS:", build["handoff"])

    def test_skill_check_handoff_is_assistant_summary(self):
        text = "node a skill ceo --check true"
        defn = parse_graph(text, "t")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=self._pass_act(root)), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"), \
                 patch("lmloop.loop.run_shell", return_value="ok\n[exit code: 0]"):
                run = GraphRun.create("t")
                run_graph(
                    _cfg(), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    workspace_root=root,
                )
            node = [e for e in run.events if e.get("role") == "node"][0]
            self.assertEqual(node["handoff"], "did the skill")
            self.assertNotIn("exit code", node["handoff"])

    def test_skill_node_records_uses_skill_after_act(self):
        from lmloop import memory as memory_mod

        text = "node a skill ceo"
        defn = parse_graph(text, "t")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=self._pass_act(root)), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("t")
                run_graph(
                    _cfg(use_graph=True), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    workspace_root=root,
                )
                uses = [
                    e for e in memory_mod.get_graph_edges()
                    if e.get("edge_type") == "uses_skill"
                ]
            self.assertEqual(len(uses), 1)
            self.assertEqual(uses[0]["to_key"], "ceo")

    def test_qa_fail_cycles_to_build(self):
        defn = parse_graph(COMPANY, "company")
        qa_evals = {"n": 0}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            log = root / "s.jsonl"
            log.write_text("")

            def fake_act(cfg, model, user_text, **kwargs):
                messages = [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": user_text},
                ]
                if "independent checker" in user_text:
                    if "complete the qa playbook" in user_text:
                        qa_evals["n"] += 1
                        status = "pass" if qa_evals["n"] >= 2 else "fail"
                        messages.append({
                            "role": "assistant",
                            "content": f"STATUS: {status}",
                        })
                    else:
                        messages.append({
                            "role": "assistant",
                            "content": "STATUS: pass",
                        })
                else:
                    messages.append({"role": "assistant", "content": "worked"})
                return messages, log
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=fake_act), \
                 patch("lmloop.graph.run_until", side_effect=self._until_pass()), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("company")
                run_graph(
                    _cfg(), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    workspace_root=root,
                )
            nodes = [e.get("node") for e in run.events if e.get("role") == "node"]
            self.assertEqual(nodes[:5], ["ceo", "build", "qa", "build", "qa"])
            self.assertEqual(qa_evals["n"], 2)

    def test_blocked_without_edge_gate_no_stops(self):
        text = "node a skill ceo\nedge a -> a on pass"
        defn = parse_graph(text, "t")

        def fake_act(cfg, model, user_text, **kwargs):
            log = Path(d) / "s.jsonl"
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": user_text},
            ]
            if "independent checker" in user_text:
                messages.append({
                    "role": "assistant",
                    "content": "STATUS: blocked",
                })
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages, log

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "s.jsonl").write_text("")
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=fake_act), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("t")
                run_graph(
                    _cfg(), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    ask_gate=lambda _p: False,
                )
            self.assertTrue(run.is_done())
            gates = [e for e in run.events if e.get("role") == "gate"]
            self.assertEqual(gates[-1]["status"], "no")

    def test_max_steps_pauses_and_continue_resumes(self):
        text = "node a skill ceo\nnode b skill qa\nedge a -> b"
        defn = parse_graph(text, "t")

        def fake_act(cfg, model, user_text, **kwargs):
            log = Path(d) / "s.jsonl"
            messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": user_text},
            ]
            if "independent checker" in user_text:
                messages.append({
                    "role": "assistant",
                    "content": "STATUS: pass",
                })
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages, log

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "s.jsonl").write_text("")
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.graph.isolated_act", side_effect=fake_act), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("t")
                run_graph(
                    _cfg(graph_max_steps=1), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                )
                self.assertTrue(run.is_paused())
                loaded = GraphRun.load(run.path)
                run_graph(
                    _cfg(graph_max_steps=1), "m", run=loaded, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                )
            loaded = GraphRun.load(run.path)
            nodes = [e.get("node") for e in loaded.events if e.get("role") == "node"]
            self.assertEqual(nodes, ["a", "b"])
            self.assertTrue(loaded.is_done())

    def test_session_paths_includes_until_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root):
                urun = UntilRun.create("goal")
                first = root / "maker-1.jsonl"
                second = root / "maker-2.jsonl"
                first.write_text("")
                second.write_text("")
                urun.append("maker", "next", session=str(first))
                urun.append("maker", "next", session=str(second))
                run = GraphRun.create("company")
                run.append(
                    "node", "pass", node="build",
                    session=str(second), until_run=str(urun.path),
                )
                self.assertEqual(
                    [p.name for p in run.session_paths()],
                    ["maker-1.jsonl", "maker-2.jsonl"],
                )

    def test_isolated_threads_per_node(self):
        text = "node a skill ceo"
        defn = parse_graph(text, "t")
        seen = []

        def fake_act(cfg, model, messages, **kwargs):
            seen.append([m.get("role") for m in messages])
            text = messages[-1]["content"]
            if "independent checker" in text:
                messages.append({
                    "role": "assistant",
                    "content": "STATUS: pass",
                })
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.graph.agent.load_skill", return_value="skill body"):
                run = GraphRun.create("t")
                run_graph(
                    _cfg(), "m", run=run, defn=defn,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    workspace_root=root,
                )
        self.assertGreaterEqual(len(seen), 2)
        for roles in seen:
            self.assertEqual(roles, ["system", "user"])


if __name__ == "__main__":
    unittest.main()
