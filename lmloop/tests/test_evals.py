"""Eval aggregation, validation, and instrumentation registry."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import evals, usage
from support import parametrize


def _row(feature: str, **detail):
    return {"ts": "2026-01-01T00:00:00Z", "feature": feature, "detail": detail}


class ValidateRowTests(unittest.TestCase):
    @parametrize(
        ({}, "missing ts"),
        ({"ts": "x"}, "missing feature"),
        ({"ts": "x", "feature": "nope"}, "unknown feature"),
        ({"ts": "x", "feature": "tool", "detail": "bad"}, "detail must be an object"),
        names=("row", "msg_fragment"),
    )
    def test_invalid_rows(self, row, msg_fragment):
        err = evals.validate_row(row, index=3)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn(msg_fragment, err.message)
        self.assertEqual(err.index, 3)

    def test_valid_tool_row(self):
        self.assertIsNone(evals.validate_row(_row("tool", name="list_dir")))


class AggregateTests(unittest.TestCase):
    def test_counts_features_tools_and_outcomes(self):
        events = [
            _row("tool", name="read_file"),
            _row("tool", name="read_file"),
            _row("tool.error", name="read_file", phase="impl"),
            _row("until.finish", outcome="paused"),
            _row("until.finish", outcome="pass"),
            _row("agent.act.finish", rounds=14, interrupted=False),
            _row("check.cycle", status="fail", checks=2, blocked=0),
        ]
        stats = evals.aggregate(events)
        self.assertEqual(stats.events, len(events))
        self.assertEqual(stats.tools["read_file"], 2)
        self.assertEqual(stats.tool_errors["read_file"], 1)
        self.assertEqual(stats.until_outcomes["paused"], 1)
        self.assertEqual(stats.act_rounds, [14])

    def test_skips_invalid_rows(self):
        stats = evals.aggregate([{"feature": "tool"}, _row("tool", name="x")])
        self.assertEqual(stats.invalid_rows, 1)
        self.assertEqual(stats.events, 1)


class GapTests(unittest.TestCase):
    def test_tool_error_rate_gap(self):
        events = [_row("tool", name="run_shell") for _ in range(10)]
        events += [_row("tool.error", name="run_shell", phase="impl") for _ in range(4)]
        stats = evals.aggregate(events)
        gaps = evals.find_gaps(stats, min_samples=5)
        ids = {g.id for g in gaps}
        self.assertIn("tool-error-rate", ids)

    def test_until_pause_gap(self):
        events = [_row("until.finish", outcome="paused") for _ in range(5)]
        events += [_row("until.finish", outcome="pass") for _ in range(3)]
        gaps = evals.find_gaps(evals.aggregate(events), min_samples=5)
        self.assertIn("until-pause-heavy", {g.id for g in gaps})


class ReportTests(unittest.TestCase):
    def test_json_round_trip(self):
        stats = evals.aggregate([_row("cli.repl", has_task=True)])
        gaps = evals.find_gaps(stats)
        payload = evals.report_dict(stats, gaps)
        json.loads(json.dumps(payload))
        self.assertIn("gaps", payload)

    def test_design_skeleton_contains_gaps(self):
        gap = evals.EvalGap(
            id="demo", severity="watch", title="t", evidence="e", design_hook="D.md",
        )
        text = evals.design_doc_skeleton([gap])
        self.assertIn("demo", text)
        self.assertIn("Proposed changes", text)


class InstrumentationRegistryTests(unittest.TestCase):
    _FEATURE_RE = re.compile(
        r"""usage(?:_mod)?\.record\(\s*(?:\n\s*)?["']([^"']+)["']""",
        re.MULTILINE,
    )

    def test_known_features_cover_codebase(self):
        root = Path(__file__).resolve().parents[1] / "lmloop"
        found: set[str] = set()
        for path in root.rglob("*.py"):
            if path.name in ("evals.py", "usage.py"):
                continue
            text = path.read_text(encoding="utf-8")
            found.update(self._FEATURE_RE.findall(text))
        self.assertTrue(found, "expected usage.record calls in lmloop/")
        unknown = found - evals.KNOWN_FEATURES
        self.assertEqual(unknown, set(), f"add to KNOWN_FEATURES: {unknown}")
        unused = evals.KNOWN_FEATURES - found
        self.assertEqual(unused, set(), f"remove stale KNOWN_FEATURES: {unused}")


class EvalCliIntegrationTests(unittest.TestCase):
    def test_load_stats_from_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.jsonl"
            with mock.patch.object(usage, "USAGE_PATH", path):
                usage.record("cli.repl", has_task=False)
                stats = evals.load_stats(path)
            self.assertEqual(stats.features["cli.repl"], 1)


class ToolErrorInstrumentationTests(unittest.TestCase):
    def test_dispatch_records_tool_error_on_validation(self):
        from lmloop.tools import build_tools, dispatch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.jsonl"
            with mock.patch.object(usage, "USAGE_PATH", path):
                _specs, impls = build_tools({})
                out = dispatch(impls, "read_file", "{}")
            self.assertTrue(out.startswith("ERROR:"))
            rows = usage.read_events(path)
            self.assertTrue(any(r["feature"] == "tool.error" for r in rows))


class UntilOutcomeTests(unittest.TestCase):
    def test_until_outcome_label(self):
        from lmloop.loop import _until_outcome_label

        self.assertEqual(_until_outcome_label([{"role": "pause"}]), "paused")
        self.assertEqual(
            _until_outcome_label([{"role": "done", "status": "pass"}]),
            "pass",
        )


if __name__ == "__main__":
    unittest.main()
