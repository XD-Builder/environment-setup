import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import workflow
from lmloop.core.commands import parse_flow_words
from lmloop.core.config import DEFAULTS


class WorkflowTests(unittest.TestCase):
    def test_collect_empty_stats(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch("lmloop.workflow.project_dir", return_value=root):
                stats = workflow.collect_flow_stats()
        self.assertEqual(stats.until_runs, 0)
        self.assertEqual(stats.to_dict()["maker_cycles_max"], 0)

    def test_budget_bound_rule(self):
        stats = workflow.FlowStats(until_runs=3, maker_cycles=[11, 12, 10])
        cfg = dict(DEFAULTS)
        rules = workflow.flow_rules(stats, cfg)
        self.assertTrue(any(r.startswith("budget-bound") for r in rules))

    def test_flow_request_is_shared_by_both_invocations(self):
        cli = parse_flow_words(["--json"], invocation="lmloop flow")
        repl = parse_flow_words(["--json"], invocation="/flow")
        self.assertTrue(cli.as_json and repl.as_json)
        self.assertEqual(cli.error, "")
        bad = parse_flow_words(["--json", "extra"], invocation="lmloop flow")
        self.assertEqual(bad.error, "usage: lmloop flow [--json]")
        stats = workflow.FlowStats(until_runs=2, until_pass=1)
        text = workflow.render_flow(stats, dict(DEFAULTS), cli)
        self.assertIn('"until_runs": 2', text)
        plain = workflow.render_flow(stats, dict(DEFAULTS), parse_flow_words([], invocation="/flow"))
        self.assertIn("lmloop flow", plain)


def _ids(stats, cfg=None):
    text = workflow.flow_rules(stats, cfg or dict(DEFAULTS))
    return [line.split(":", 1)[0] for line in text]


class FlowRuleTests(unittest.TestCase):
    def test_each_rule_fires_and_stays_quiet(self):
        quiet = workflow.FlowStats()
        self.assertEqual(_ids(quiet), [])

        bound = workflow.FlowStats(until_runs=4, until_paused=3, maker_cycles=[12, 12, 12, 12])
        self.assertIn("budget-bound", _ids(bound))
        not_paused = workflow.FlowStats(until_runs=4, until_paused=0, maker_cycles=[12, 12, 12, 12])
        self.assertNotIn("budget-bound", _ids(not_paused))

        checker = workflow.FlowStats(terminal_runs=4, checker_only_runs=3)
        self.assertIn("checker-only", _ids(checker))
        self.assertNotIn("checker-only", _ids(workflow.FlowStats(terminal_runs=4, checker_only_runs=1)))

        flaky = workflow.FlowStats(check_observations=[
            {"cmd": "pytest -q", "status": "pass", "tree": "abc123"},
            {"cmd": "pytest -q", "status": "fail", "tree": "abc123"},
        ])
        self.assertIn("flaky-gate", _ids(flaky))
        no_tree = workflow.FlowStats(check_observations=[
            {"cmd": "pytest -q", "status": "pass", "tree": ""},
            {"cmd": "pytest -q", "status": "fail", "tree": ""},
        ])
        self.assertNotIn("flaky-gate", _ids(no_tree))

        blocked = workflow.FlowStats(eval_total=4, eval_blocked=2)
        self.assertIn("blocked-loop", _ids(blocked))
        self.assertNotIn("blocked-loop", _ids(workflow.FlowStats(eval_total=4, eval_blocked=0)))

        denied = workflow.FlowStats(denial_shapes={"rm": {"a", "b", "c"}})
        self.assertIn("repeat-denial", _ids(denied))
        self.assertNotIn("repeat-denial", _ids(workflow.FlowStats(denial_shapes={"rm": {"a"}})))

        self.assertIn("dead-node", _ids(workflow.FlowStats(dead_nodes=["company:qa"])))
        self.assertNotIn("dead-node", _ids(workflow.FlowStats(dead_nodes=[])))

        hot = workflow.FlowStats(transitions=[
            ("build", "fail", "qa"),
            ("build", "fail", "qa"),
            ("build", "fail", "qa"),
            ("docs", "pass", "qa"),
        ])
        self.assertIn("hot-cycle", _ids(hot))
        self.assertTrue(any("build -> qa on fail: 3/4" in line for line in workflow.flow_rules(hot, dict(DEFAULTS))))
        sharp = workflow.FlowStats(
            transitions=list(hot.transitions),
            baseline_fail_nodes={"build"},
        )
        self.assertNotIn("hot-cycle", _ids(sharp))

        source = workflow.FlowStats(dropped_sources={"project": {"r1", "r2", "r3"}})
        self.assertIn("bad-source", _ids(source))
        self.assertNotIn("bad-source", _ids(workflow.FlowStats(dropped_sources={"project": {"r1"}})))

    def test_collect_reads_tree_denials_and_dropped_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            until = root / "until"
            until.mkdir()
            (until / "one.jsonl").write_text(
                "\n".join([
                    json.dumps({"role": "meta", "goal": "g"}),
                    json.dumps({
                        "role": "baseline", "status": "ready",
                        "dropped": [{"cmd": "pytest -q", "source": "project"}],
                    }),
                    json.dumps({
                        "role": "maker", "status": "next",
                        "denied": ["rm -rf build"],
                    }),
                    json.dumps({
                        "role": "check", "status": "fail", "tree": "deadbeef",
                        "results": [{"cmd": "pytest -q", "status": "fail"}],
                    }),
                    json.dumps({"role": "eval", "status": "pass"}),
                    json.dumps({"role": "done", "status": "pass"}),
                ]) + "\n",
                encoding="utf-8",
            )
            graphs = root / "graphs" / "company"
            graphs.mkdir(parents=True)
            body = "\n".join([
                json.dumps({"role": "meta", "name": "company"}),
                json.dumps({
                    "role": "node", "node": "ceo", "status": "fail",
                    "usage": {"total_tokens": 12},
                }),
                json.dumps({"role": "node", "node": "qa", "status": "pass"}),
            ]) + "\n"
            for i in range(5):
                (graphs / f"run-{i}.jsonl").write_text(body, encoding="utf-8")
            with mock.patch("lmloop.workflow.project_dir", return_value=root), \
                 mock.patch("lmloop.workflow._authored_node_names", return_value={"ceo", "qa", "docs"}):
                stats = workflow.collect_flow_stats()
        self.assertEqual(stats.until_runs, 1)
        self.assertEqual(stats.until_pass, 1)
        self.assertEqual(stats.checker_only_runs, 1)
        self.assertIn("rm", stats.denial_shapes)
        self.assertIn("project", stats.dropped_sources)
        self.assertEqual(stats.check_observations[0]["tree"], "deadbeef")
        self.assertEqual(stats.transitions, [("ceo", "fail", "qa")] * 5)
        self.assertEqual(stats.tokens_by_node.get("ceo"), 60)
        self.assertEqual(stats.dead_nodes, ["company:docs"])
        self.assertIn("dead-node", _ids(stats))


if __name__ == "__main__":
    unittest.main()
