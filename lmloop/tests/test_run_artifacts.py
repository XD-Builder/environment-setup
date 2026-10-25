"""Tests for Meta-style until run artifacts and host assess."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.loop import UntilRun, run_until
from lmloop.run.run_artifacts import (
    UntilArtifactStore,
    analyze_until_run,
    extract_json_object,
    format_budget_block,
    format_until_analyze,
    host_assess_state,
    normalize_until_control,
    single_action_instruction,
)


class ControlParseTests(unittest.TestCase):
    def test_normalize_modes(self):
        self.assertEqual(normalize_until_control("budget"), "budget")
        self.assertEqual(normalize_until_control("META"), "meta")
        self.assertEqual(normalize_until_control("nope"), "off")

    def test_until_args_control_flag(self):
        from lmloop.loop import parse_until_args

        parsed = parse_until_args(["--control", "budget", "ship", "it"])
        self.assertIsNone(parsed.err)
        self.assertEqual(parsed.control, "budget")
        self.assertEqual(parsed.goal, "ship it")


class ArtifactStoreTests(unittest.TestCase):
    def test_monotonic_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_path = Path(tmp) / "20260101.jsonl"
            run_path.write_text("", encoding="utf-8")
            store = UntilArtifactStore(run_path)
            first = store.append_artifact(kind="maker", step=1, summary="one")
            second = store.append_artifact(kind="check", step=2, summary="two")
            self.assertEqual(first, "a1")
            self.assertEqual(second, "a2")
            rows = store.load_artifacts()
            self.assertEqual(rows[1]["refs"], ["a1"])

    def test_host_assess_mentions_failing_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_path = Path(tmp) / "run.jsonl"
            run_path.write_text("", encoding="utf-8")
            store = UntilArtifactStore(run_path)
            focus = host_assess_state(
                store,
                goal="fix tests",
                run_tokens=100,
                token_budget=0,
                makers_this_call=1,
                until_max_steps=12,
                maker_count=1,
                check_results=[
                    {"cmd": "pytest -q", "status": "fail", "output": "FAILED test_x"},
                ],
                changed_files=["lmloop/loop.py"],
            )
            self.assertIn("pytest", focus)
            state = store.read_state()
            self.assertIn("failing", state)
            self.assertIn("Fix failing check", store.assignment_from_state())


class UntilControlIntegrationTests(unittest.TestCase):
    def test_budget_prompt_and_artifacts(self):
        cfg = {
            "until_max_steps": 3,
            "until_mine": False,
            "confirm_shell": False,
            "until_control": "budget",
            "run_token_budget": 0,
        }
        captured: list[str] = []

        def fake_act(_cfg, _model, user_text, **_kwargs):
            captured.append(user_text)
            if "independent checker" in user_text:
                return (
                    [{"role": "assistant", "content": "not yet\nSTATUS: fail"}],
                    Path("/tmp/fake-eval.jsonl"),
                )
            return (
                [{"role": "assistant", "content": "did work"}],
                Path("/tmp/fake-maker.jsonl"),
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            state_root.mkdir()
            proj = state_root / "projects" / "ws"
            (proj / "until").mkdir(parents=True)
            with patch("lmloop.loop.isolated_act", side_effect=fake_act), \
                 patch("lmloop.loop.run_plan_commands", return_value=[
                     {"cmd": "false", "status": "fail", "output": "[exit code: 1]"},
                 ]), \
                 patch("lmloop.loop.snapshot.take_snapshot", return_value=type(
                     "Snap", (), {"ref": "refs/lmloop/x"},
                 )()), \
                 patch("lmloop.loop.project_dir", return_value=proj):
                run = UntilRun.create(
                    "goal", checks=("false",), until_control="budget",
                )
                run_until(cfg, "m", run=run, workspace_root=root, echo_status=lambda *_: None)
                self.assertTrue(captured)
                self.assertIn("Budget (this until run)", captured[0])
                self.assertIn("Run state (compact)", captured[0])
                store = UntilArtifactStore(run.path)
                self.assertGreaterEqual(len(store.load_artifacts()), 2)
                self.assertIn("Fix failing check", store.read_state())


class BudgetBlockTests(unittest.TestCase):
    def test_unlimited_token_label(self):
        block = format_budget_block(
            run_tokens=50,
            token_budget=0,
            makers_this_call=2,
            until_max_steps=12,
            maker_count=4,
        )
        self.assertIn("50 / unlimited", block)


class ControlHelperTests(unittest.TestCase):
    def test_extract_json_object_from_prose(self):
        payload = extract_json_object('note {"instruction": "fix it", "context_artifact_ids": []}')
        self.assertEqual(payload.get("instruction"), "fix it")

    def test_single_action_requires_one_fail(self):
        self.assertIsNone(single_action_instruction([
            {"cmd": "a", "status": "fail"},
            {"cmd": "b", "status": "fail"},
        ]))
        text = single_action_instruction([{"cmd": "pytest -q", "status": "fail"}])
        self.assertIn("pytest", text or "")


class MetaControlIntegrationTests(unittest.TestCase):
    def test_meta_runs_control_stages(self):
        cfg = {
            "until_max_steps": 3,
            "until_mine": False,
            "confirm_shell": False,
            "until_control": "meta",
            "run_token_budget": 0,
            "eval_model": "eval-m",
        }
        stage_calls: list[str] = []

        def fake_act(_cfg, _model, user_text, **_kwargs):
            if "independent checker" in user_text:
                return (
                    [{"role": "assistant", "content": "no\nSTATUS: fail"}],
                    Path("/tmp/fake-eval.jsonl"),
                )
            if "_control_assess" in user_text or "Control: Assess" in user_text:
                stage_calls.append("assess")
                body = '{"status_map": [], "open_risks": [], "artifact_verdicts": []}'
            elif "Propose" in user_text:
                stage_calls.append("propose")
                body = '{"actions": [{"id":"p1","description":"a","targets":[],"est_calls":1},'
                body += '{"id":"p2","description":"b","targets":[],"est_calls":1},'
                body += '{"id":"p3","description":"c","targets":[],"est_calls":1}]}'
            elif "Evaluate" in user_text:
                stage_calls.append("evaluate")
                body = '{"ranked": [{"id":"p1","score":0.9,"feasible":true,"reason":"ok"}]}'
            elif "Dispatch" in user_text:
                stage_calls.append("dispatch")
                body = '{"instruction":"Fix timeout","context_artifact_ids":["a1"],"abstain_reason":""}'
            else:
                body = "did work"
            return (
                [{"role": "assistant", "content": body}],
                Path("/tmp/fake-maker.jsonl"),
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            state_root.mkdir()
            proj = state_root / "projects" / "ws"
            (proj / "until").mkdir(parents=True)
            with patch("lmloop.loop.isolated_act", side_effect=fake_act), \
                 patch("lmloop.loop.run_plan_commands", return_value=[
                     {"cmd": "false1", "status": "fail", "output": "a"},
                     {"cmd": "false2", "status": "fail", "output": "b"},
                 ]), \
                 patch("lmloop.loop.snapshot.take_snapshot", return_value=type(
                     "Snap", (), {"ref": "refs/lmloop/x"},
                 )()), \
                 patch("lmloop.loop.project_dir", return_value=proj):
                run = UntilRun.create(
                    "goal", checks=("false1", "false2"), until_control="meta",
                )
                run_until(cfg, "m", run=run, workspace_root=root, echo_status=lambda *_: None)
                loaded = UntilRun.load(run.path)
                controls = [
                    ev for ev in loaded.events if ev.get("role") == "control"
                ]
                stages = {ev.get("stage") for ev in controls}
                self.assertIn("dispatch", stages)
                self.assertTrue({"assess", "propose", "evaluate"} & stages)
                self.assertIn("Fix timeout", UntilArtifactStore(run.path).read_state())


class AnalyzeCliTests(unittest.TestCase):
    def test_analyze_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_path = Path(tmp) / "20260101.jsonl"
            run_path.write_text(
                json.dumps({"role": "meta", "goal": "g", "until_control": "budget"})
                + "\n"
                + json.dumps({"role": "maker", "status": "next", "usage": {"total_tokens": 10}})
                + "\n",
                encoding="utf-8",
            )
            metrics = analyze_until_run(run_path, {"run_token_budget": 100, "until_max_steps": 5})
            self.assertEqual(metrics["maker_cycles"], 1)
            text = format_until_analyze(metrics)
            self.assertIn("reuse_edges", text)


if __name__ == "__main__":
    unittest.main()
