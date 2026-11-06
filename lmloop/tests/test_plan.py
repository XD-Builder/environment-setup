"""Implementation plans: parser, red-green ledger, finish word, worktrees."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.company.worktree import (
    WorktreeError,
    ensure_worktree,
    feature_worktree,
    land_branch,
    remove_owned_worktree,
    worktrees_ignored,
)
from lmloop.core.commands import PlanRequest, parse_plan_words
from lmloop.harness.contracts import check_trajectory
from lmloop.prompts import skills
from lmloop.run.plan_runner import (
    PlanHost,
    discard_refusal,
    gate_reason,
    parse_plan,
    run_plan,
    serve_plan,
    shell_evidence,
)


FIXTURE = """# Widget

**Goal:** add a widget

**Spec:** docs/designs/widget.md

## Global Constraints

- python 3.14

## Review Focus

- empty name is rejected

### Task 1: Parser

- [ ] **Step 1: Write the failing test**

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_widget -v`
Expected: FAIL

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m unittest tests.test_widget -v`
Expected: PASS

### Task 2: Wire the command

- [ ] **Step 1: Run the suite**

Run: `python -m unittest tests.test_widget -v`
Expected: PASS
"""

CMD = "python -m unittest tests.test_widget -v"


def _messages(*rounds: tuple) -> list:
    """rounds are (command, exit_code) pairs, then a final assistant claim."""
    messages = []
    for command, code in rounds:
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "run_shell",
                    "arguments": json.dumps({"command": command}),
                },
            }],
        })
        messages.append({
            "role": "tool",
            "content": f"output\n[exit code: {code}]",
        })
    messages.append({"role": "assistant", "content": "task done"})
    return messages


def _host(**overrides) -> PlanHost:
    notes = []
    base = dict(
        cfg={},
        model="test",
        workspace=Path("."),
        echo=lambda text: notes.append(("echo", text)),
        error=lambda text: notes.append(("error", text)),
        ask=lambda _prompt: False,
        isatty=False,
        open_worktree=lambda *_args: Path("."),
        run_check=lambda *_args: "[exit code: 0]",
        land=lambda *_args, **_kwargs: (True, "landed"),
        discard=lambda *_args: (True, "discarded"),
        linked=True,
        branch="feature",
        base="master",
    )
    base.update(overrides)
    host = PlanHost(**base)
    host._notes = notes  # type: ignore[attr-defined]
    return host


class PlanParseTests(unittest.TestCase):
    def test_parser_reads_commands_expected_and_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-11-06-widget.md"
            path.write_text(FIXTURE, encoding="utf-8")
            doc = parse_plan(path.read_text(encoding="utf-8"), path=path)
            again = parse_plan(path.read_text(encoding="utf-8"), path=path)
        self.assertEqual(doc.plan_id, "2026-11-06-widget")
        self.assertEqual(doc.goal, "add a widget")
        self.assertIn("python 3.14", doc.constraints)
        self.assertEqual(len(doc.tasks), 2)
        red = doc.tasks[0].steps[1]
        green = doc.tasks[0].steps[2]
        self.assertTrue(red.is_red)
        self.assertEqual(red.command, CMD)
        self.assertEqual(red.expected, "FAIL")
        self.assertTrue(green.is_green)
        self.assertEqual(green.expected, "PASS")
        self.assertEqual(
            [step.command for step in doc.tasks[1].steps],
            [step.command for step in again.tasks[1].steps],
        )

    def test_unnamed_task_is_an_error(self):
        with self.assertRaises(Exception) as caught:
            parse_plan("### Task\n\n- [ ] Step\n")
        self.assertIn("cannot name", str(caught.exception))

    def test_fenced_run_line_is_not_a_command(self):
        text = (
            "### Task 1: Example\n\n"
            "- [ ] **Step 1: Show a sample**\n\n"
            "```\nRun: `not-a-command`\nExpected: FAIL\n```\n"
            "Run: `python -m unittest tests.test_widget -v`\n"
            "Expected: PASS\n"
        )
        doc = parse_plan(text)
        step = doc.tasks[0].steps[0]
        self.assertEqual(step.command, CMD)
        self.assertFalse(step.is_red)
        self.assertTrue(step.is_green)


class PlanGateTests(unittest.TestCase):
    def _task(self):
        return parse_plan(FIXTURE).tasks[0]

    def test_red_step_requires_fail_then_pass(self):
        task = self._task()
        only_green = shell_evidence(_messages((CMD, 0)))
        reason = gate_reason(task, only_green, [])
        self.assertIn("fail-then-pass", reason)
        both = shell_evidence(_messages((CMD, 1), (CMD, 0)))
        self.assertEqual(gate_reason(task, both, []), "")

    def test_claim_without_exit_zero_stays_open(self):
        task = parse_plan(FIXTURE).tasks[1]
        spans = shell_evidence([{"role": "assistant", "content": "done"}])
        reason = gate_reason(task, spans, [])
        self.assertIn("no fresh exit 0", reason)

    def test_ruling_does_not_skip_a_fresh_success(self):
        task = parse_plan(FIXTURE).tasks[1]
        spans = shell_evidence([{"role": "assistant", "content": f"Ruling: {CMD} - plan command is wrong"}])
        reason = gate_reason(task, spans, [f"Ruling: {CMD} - plan command is wrong"])
        self.assertEqual(reason, "derived")


class PlanRunTests(unittest.TestCase):
    def test_runner_blocks_green_without_red_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "2026-11-06-widget.md"
            plan.write_text(FIXTURE, encoding="utf-8")
            state = root / "state"
            calls = []

            def act(prompt: str) -> list:
                calls.append(prompt)
                if "Wire the command" in prompt:
                    return _messages((CMD, 0))
                return _messages((CMD, 0))

            with patch("lmloop.run.plan_runner.STATE_ROOT", state), \
                 patch("lmloop.run.plan_runner.project_slug", return_value="proj"):
                blocked = run_plan(
                    {}, "m", plan, workspace=root, act=act, review_act=act,
                    snapshot=False,
                )
                self.assertEqual(blocked.status, "blocked")
                self.assertEqual(len(calls), 1)
                self.assertIn("Parser", calls[0])
                self.assertNotIn("Wire the command", calls[0])

                def act_red(prompt: str) -> list:
                    calls.append(prompt)
                    if "Wire the command" in prompt:
                        return _messages((CMD, 0))
                    return _messages((CMD, 1), (CMD, 0))

                calls.clear()
                done = run_plan(
                    {}, "m", plan, workspace=root, act=act_red,
                    review_act=lambda _prompt: [{"role": "assistant", "content": "Verdict: SHIP"}],
                    snapshot=False,
                )
            self.assertEqual(done.status, "done")
            self.assertEqual(len(calls), 2)
            self.assertIn("task 1", calls[0].lower())
            joined = "\n".join(calls)
            self.assertNotIn("### Task 1", calls[1])
            self.assertIn("Wire the command", joined)

    def test_resume_skips_a_complete_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "2026-11-06-widget.md"
            plan.write_text(FIXTURE, encoding="utf-8")
            state = root / "state"
            seen = []

            def act(prompt: str) -> list:
                seen.append(prompt)
                if "Parser" in prompt:
                    raise AssertionError("completed task was dispatched again")
                return _messages((CMD, 0))

            with patch("lmloop.run.plan_runner.STATE_ROOT", state), \
                 patch("lmloop.run.plan_runner.project_slug", return_value="proj"):
                from lmloop.run.plan_runner import ledger_path, save_ledger
                doc = parse_plan(plan.read_text(encoding="utf-8"), path=plan)
                ledger = ledger_path("proj", doc.plan_id)
                save_ledger(ledger, {
                    "version": 1,
                    "plan_path": str(plan.resolve()),
                    "plan_id": doc.plan_id,
                    "tasks": {"1": {"status": "complete", "title": "Parser"}},
                    "minors": [],
                    "review": "",
                })
                result = run_plan(
                    {}, "m", plan, workspace=root, act=act,
                    review_act=lambda _prompt: [{
                        "role": "assistant",
                        "content": "MINOR naming\nVerdict: SHIP",
                    }],
                    snapshot=False,
                )
            self.assertEqual(result.status, "done")
            self.assertEqual(len(seen), 1)
            self.assertTrue(any(line.startswith("MINOR") for line in result.minors))

    def test_fix_wave_requires_red_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "2026-11-06-widget.md"
            plan.write_text(FIXTURE, encoding="utf-8")
            prompts = []

            def act(prompt: str) -> list:
                prompts.append(prompt)
                if prompt.startswith("# Skill: tdd") and "One fix wave" in prompt:
                    return [{"role": "assistant", "content": "fixed"}]
                if "Parser" in prompt:
                    return _messages((CMD, 1), (CMD, 0))
                return _messages((CMD, 0))

            with patch("lmloop.run.plan_runner.STATE_ROOT", root / "state"), \
                 patch("lmloop.run.plan_runner.project_slug", return_value="proj"):
                result = run_plan(
                    {}, "m", plan, workspace=root, act=act,
                    review_act=lambda _prompt: [{
                        "role": "assistant",
                        "content": "BLOCKER widget.py:1 empty name crashes",
                    }],
                    snapshot=False,
                )
            self.assertEqual(result.status, "review_blocked")
            self.assertTrue(any("One fix wave" in prompt for prompt in prompts))


class PlanFinishTests(unittest.TestCase):
    def test_discard_requires_the_word(self):
        self.assertIn("discard", discard_refusal("please"))
        self.assertEqual(discard_refusal("discard"), "")
        host = _host()
        code = serve_plan(PlanRequest(action="finish", discard="please"), host)
        self.assertEqual(code, 2)
        self.assertTrue(any(
            item[0] == "error" and "discard" in item[1] for item in host._notes
        ))

    def test_finish_lands_the_worktree_the_run_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "2026-11-06-widget.md"
            plan.write_text(FIXTURE, encoding="utf-8")
            opened = []
            landed = []

            def open_wt(repo, name):
                dest = Path(repo) / ".lmloop" / "worktrees" / name
                opened.append(dest)
                return dest

            def land(repo, choice, *, base, branch, worktree_path=None):
                landed.append((repo, choice, base, branch, worktree_path))
                return True, "merged"

            def act(prompt: str) -> list:
                if "Parser" in prompt:
                    return _messages((CMD, 1), (CMD, 0))
                return _messages((CMD, 0))

            with patch("lmloop.run.plan_runner.STATE_ROOT", root / "state"), \
                 patch("lmloop.run.plan_runner.project_slug", return_value="proj"):
                host = _host(
                    workspace=root, linked=False, branch="master", base="master",
                    open_worktree=open_wt, land=land, act=act,
                    review_act=lambda _prompt: [{
                        "role": "assistant", "content": "Verdict: SHIP",
                    }],
                )
                code = serve_plan(
                    PlanRequest(action="run", path=str(plan), worktree="yes"), host,
                )
                self.assertEqual(code, 0, host._notes)
                finish = _host(
                    workspace=root, linked=False, branch="master", base="master",
                    land=land,
                )
                finished = serve_plan(PlanRequest(action="finish", choice="merge"), finish)
            self.assertEqual(finished, 0)
            self.assertEqual(len(opened), 1)
            self.assertEqual(landed, [(root, "merge", "master", "lmloop/2026-11-06-widget", opened[0])])

    def test_finish_from_the_linked_worktree_uses_the_main_checkout(self):
        landed = []

        def land(repo, choice, *, base, branch, worktree_path=None):
            landed.append((repo, choice, branch, worktree_path))
            return True, "merged"

        work = Path("/repo/.lmloop/worktrees/widget")
        host = _host(
            workspace=work, linked=True, branch="lmloop/widget", base="main",
            land=land, main_checkout=lambda: Path("/repo"),
        )
        code = serve_plan(PlanRequest(action="finish", choice="merge"), host)
        self.assertEqual(code, 0)
        self.assertEqual(landed, [(Path("/repo"), "merge", "lmloop/widget", work)])

    def test_parse_plan_words(self):
        request = parse_plan_words(["run", "docs/plans/a.md", "--no-worktree"])
        self.assertEqual(request.action, "run")
        self.assertEqual(request.worktree, "no")
        self.assertFalse(request.allow_red_baseline)
        bad = parse_plan_words(["finish", "--choice", "rebase"])
        self.assertTrue(bad.error)


class WorktreePlanTests(unittest.TestCase):
    def test_refuses_a_name_that_escapes_the_worktree_dir(self):
        repo = Path("/tmp/lmloop-not-a-repo")
        with self.assertRaises(WorktreeError):
            ensure_worktree(repo, "../outside", "abc")
        with self.assertRaises(WorktreeError):
            ensure_worktree(repo, "a/b", "abc")

    def test_feature_worktree_requires_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            self.assertFalse(worktrees_ignored(repo))
            with self.assertRaises(WorktreeError) as caught:
                feature_worktree(repo, "widget")
            self.assertIn("gitignore", str(caught.exception).lower())
            (repo / ".gitignore").write_text(".lmloop/\n", encoding="utf-8")
            self.assertTrue(worktrees_ignored(repo))

    def test_remove_never_passes_force(self):
        with patch("lmloop.company.worktree.git") as git:
            git.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            )
            ok, _detail = remove_owned_worktree(
                Path("/repo"), Path("/repo/.lmloop/worktrees/widget"),
            )
        self.assertTrue(ok)
        for call in git.call_args_list:
            self.assertNotIn("--force", call.args)
            self.assertNotIn("-f", call.args)

    def test_local_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "t@example.com")
            self._git(repo, "config", "user.name", "t")
            (repo / "f.txt").write_text("base\n", encoding="utf-8")
            self._git(repo, "add", "f.txt")
            self._git(repo, "commit", "-m", "base")
            self._git(repo, "checkout", "-b", "feature")
            (repo / "f.txt").write_text("feature\n", encoding="utf-8")
            self._git(repo, "commit", "-am", "feature")
            self._git(repo, "checkout", "main")
            ok, detail = land_branch(repo, "merge", base="main", branch="feature")
            self.assertTrue(ok, detail)
            self.assertIn("feature", (repo / "f.txt").read_text(encoding="utf-8"))

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        )


class SkillIndexTests(unittest.TestCase):
    def test_index_omits_skill_bodies(self):
        text = skills.system_prompt({})
        self.assertIn(skills.SKILL_INDEX_HEADING, text)
        self.assertIn("`design`:", text)
        self.assertNotIn("no production code without a failing test first", text.lower())

    def test_read_skill_directory_part_and_user_override(self):
        body = skills.read_skill("investigate", "techniques")
        self.assertIn("Backward trace", body)
        with tempfile.TemporaryDirectory() as tmp:
            packaged = Path(tmp) / "packaged"
            user = Path(tmp) / "user"
            (packaged / "widget").mkdir(parents=True)
            (packaged / "widget" / "SKILL.md").write_text(
                "# Skill: widget — demo\n\nUse when: directory skill.\n\nSECRET_BODY_SENTENCE\n",
                encoding="utf-8",
            )
            (packaged / "widget" / "extra.md").write_text("part body\n", encoding="utf-8")
            user.mkdir()
            with patch.object(skills, "SKILLS_DIR", packaged), \
                 patch.object(skills, "USER_SKILLS_DIR", user):
                self.assertIn("widget", skills.list_skills())
                self.assertIn("SECRET_BODY_SENTENCE", skills.read_skill("widget"))
                self.assertIn("part body", skills.read_skill("widget", "extra"))
                index = skills.skill_index_block()
                self.assertIn("directory skill", index)
                self.assertNotIn("SECRET_BODY_SENTENCE", index)
                (user / "widget.md").write_text(
                    "# Skill: widget — user\n\nUse when: user copy.\n\nUSER_BODY\n",
                    encoding="utf-8",
                )
                loaded = skills.read_skill("widget")
                self.assertIn("USER_BODY", loaded)
                self.assertNotIn("SECRET_BODY_SENTENCE", loaded)

    def test_read_skill_tool_returns_the_body(self):
        from lmloop.capabilities.tools import build_tools, dispatch
        _specs, impls = build_tools({"confirm_shell": False})
        text = dispatch(impls, "read_skill", json.dumps({"name": "tdd"}))
        self.assertIn("failing test", text)
        self.assertFalse(text.startswith("ERROR"))
        readonly_specs, readonly_impls = build_tools(
            {"confirm_shell": False}, readonly=True,
        )
        names = {spec["function"]["name"] for spec in readonly_specs}
        self.assertIn("read_skill", names)
        self.assertIn("read_skill", readonly_impls)

    def test_plan_runner_does_not_import_company(self):
        source = Path(__file__).resolve().parents[1] / "lmloop" / "run" / "plan_runner.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("import company", text)
        self.assertNotIn("from ..company", text)


class TrajectoryGateTests(unittest.TestCase):
    def test_edit_before_tdd_fails_and_bare_claim_fails(self):
        early = check_trajectory({
            "order": ["read_skill", "write_file"],
            "calls": [{"name": "write_file", "arguments": {"path": "a.py"}}],
        })
        self.assertTrue(any("order" in item for item in early))
        bare = check_trajectory({
            "require_fresh_shell": True,
            "calls": [{"name": "write_file", "arguments": {}}],
        })
        self.assertIn("completion claim without a shell span", bare)
        covered = check_trajectory({
            "require_fresh_shell": True,
            "calls": [{"name": "run_shell", "arguments": {"command": "true"}}],
        })
        self.assertEqual(covered, [])


if __name__ == "__main__":
    unittest.main()
