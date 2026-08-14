"""Tests for the until goal loop: parse, check, eval, runner, persist."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.loop import (
    UntilRun,
    check_status_from_output,
    clip_check_output,
    parse_eval_status,
    parse_until_arg_line,
    parse_until_args,
    run_until,
    superseded_until_hint,
)


def _cfg(**kwargs) -> dict:
    out = {
        "until_max_steps": 12,
        "until_mine": False,
        "confirm_shell": False,
        "shell_timeout_s": 5,
        "context_reserve": 2048,
    }
    out.update(kwargs)
    return out


class ParseUntilTests(unittest.TestCase):
    def test_goal_only(self):
        goal, check, err = parse_until_args(["make", "tests", "pass"])
        self.assertIsNone(err)
        self.assertEqual(goal, "make tests pass")
        self.assertIsNone(check)

    def test_check_flag(self):
        goal, check, err = parse_until_args(["--check", "pytest -q", "green"])
        self.assertIsNone(err)
        self.assertEqual(goal, "green")
        self.assertEqual(check, "pytest -q")

    def test_missing_goal(self):
        goal, check, err = parse_until_args(["--check", "true"])
        self.assertIsNotNone(err)
        self.assertEqual(goal, "")

    def test_shlex_quoted_check(self):
        goal, check, err = parse_until_arg_line("--check 'pytest -q' ship it")
        self.assertIsNone(err)
        self.assertEqual(check, "pytest -q")
        self.assertEqual(goal, "ship it")


class EvalStatusTests(unittest.TestCase):
    def test_last_line_pass(self):
        self.assertEqual(parse_eval_status("looks good\nSTATUS: pass"), "pass")

    def test_fail_and_blocked(self):
        self.assertEqual(parse_eval_status("STATUS: fail"), "fail")
        self.assertEqual(parse_eval_status("STATUS: blocked"), "blocked")

    def test_missing_token_is_blocked(self):
        self.assertEqual(parse_eval_status("I think it's done."), "blocked")

    def test_unknown_token_is_blocked(self):
        self.assertEqual(parse_eval_status("STATUS: maybe"), "blocked")

    def test_empty_is_blocked(self):
        self.assertEqual(parse_eval_status(""), "blocked")


class CheckStatusTests(unittest.TestCase):
    def test_exit_zero_is_pass(self):
        self.assertEqual(check_status_from_output("ok\n[exit code: 0]"), "pass")

    def test_nonzero_is_fail(self):
        self.assertEqual(check_status_from_output("[exit code: 1]"), "fail")

    def test_denied_is_blocked(self):
        self.assertEqual(
            check_status_from_output("DENIED: the user declined to run this"),
            "blocked",
        )

    def test_error_is_fail(self):
        self.assertEqual(check_status_from_output("ERROR: boom"), "fail")

    def test_clip_marks_truncation(self):
        body = "x" * 2500 + "\n[exit code: 1]"
        out = clip_check_output(body, limit=2000)
        self.assertIn("[truncated", out)
        self.assertIn(str(len(body)), out)
        self.assertLess(len(out), len(body) + 80)
        self.assertEqual(check_status_from_output(body), "fail")


class UntilRunGraphTests(unittest.TestCase):
    def test_next_role_maker_then_eval(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.loop.project_dir", return_value=Path(d)):
                run = UntilRun.create("g")
                self.assertEqual(run.next_role(do_mine=False), "maker")
                run.append("maker", "next", handoff="did a bit")
                self.assertEqual(run.next_role(do_mine=False), "eval")

    def test_check_fail_goes_to_eval(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.loop.project_dir", return_value=Path(d)):
                run = UntilRun.create("g", check_cmd="false")
                run.append("maker", "next")
                self.assertEqual(run.next_role(do_mine=False), "check")
                run.append("check", "fail")
                self.assertEqual(run.next_role(do_mine=False), "eval")

    def test_eval_pass_without_mine_is_done(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.loop.project_dir", return_value=Path(d)):
                run = UntilRun.create("g")
                run.append("eval", "pass")
                self.assertIsNone(run.next_role(do_mine=False))

    def test_gate_no_is_done(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.loop.project_dir", return_value=Path(d)):
                run = UntilRun.create("g")
                run.append("eval", "blocked")
                self.assertEqual(run.next_role(do_mine=False), "gate")
                run.append("gate", "no")
                self.assertTrue(run.is_done())
                self.assertIsNone(run.next_role(do_mine=False))

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.loop.project_dir", return_value=Path(d)):
                run = UntilRun.create("ship", check_cmd="pytest")
                run.append("maker", "next", handoff="wip")
                loaded = UntilRun.load(run.path)
                self.assertEqual(loaded.goal, "ship")
                self.assertEqual(loaded.check_cmd, "pytest")
                self.assertEqual(loaded.last_handoff(), "wip")
                self.assertFalse(loaded.is_done())
                self.assertFalse(loaded.is_paused())

    def test_superseded_hint_when_open_run_exists(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("lmloop.loop.project_dir", return_value=Path(d)):
                self.assertIsNone(superseded_until_hint())
                UntilRun.create("first")
                hint = superseded_until_hint()
                self.assertIsNotNone(hint)
                self.assertIn("still open", hint)


class UntilRunnerTests(unittest.TestCase):
    def _run(self, root: Path, cfg, fake_act, **kwargs):
        with patch("lmloop.loop.project_dir", return_value=root), \
             patch("lmloop.memory.project_dir", return_value=root), \
             patch("lmloop.loop.agent.act", side_effect=fake_act), \
             patch("lmloop.loop.agent.system_prompt", return_value="sys"):
            run = UntilRun.create(kwargs.pop("goal", "make it work"),
                                  check_cmd=kwargs.pop("check_cmd", None))
            return run_until(
                cfg, "m", run=run, echo=lambda *_a, **_k: None,
                echo_status=lambda *_a, **_k: None,
                workspace_root=root,
                **kwargs,
            )

    def test_act_eval_until_pass(self):
        evals = {"n": 0}

        def fake_act(cfg, model, messages, **kwargs):
            text = messages[-1]["content"]
            if "independent checker" in text:
                evals["n"] += 1
                status = "pass" if evals["n"] >= 2 else "fail"
                messages.append({
                    "role": "assistant",
                    "content": f"checked\nSTATUS: {status}",
                })
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = self._run(root, _cfg(), fake_act)
            self.assertTrue(run.is_done())
            roles = [e.get("role") for e in run.events if e.get("role") != "meta"]
            self.assertEqual(roles[:4], ["maker", "eval", "maker", "eval"])
            self.assertIn("done", roles)
            self.assertEqual(evals["n"], 2)

    def test_eval_uses_fresh_thread(self):
        seen = []
        users = []

        def fake_act(cfg, model, messages, **kwargs):
            seen.append([m.get("role") for m in messages])
            users.append(messages[-1]["content"])
            text = messages[-1]["content"]
            if "independent checker" in text:
                messages.append({
                    "role": "assistant",
                    "content": "ok\nSTATUS: pass",
                })
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            self._run(Path(d), _cfg(), fake_act)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[0], ["system", "user"])
        self.assertEqual(seen[1], ["system", "user"])
        self.assertTrue(any("independent checker" in t for t in users))
        self.assertTrue(any("Do the next checkable increment" in t for t in users))

    def test_missing_status_token_is_blocked(self):
        def no_status(cfg, model, messages, **kwargs):
            messages.append({"role": "assistant", "content": "looks fine to me"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            run = self._run(
                Path(d), _cfg(), no_status, ask_gate=lambda _p: False,
            )
        evals = [e for e in run.events if e.get("role") == "eval"]
        self.assertEqual(evals[0]["status"], "blocked")
        self.assertTrue(run.is_done())
        self.assertFalse(any(
            e.get("role") == "eval" and e.get("status") == "pass"
            for e in run.events
        ))

    def test_eval_after_check_fail_sees_maker_handoff(self):
        users = []

        def fake_act(cfg, model, messages, **kwargs):
            users.append(messages[-1]["content"])
            if "independent checker" in messages[-1]["content"]:
                messages.append({
                    "role": "assistant",
                    "content": "STATUS: pass",
                })
            else:
                messages.append({
                    "role": "assistant",
                    "content": "maker did the work on foo.py",
                })
            return messages

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.loop.run_shell", return_value="fail\n[exit code: 1]"):
                self._run(root, _cfg(), fake_act, check_cmd="false")
        eval_prompt = next(t for t in users if "independent checker" in t)
        self.assertIn("maker did the work on foo.py", eval_prompt)

    def test_check_never_calls_act_on_pass(self):
        acts = {"n": 0}

        def fake_act(cfg, model, messages, **kwargs):
            acts["n"] += 1
            messages.append({"role": "assistant", "content": "worked"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.loop.run_shell", return_value="ok\n[exit code: 0]") as shell:
                run = self._run(
                    root, _cfg(), fake_act, check_cmd="true",
                )
            shell.assert_called_once()
            self.assertEqual(acts["n"], 1)
            self.assertTrue(run.is_done())
            roles = [e.get("role") for e in run.events]
            self.assertIn("check", roles)
            self.assertNotIn("eval", roles)

    def test_max_steps_pauses_and_continue_resumes(self):
        def always_fail(cfg, model, messages, **kwargs):
            if "independent checker" in messages[-1]["content"]:
                messages.append({"role": "assistant", "content": "STATUS: fail"})
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = self._run(root, _cfg(until_max_steps=1), always_fail)
            self.assertTrue(run.is_paused())
            self.assertFalse(run.is_done())
            with patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.agent.act", side_effect=always_fail), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"):
                loaded = UntilRun.load(run.path)
                run_until(
                    _cfg(until_max_steps=1), "m", run=loaded,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    workspace_root=root,
                )
            loaded = UntilRun.load(run.path)
            makers = [e for e in loaded.events if e.get("role") == "maker"]
            self.assertEqual(len(makers), 2)

    def test_gate_no_stops_without_another_maker(self):
        def blocked(cfg, model, messages, **kwargs):
            if "independent checker" in messages[-1]["content"]:
                messages.append({"role": "assistant", "content": "STATUS: blocked"})
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        with tempfile.TemporaryDirectory() as d:
            run = self._run(
                Path(d), _cfg(), blocked, ask_gate=lambda _p: False,
            )
            self.assertTrue(run.is_done())
            makers = [e for e in run.events if e.get("role") == "maker"]
            self.assertEqual(len(makers), 1)
            gates = [e for e in run.events if e.get("role") == "gate"]
            self.assertEqual(gates[-1]["status"], "no")

    def test_pass_with_mine_invokes_callback(self):
        mined = {"n": 0}

        def fake_act(cfg, model, messages, **kwargs):
            if "independent checker" in messages[-1]["content"]:
                messages.append({"role": "assistant", "content": "STATUS: pass"})
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        def mine(paths):
            mined["n"] += 1

        with tempfile.TemporaryDirectory() as d:
            run = self._run(
                Path(d), _cfg(until_mine=True), fake_act, mine=mine,
            )
            self.assertEqual(mined["n"], 1)
            self.assertTrue(run.is_done())
            self.assertTrue(any(e.get("role") == "mine" for e in run.events))

    def test_keyboard_interrupt_pauses(self):
        def boom(cfg, model, messages, **kwargs):
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as d:
            run = self._run(Path(d), _cfg(), boom)
            self.assertTrue(run.is_paused())
            self.assertFalse(run.is_done())

    def test_gate_interrupt_pauses_not_done(self):
        def blocked(cfg, model, messages, **kwargs):
            if "independent checker" in messages[-1]["content"]:
                messages.append({"role": "assistant", "content": "STATUS: blocked"})
            else:
                messages.append({"role": "assistant", "content": "worked"})
            return messages

        def raise_ki(_p):
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as d:
            run = self._run(
                Path(d), _cfg(), blocked, ask_gate=raise_ki,
            )
            self.assertTrue(run.is_paused())
            self.assertFalse(run.is_done())
            self.assertFalse(any(e.get("role") == "gate" for e in run.events))

    def test_check_handoff_truncation_is_marked(self):
        def fake_act(cfg, model, messages, **kwargs):
            messages.append({"role": "assistant", "content": "worked"})
            return messages

        big = ("x" * 2500) + "\n[exit code: 0]"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.loop.run_shell", return_value=big):
                run = self._run(root, _cfg(), fake_act, check_cmd="true")
            checks = [e for e in run.events if e.get("role") == "check"]
            self.assertTrue(checks)
            self.assertIn("[truncated", checks[0]["handoff"])
            self.assertTrue(run.is_done())


if __name__ == "__main__":
    unittest.main()
