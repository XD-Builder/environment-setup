import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import memory, workflow
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


if __name__ == "__main__":
    unittest.main()
