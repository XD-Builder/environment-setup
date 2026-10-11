import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import memory, workflow
from lmloop.commands import parse_flow_words
from lmloop.config import DEFAULTS


class WorkflowTests(unittest.TestCase):
    def test_collect_empty_stats(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.object(memory, "project_dir", return_value=root):
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


if __name__ == "__main__":
    unittest.main()
