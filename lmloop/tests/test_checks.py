"""Derived check plans: goal text, project files, memory, baseline, judge."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.checks import (
    PlannedCheck,
    apply_baseline,
    confirm_plan,
    infer_plan,
    judge_cycle,
)


def _check(cmd, role="check", tier="authoritative", user_typed=False, proves=True,
           source="project"):
    return PlannedCheck(
        cmd=cmd, role=role, source=source, tier=tier, reason="test",
        user_typed=user_typed, proves=proves,
    )


def _result(cmd, status):
    return {"cmd": cmd, "status": status, "output": status}


class GoalExtractionTests(unittest.TestCase):
    def test_backtick_command_extracted(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("make `true` pass", Path(d))
        self.assertEqual([item.cmd for item in plan.checks], ["true"])
        self.assertEqual(plan.checks[0].role, "check")
        self.assertEqual(plan.checks[0].tier, "authoritative")

    def test_keep_cue_marks_keep(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("keep `true` green", Path(d))
        self.assertEqual(plan.checks[0].role, "keep")

    def test_without_breaking_is_keep(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan(
                "make `true` pass without breaking `false`", Path(d),
            )
        roles = {item.cmd: item.role for item in plan.checks}
        self.assertEqual(roles["true"], "check")
        self.assertEqual(roles["false"], "keep")

    def test_unparsable_span_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("see `unterminated", Path(d))
        self.assertEqual(plan.checks, ())

    def test_prose_does_not_invent_a_command(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("please test the widget thoroughly", Path(d))
        self.assertEqual(plan.checks, ())

    def test_make_target_only_when_it_exists(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(infer_plan("make test pass", root).checks, ())
            (root / "Makefile").write_text("test:\n\ttrue\n")
            plan = infer_plan("make test pass", root)
        self.assertEqual([item.cmd for item in plan.checks], ["make test"])

    def test_quoted_span_after_cue(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan('run "true" now', Path(d))
        self.assertEqual([item.cmd for item in plan.checks], ["true"])

    def test_unresolved_backtick_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("run `this-bin-does-not-exist-zzz`", Path(d))
        self.assertEqual(plan.checks, ())


class ProjectReaderTests(unittest.TestCase):
    def test_package_json_uses_lockfile_manager(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "package.json").write_text(json.dumps({
                "scripts": {"test": "jest", "lint": "eslint .", "start": "node"},
            }))
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
            plan = infer_plan("ship it", root)
        cmds = [item.cmd for item in plan.checks]
        self.assertIn("pnpm run test", cmds)
        self.assertIn("pnpm run lint", cmds)
        self.assertNotIn("pnpm run start", cmds)

    def test_pytest_config_prefers_venv_python(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pytest.ini").write_text("[pytest]\n")
            venv = root / ".venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python").write_text("")
            plan = infer_plan("ship it", root)
        self.assertEqual(plan.checks[0].cmd, ".venv/bin/python -m pytest -q")

    def test_pyproject_section_scan_without_pytest_key(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
            self.assertEqual(infer_plan("ship it", root).checks, ())
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
            plan = infer_plan("ship it", root)
        self.assertIn("pytest", plan.checks[0].cmd)

    def test_presence_ecosystems(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "go.mod").write_text("module example.com/x\n")
            (root / "Cargo.toml").write_text("[package]\nname='x'\n")
            plan = infer_plan("ship it", root)
        cmds = [item.cmd for item in plan.checks]
        self.assertIn("go test ./...", cmds)
        self.assertIn("cargo test", cmds)

    def test_precommit_is_a_keep(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".pre-commit-config.yaml").write_text("repos: []\n")
            plan = infer_plan("ship it", root)
        self.assertEqual(plan.checks[0].cmd, "pre-commit run --all-files")
        self.assertEqual(plan.checks[0].role, "keep")

    def test_ci_skips_multiline_and_expressions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            wf = root / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text(
                "jobs:\n"
                "  test:\n"
                "    steps:\n"
                "      - run: npm test\n"
                "      - run: |\n"
                "          npm run lint\n"
                "      - run: echo ${{ secrets.TOKEN }}\n"
                "  integration:\n"
                "    services:\n"
                "      redis:\n"
                "        image: redis\n"
                "    steps:\n"
                "      - run: npm run e2e\n"
            )
            plan = infer_plan("ship it", root)
        cmds = [item.cmd for item in plan.checks]
        self.assertIn("npm test", cmds)
        self.assertNotIn("npm run lint", cmds)
        self.assertFalse(any("secrets" in cmd for cmd in cmds))
        self.assertNotIn("npm run e2e", cmds)

    def test_repo_doc_picks_unittest_and_skips_setup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "DEVELOPMENT.md").write_text(
                "## Setup and tests\n\n"
                "```bash\n"
                "bash setup.sh\n"
                "python -m unittest discover -s tests -v\n"
                "```\n\n"
                "## Install\n\n"
                "```bash\n"
                "python -m unittest discover -s tests -v\n"
                "```\n"
            )
            plan = infer_plan("ship it", root)
        cmds = [item.cmd for item in plan.checks]
        self.assertEqual(cmds, ["python -m unittest discover -s tests -v"])
        self.assertIn("Setup and tests", plan.checks[0].reason)

    def test_this_repo_development_md(self):
        root = Path(__file__).resolve().parents[2]
        plan = infer_plan("ship it", root)
        cmds = [item.cmd for item in plan.checks]
        self.assertTrue(any("unittest discover" in cmd for cmd in cmds))
        self.assertFalse(any("setup-lmloop" in cmd for cmd in cmds))


class MemoryAndHistoryTests(unittest.TestCase):
    def test_user_stated_is_authoritative_observed_is_advisory(self):
        learnings = [
            {
                "type": "tool", "source": "user-stated",
                "insight": "the suite is `true`",
            },
            {
                "type": "operational", "source": "observed",
                "insight": "lint with `false`",
            },
            {
                "type": "pattern", "source": "user-stated",
                "insight": "ignore `ignore-me-bin`",
            },
        ]
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("ship it", Path(d), learnings=learnings, history=[])
        by_cmd = {item.cmd: item for item in plan.checks}
        self.assertEqual(by_cmd["true"].tier, "authoritative")
        self.assertEqual(by_cmd["true"].source, "memory")
        self.assertEqual(by_cmd["false"].tier, "advisory")
        self.assertNotIn("ignore-me-bin", by_cmd)

    def test_proven_history_ranks_first(self):
        learnings = [{
            "type": "operational", "source": "observed",
            "insight": "try `false`",
        }]
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan(
                "ship it", Path(d), learnings=learnings, history=["true"],
            )
        self.assertEqual(plan.checks[0].cmd, "true")
        self.assertEqual(plan.checks[0].source, "history")
        self.assertEqual(plan.checks[0].tier, "authoritative")
        self.assertEqual(plan.checks[1].cmd, "false")

    def test_earlier_source_wins_duplicate(self):
        learnings = [{
            "type": "tool", "source": "user-stated",
            "insight": "run `true`",
        }]
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan(
                "ship it", Path(d), learnings=learnings, history=["true"],
            )
        self.assertEqual(len(plan.checks), 1)
        self.assertEqual(plan.checks[0].source, "memory")

    def test_history_scan_fail_then_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            until = root / "until"
            until.mkdir()
            (until / "run.jsonl").write_text(
                "\n".join([
                    json.dumps({"role": "meta", "check_cmd": "pytest -q"}),
                    json.dumps({"role": "check", "status": "fail"}),
                    json.dumps({"role": "check", "status": "pass"}),
                ]) + "\n"
            )
            with patch("lmloop.memory.project_dir", return_value=root):
                plan = infer_plan("ship it", root, learnings=[])
        self.assertEqual(plan.checks[0].cmd, "pytest -q")
        self.assertEqual(plan.checks[0].source, "history")


class BaselineAndJudgeTests(unittest.TestCase):
    def test_inferred_pass_becomes_keep(self):
        outcome = apply_baseline(
            (_check("make test"),),
            [_result("make test", "pass")],
        )
        self.assertEqual(outcome.status, "ready")
        self.assertEqual(outcome.checks[0].role, "keep")

    def test_user_typed_pass_cannot_prove(self):
        outcome = apply_baseline(
            (_check("true", user_typed=True, source="flag"),),
            [_result("true", "pass")],
        )
        self.assertTrue(outcome.checks)
        self.assertFalse(outcome.checks[0].proves)
        self.assertEqual(outcome.checks[0].role, "check")
        self.assertTrue(any("cannot show the change" in note for note in outcome.notes))

    def test_unrunnable_inferred_dropped_typed_blocks(self):
        dropped = apply_baseline(
            (_check("pytest"),), [_result("pytest", "blocked")],
        )
        self.assertEqual(dropped.status, "ready")
        self.assertEqual(dropped.checks, ())
        blocked = apply_baseline(
            (_check("pytest", user_typed=True, source="flag"),),
            [_result("pytest", "blocked")],
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertTrue(any("not runnable" in note for note in blocked.notes))

    def test_failing_keep_blocks(self):
        outcome = apply_baseline(
            (_check("make lint", role="keep"),),
            [_result("make lint", "fail")],
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(any("already broken" in note for note in outcome.notes))

    def test_fail_then_pass_is_done(self):
        checks = (_check("pytest"), _check("make lint", role="keep"))
        status = judge_cycle(
            checks,
            [_result("pytest", "pass"), _result("make lint", "pass")],
            baseline_on=True,
            baseline={"pytest": "fail", "make lint": "pass"},
        )
        self.assertEqual(status, "pass")

    def test_no_proof_is_pending_and_keep_failure_retries(self):
        checks = (_check("make test", role="keep"),)
        self.assertEqual(judge_cycle(
            checks, [_result("make test", "pass")],
            baseline_on=True, baseline={"make test": "pass"},
        ), "pending")
        self.assertEqual(judge_cycle(
            checks, [_result("make test", "fail")],
            baseline_on=True, baseline={"make test": "pass"},
        ), "fail")

    def test_advisory_cannot_finish_a_run(self):
        advisory = (_check("npm test", tier="advisory", source="ci"),)
        self.assertEqual(judge_cycle(
            advisory, [_result("npm test", "pass")],
            baseline_on=False, baseline={},
        ), "pending")
        self.assertEqual(judge_cycle(
            advisory, [_result("npm test", "fail")],
            baseline_on=False, baseline={},
        ), "fail")

    def test_baseline_off_passes_on_a_green_check(self):
        checks = (_check("true", user_typed=True, source="flag"),)
        self.assertEqual(judge_cycle(
            checks, [_result("true", "pass")],
            baseline_on=False, baseline={},
        ), "pass")

    def test_edit_becomes_user_typed(self):
        with tempfile.TemporaryDirectory() as d:
            original = infer_plan("make `true` pass", Path(d))
        answers = iter(["e", "keep false", ""])

        def read_line(_prompt=""):
            return next(answers)

        edited = confirm_plan(original, read_line, lambda *_a: None)
        self.assertEqual(len(edited.checks), 1)
        self.assertEqual(edited.checks[0].cmd, "false")
        self.assertEqual(edited.checks[0].role, "keep")
        self.assertTrue(edited.checks[0].user_typed)
        self.assertEqual(edited.checks[0].tier, "authoritative")

    def test_skip_clears_the_plan(self):
        with tempfile.TemporaryDirectory() as d:
            plan = infer_plan("make `true` pass", Path(d))
        cleared = confirm_plan(plan, lambda _prompt="": "s", lambda *_a: None)
        self.assertEqual(cleared.checks, ())


class CompanyInferenceTests(unittest.TestCase):
    def test_company_until_node_infers_on_a_node_project(self):
        from lmloop.graph import parse_graph

        text = Path(__file__).resolve().parents[1] / "lmloop" / "graphs" / "company.md"
        defn = parse_graph(text.read_text(), "company")
        self.assertIsNone(defn.node("build").check_cmd)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "package.json").write_text(json.dumps({
                "scripts": {"test": "node --test"},
            }))
            plan = infer_plan(defn.node("build").goal, root)
        cmds = [item.cmd for item in plan.checks]
        self.assertIn("npm run test", cmds)
        self.assertFalse(any(cmd.startswith("pytest") for cmd in cmds))
