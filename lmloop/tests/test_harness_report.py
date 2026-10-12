"""Harness execution report (H2)."""

import json
import unittest
from pathlib import Path

from lmloop import harness_report

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "harness_report" / "sample_until.jsonl"
)


class HarnessReportTests(unittest.TestCase):
    def test_build_report_from_fixture(self):
        report = harness_report.build_harness_execution_report(FIXTURE)
        self.assertEqual(report["task"]["mode"], "until")
        self.assertEqual(report["task"]["goal"], "make tests pass")
        self.assertEqual(report["harness_bundle_id"], "deadbeef")
        agg = report["aggregate"]
        self.assertEqual(agg["checks_fail"], 1)
        self.assertEqual(len(report["failures"]), 1)
        self.assertIn("FAILED", report["failures"][0]["excerpt"])
        feedback = report["question_sets"]["feedback"]
        self.assertIn("schema-read-file-shape", feedback)
        self.assertNotIn("metric-exact", feedback)

    def test_write_round_trip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "report.json"
            harness_report.write_harness_execution_report(
                FIXTURE, dest=dest,
            )
            loaded = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(loaded["task"]["goal"], "make tests pass")


if __name__ == "__main__":
    unittest.main()
