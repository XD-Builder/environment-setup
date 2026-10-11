"""Assertion pyramid: schema, contracts, trajectories, gates."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from lmloop import contracts
from lmloop.cli import main
from support import parametrize


def _python(case_id: str, text: str, *, expect: str = "pass", layer: int = 1) -> dict:
    return {
        "id": case_id, "layer": layer, "kind": "python",
        "expect": expect, "text": text,
    }


class SchemaTests(unittest.TestCase):
    def test_integer_rejects_bool(self):
        result = contracts.evaluate_case({
            "id": "bool", "layer": 1, "kind": "schema", "expect": "pass",
            "value": {"n": True},
            "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
        })
        self.assertFalse(result.passed)
        self.assertIn("integer", result.failures[0])

    def test_additional_property_rejected(self):
        result = contracts.evaluate_case({
            "id": "extra", "layer": 1, "kind": "schema", "expect": "fail",
            "value": {"path": "a.py", "note": "x"},
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
            },
        })
        self.assertTrue(result.passed)

    def test_bad_json_text_fails_closed(self):
        result = contracts.evaluate_case({
            "id": "json", "layer": 1, "kind": "schema", "expect": "pass",
            "text": "{", "schema": {"type": "object"},
        })
        self.assertFalse(result.passed)


class ContractTests(unittest.TestCase):
    def test_redact_removes_assignment_and_sk_key(self):
        raw = "api_key=supersecretvalue and sk-abcdefghij"
        cleaned = contracts.redact(raw)
        self.assertNotIn("supersecretvalue", cleaned)
        self.assertNotIn("sk-abcdefghij", cleaned)
        self.assertFalse(contracts.has_secret(cleaned))

    def test_plain_token_word_is_not_a_secret(self):
        self.assertFalse(contracts.has_secret("session token rotation"))

    def test_secret_at_end_of_long_text_is_found(self):
        text = ("filler " * 4000) + "api_key=supersecretvalue"
        self.assertTrue(contracts.has_secret(text))
        result = contracts.evaluate_case({
            "id": "long", "layer": 2, "kind": "invariant", "expect": "pass",
            "text": text, "forbid_secrets": True,
        })
        self.assertFalse(result.passed)

    def test_rouge_bleu_and_cosine_score_overlap(self):
        self.assertGreater(
            contracts.rouge1_f1("the function returns three", "the function returns three items"),
            0.8,
        )
        self.assertGreater(
            contracts.bleu1("the function returns three items", "the function returns three"),
            0.7,
        )
        self.assertGreater(
            contracts.bow_cosine("read the file then write", "write the file"),
            0.5,
        )
        self.assertEqual(contracts.bow_cosine("alpha", "beta"), 0.0)

    def test_rubric_requires_citation_and_grounding(self):
        faithful = contracts.evaluate_case({
            "id": "ok", "layer": 2, "kind": "rubric", "expect": "pass",
            "context": "read_file returns numbered lines",
            "claim": "read_file returns numbered lines",
            "output": "read_file returns numbered lines\nPrior learning applied: read",
            "citation": "Prior learning applied:",
            "min_faithfulness": 0.8,
        })
        self.assertTrue(faithful.passed)
        missing = contracts.evaluate_case({
            "id": "cite", "layer": 2, "kind": "rubric", "expect": "pass",
            "context": "read_file returns numbered lines",
            "claim": "read_file returns numbered lines",
            "output": "read_file returns numbered lines",
            "citation": "Prior learning applied:",
            "min_faithfulness": 0.8,
        })
        self.assertFalse(missing.passed)

    def test_unknown_kind_fails_closed(self):
        result = contracts.evaluate_case({
            "id": "nope", "layer": 1, "kind": "vibes", "expect": "pass",
        })
        self.assertFalse(result.passed)


class TrajectoryTests(unittest.TestCase):
    def test_order_redundancy_cycle_and_abstain(self):
        ordered = contracts.evaluate_case({
            "id": "order", "layer": 3, "kind": "trajectory", "expect": "pass",
            "order": ["read_file", "write_file"],
            "calls": [
                {"name": "read_file", "arguments": {"path": "a"}},
                {"name": "write_file", "arguments": {"path": "b"}},
            ],
        })
        self.assertTrue(ordered.passed)
        redundant = contracts.evaluate_case({
            "id": "dup", "layer": 3, "kind": "trajectory", "expect": "pass",
            "calls": [
                {"name": "read_file", "arguments": {"path": "a"}},
                {"name": "read_file", "arguments": {"path": "a"}},
            ],
        })
        self.assertFalse(redundant.passed)
        cycle = contracts.evaluate_case({
            "id": "cycle", "layer": 3, "kind": "trajectory", "expect": "pass",
            "calls": [
                {"name": "read_file", "arguments": {"path": "a"}},
                {"name": "write_file", "arguments": {"path": "a"}},
                {"name": "read_file", "arguments": {"path": "b"}},
                {"name": "write_file", "arguments": {"path": "b"}},
            ],
        })
        self.assertFalse(cycle.passed)
        abstain = contracts.evaluate_case({
            "id": "stop", "layer": 3, "kind": "trajectory", "expect": "pass",
            "expect_abstain": True, "status": "abstain", "calls": [],
            "rollback": True,
            "state_before": {"goal": "impossible"},
            "state_after": {"goal": "impossible"},
            "state_keys": ["goal"],
        })
        self.assertTrue(abstain.passed)
        acted = contracts.evaluate_case({
            "id": "acted", "layer": 3, "kind": "trajectory", "expect": "pass",
            "expect_abstain": True, "status": "act",
            "calls": [{"name": "run_shell", "arguments": {"command": "true"}}],
        })
        self.assertFalse(acted.passed)


class GateTests(unittest.TestCase):
    @parametrize(
        (0.98, 1.0, 0.02, False),
        (0.97, 1.0, 0.02, True),
        (1.0, 1.0, 0.0, False),
        (0.999, 1.0, 0.0, True),
        names=("rate", "baseline", "max_drop", "exceeds"),
    )
    def test_drop_threshold_uses_basis_points(self, rate, baseline, max_drop, exceeds):
        self.assertEqual(contracts.drop_exceeds(rate, baseline, max_drop), exceeds)

    def test_pr_blocks_above_two_percent_and_allows_two(self):
        good = [_python(f"ok{i}", "x = 1\n") for i in range(98)]
        bad = [_python(f"bad{i}", "def (\n") for i in range(2)]
        allowed = contracts.run_gate("pr", cases=good + bad, baseline={"pr": 1.0})
        self.assertTrue(allowed.ok)
        self.assertFalse(allowed.blocks_merge)
        self.assertAlmostEqual(allowed.pass_rate, 0.98)
        worse = [_python(f"ok{i}", "x = 1\n") for i in range(97)]
        worse += [_python(f"bad{i}", "def (\n") for i in range(3)]
        blocked = contracts.run_gate("pr", cases=worse, baseline={"pr": 1.0})
        self.assertFalse(blocked.ok)
        self.assertTrue(blocked.blocks_merge)
        self.assertFalse(blocked.alert)

    def test_commit_requires_every_layer1_case(self):
        cases = [
            _python("ok", "x = 1\n", layer=1),
            _python("later", "x = 1\n", layer=2),
            _python("bad", "def (\n", layer=1),
        ]
        report = contracts.run_gate("commit", cases=cases, baseline={"commit": 1.0})
        self.assertFalse(report.ok)
        self.assertEqual(report.cases, 2)

    def test_nightly_alerts_without_blocking_merge(self):
        cases = [_python("bad", "def (\n", layer=4)]
        report = contracts.run_gate("nightly", cases=cases, baseline={"nightly": 1.0})
        self.assertFalse(report.ok)
        self.assertTrue(report.alert)
        self.assertFalse(report.blocks_merge)

    def test_unknown_gate_fails_closed(self):
        report = contracts.run_gate("hourly")
        self.assertFalse(report.ok)
        self.assertIn("unknown gate", report.error)

    def test_packaged_golden_gates_match_baseline(self):
        cases = contracts.load_cases()
        baseline = contracts.load_baseline()
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(cases), 40)
        for name in contracts.GATES:
            report = contracts.run_gate(name, cases=cases, baseline=baseline)
            self.assertTrue(report.ok, report.failed or report.error)
            self.assertEqual(report.failed, [])
            self.assertAlmostEqual(report.pass_rate, baseline[name])


class EvalGateCliTests(unittest.TestCase):
    def test_commit_gate_json_exits_zero(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["eval", "--gate", "commit", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["gate"], "commit")
        self.assertGreater(payload["cases"], 0)

    def test_unknown_gate_exits_one(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["eval", "--gate", "hourly"])
        self.assertEqual(code, 1)
        self.assertIn("commit|pr|nightly", err.getvalue())

    def test_mixed_modes_exit_one(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["eval", "--design", "--drain"])
        self.assertEqual(code, 1)
        self.assertIn("usage:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
