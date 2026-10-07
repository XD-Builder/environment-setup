"""Roadmap step 4+7+9: V8 proposals, memory index, canvas, graph propose."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.checks import (
    PlannedCheck,
    ROLE_CHECK,
    TIER_ADVISORY,
    TIER_AUTHORITATIVE,
    parse_proposed_commands,
    plan_needs_proposal,
)
from lmloop import graph as graph_mod
from lmloop import knowledge_graph
from lmloop import memory_index
from lmloop import workflow


class V8ProposalTests(unittest.TestCase):
    def test_plan_needs_proposal_empty(self):
        self.assertTrue(plan_needs_proposal(()))

    def test_plan_needs_proposal_proving_check(self):
        chk = PlannedCheck(
            cmd="true", role=ROLE_CHECK, source="project",
            tier=TIER_AUTHORITATIVE, reason="t", proves=True,
        )
        self.assertFalse(plan_needs_proposal((chk,)))

    def test_plan_needs_proposal_keep_only(self):
        keep = PlannedCheck(
            cmd="true", role="keep", source="baseline",
            tier=TIER_AUTHORITATIVE, reason="t", proves=False,
        )
        self.assertTrue(plan_needs_proposal((keep,)))

    def test_parse_proposed_commands_caps_at_two(self):
        text = "true\nfalse\nmake test"
        rows = parse_proposed_commands(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].tier, TIER_ADVISORY)
        self.assertFalse(rows[0].proves)

    def test_parse_skips_unresolvable_argv0(self):
        rows = parse_proposed_commands("definitely-not-a-binary-xyz foo")
        self.assertEqual(rows, ())


class MemoryIndexCliTests(unittest.TestCase):
    def test_index_status_off(self):
        line = memory_index.index_status_line({"memory_index": "off"})
        self.assertIn("off", line)

    def test_reindex_scan_backend_noop(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.memory_index.project_dir", return_value=Path(d)):
                idx = memory_index.MemoryIndex()
                idx.reindex({"memory_index": "off"})
                self.assertFalse(idx.path.exists())


class CanvasViewTests(unittest.TestCase):
    def test_layout_stable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.knowledge_graph.memory.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root):
                cfg = {"use_graph": True}
                kg = knowledge_graph.KnowledgeGraph()
                kg.add_node("learning", "a", label="alpha")
                kg.add_node("learning", "b", label="beta")
                v1 = kg.canvas_view()
                v2 = kg.canvas_view()
                self.assertEqual(
                    [(n.id, n.x, n.y) for n in v1.nodes],
                    [(n.id, n.x, n.y) for n in v2.nodes],
                )


class GraphProposeTests(unittest.TestCase):
    def test_reserved_name_load(self):
        with self.assertRaises(graph_mod.GraphError):
            graph_mod.load_graph("propose")

    def test_refuses_before_five_terminal_runs(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.graph.project_dir", return_value=Path(d)), \
                 patch("lmloop.workflow.count_terminal_runs", return_value=2):
                with self.assertRaises(graph_mod.GraphError) as ctx:
                    graph_mod.propose_graph_draft(
                        {}, "m", "ship", echo_status=lambda *_: None,
                    )
                self.assertIn("5", str(ctx.exception))


class TerminalRunCountTests(unittest.TestCase):
    def test_counts_done_rows(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            until = root / "until"
            until.mkdir(parents=True)
            (until / "one.jsonl").write_text(
                '{"role":"meta"}\n{"role":"done","status":"pass"}\n',
            )
            with patch("lmloop.workflow.project_dir", return_value=root):
                self.assertEqual(workflow.count_terminal_runs(slug="x"), 1)


if __name__ == "__main__":
    unittest.main()
