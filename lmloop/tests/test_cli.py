"""Tests for lmloop CLI routing, skills listing, and completion."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from lmloop import agent
from lmloop.cli import main
from lmloop.repl import _build_slash_commands
from lmloop.ui import Console


class SkillsDiscoveryTests(unittest.TestCase):
    def test_list_skills_excludes_system(self):
        names = agent.list_skills()
        self.assertIn("ceo", names)
        self.assertIn("investigate", names)
        self.assertNotIn("system", names)
        self.assertNotIn("_author", names)
        self.assertNotIn("_graph_mine", names)
        self.assertNotIn("_reconcile", names)

    def test_skill_blurb(self):
        blurb = agent.skill_blurb("ceo")
        self.assertTrue(blurb)
        self.assertNotIn("#", blurb.split()[0] if blurb else "")

    def test_load_skill_missing(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            agent.load_skill("no-such-skill-xyz")
        self.assertIn("Available:", str(ctx.exception))

    def test_load_author_skill(self):
        text = agent.load_skill("_author")
        self.assertTrue(text.lower().startswith("# skill author"))

    def test_validate_skill_name(self):
        self.assertIsNone(agent.validate_skill_name("deploy"))
        self.assertIsNone(agent.validate_skill_name("compact"))  # skill override ok
        self.assertIsNone(agent.validate_skill_name("retro"))
        self.assertIsNotNone(agent.validate_skill_name("until"))
        self.assertIsNotNone(agent.validate_skill_name("New"))
        self.assertIsNotNone(agent.validate_skill_name("new"))
        self.assertIsNotNone(agent.validate_skill_name("_hidden"))
        self.assertIsNotNone(agent.validate_skill_name("transcript"))
        self.assertIsNotNone(agent.validate_skill_name("q"))

    def test_load_skill_public_only_hides_private(self):
        with self.assertRaises(FileNotFoundError):
            agent.load_skill("_author", public_only=True)
        with self.assertRaises(FileNotFoundError):
            agent.load_skill("system", public_only=True)
        # internal load still works
        self.assertTrue(agent.load_skill("_author"))

    def test_load_skill_rejects_path_like_name(self):
        with self.assertRaises(FileNotFoundError):
            agent.load_skill("../etc/passwd")

    def test_user_skill_overrides_packaged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "ceo.md").write_text("# Skill: ceo — user override\n\nUser body\n")
            with patch.object(agent, "USER_SKILLS_DIR", root):
                text = agent.load_skill("ceo")
                self.assertIn("user override", text)
                path = agent.skill_path("ceo")
                self.assertEqual(path, root / "ceo.md")

    def test_extract_skill_markdown_strips_fence(self):
        raw = "```markdown\n# Skill: demo — x\n\nHello\n```"
        out = agent.extract_skill_markdown(raw)
        self.assertTrue(out.startswith("# Skill: demo"))
        self.assertNotIn("```", out)

    def test_save_user_skill(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(agent, "USER_SKILLS_DIR", root):
                path = agent.save_user_skill("demo", "# Skill: demo — test\n\nBody\n")
                self.assertEqual(path, root / "demo.md")
                self.assertTrue(path.exists())
                self.assertIn("demo", agent.list_skills())


class CliRoutingTests(unittest.TestCase):
    def test_skills_lists_blurbs(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["skills"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("/ceo", out)
        self.assertIn("/investigate", out)

    def test_skills_names_only(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["skills", "names"])
        self.assertEqual(code, 0)
        names = buf.getvalue().strip().splitlines()
        self.assertIn("ceo", names)
        self.assertNotIn("system", names)
        self.assertTrue(all(" " not in n for n in names))

    def test_skill_missing_name(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["skill"])
        self.assertEqual(code, 1)
        self.assertIn("usage:", err.getvalue())

    def test_skill_unknown(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["skill", "no-such-skill-xyz"])
        self.assertEqual(code, 1)
        self.assertIn("No skill", err.getvalue())

    def test_no_skill_flag(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(err):
                main(["--skill", "investigate", "x"])
        self.assertEqual(ctx.exception.code, 2)

    def test_completion_zsh(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["completion", "zsh"])
        self.assertEqual(code, 0)
        script = buf.getvalue()
        self.assertIn("#compdef lmloop", script)
        self.assertIn("_lmloop", script)
        self.assertIn("skills names", script)

    def test_completion_bad_shell(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["completion", "bash"])
        self.assertEqual(code, 1)

    def test_freeform_task_not_reserved(self):
        # "hello world" should reach the REPL, not a subcommand
        with patch("lmloop.cli.run_repl", return_value=0) as repl:
            code = main(["hello", "world"])
        self.assertEqual(code, 0)
        repl.assert_called_once()
        kwargs = repl.call_args.kwargs
        self.assertIsNone(kwargs.get("skill"))
        self.assertEqual(kwargs.get("first_task"), "hello world")

    def test_skill_subcommand_routes_to_repl(self):
        with patch("lmloop.cli.run_repl", return_value=0) as repl:
            code = main(["skill", "ceo", "review", "this"])
        self.assertEqual(code, 0)
        kwargs = repl.call_args.kwargs
        self.assertEqual(kwargs.get("skill"), "ceo")
        self.assertEqual(kwargs.get("first_task"), "review this")

    def test_skills_new_usage(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["skills", "new"])
        self.assertEqual(code, 1)
        self.assertIn("usage:", err.getvalue())

    def test_skills_new_confirm_save(self):
        draft = "# Skill: demoskill — demo\n\nDo the thing.\n"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(agent, "USER_SKILLS_DIR", root), \
                 patch("lmloop.cli.agent.ensure_server", return_value="m"), \
                 patch("lmloop.cli.agent.generate_skill_draft", return_value=draft), \
                 patch("builtins.input", return_value="y"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(["skills", "new", "demoskill", "a", "brief"])
            self.assertEqual(code, 0)
            self.assertTrue((root / "demoskill.md").exists())
            self.assertIn("saved", buf.getvalue())

    def test_skills_new_cancel(self):
        draft = "# Skill: demoskill — demo\n\nDo the thing.\n"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(agent, "USER_SKILLS_DIR", root), \
                 patch("lmloop.cli.agent.ensure_server", return_value="m"), \
                 patch("lmloop.cli.agent.generate_skill_draft", return_value=draft), \
                 patch("builtins.input", return_value="n"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = main(["skills", "new", "demoskill"])
            self.assertEqual(code, 0)
            self.assertFalse((root / "demoskill.md").exists())
            self.assertIn("cancelled", buf.getvalue())


class AutoSlashTests(unittest.TestCase):
    def test_skills_get_slash_shortcuts(self):
        cmds = _build_slash_commands(lambda _: False)
        names = {c.name for c in cmds}
        self.assertIn("/ceo", names)
        self.assertIn("/investigate", names)
        self.assertIn("/review", names)
        self.assertIn("/skills", names)
        self.assertIn("/compact", names)
        self.assertIn("/until", names)
        self.assertIn("/graph", names)
        self.assertIn("/retro", names)
        self.assertEqual(sum(1 for c in cmds if c.name == "/compact"), 1)
        self.assertEqual(sum(1 for c in cmds if c.name == "/retro"), 1)
        retro = next(c for c in cmds if c.name == "/retro")
        self.assertTrue(retro.hidden)
        help_text = Console(color=False).help_text(cmds)
        self.assertIn("/until", help_text)
        self.assertIn("/graph", help_text)
        self.assertNotIn("/graphs", help_text)
        self.assertIn("/memory", help_text)
        self.assertIn("mine", help_text)
        self.assertNotIn("/retro", help_text)


class AtRefTurnTests(unittest.TestCase):
    def test_run_turn_expands_at_refs(self):
        from lmloop.repl import SessionState, _run_turn
        from lmloop.ui import Console, fresh_stats

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x\n")
            state = SessionState(
                cfg={},
                model="m",
                messages=[{"role": "system", "content": "s"}],
                session_log=root / "session.jsonl",
                stats=fresh_stats(),
                console=Console(color=False),
            )
            state.session_log.write_text("")
            captured = {}

            def fake_act(cfg, model, messages, **kwargs):
                captured["content"] = messages[-1]["content"]
                return messages

            with patch("lmloop.repl.agent.act", side_effect=fake_act), \
                 patch("lmloop.repl.Path.cwd", return_value=root), \
                 patch("lmloop.files_index.Path.cwd", return_value=root):
                # Skill-style turn (previously missed expand_at_refs)
                _run_turn(state, "# Skill\n\nTask: look at @a.py", lambda _: False)

            self.assertIn("Referenced files (use read_file):", captured["content"])
            self.assertIn("- a.py", captured["content"])

    def test_run_turn_keeps_repl_on_unexpected_error(self):
        from lmloop.repl import SessionState, _run_turn
        from lmloop.ui import Console, fresh_stats

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = SessionState(
                cfg={},
                model="m",
                messages=[{"role": "system", "content": "s"}],
                session_log=root / "session.jsonl",
                stats=fresh_stats(),
                console=Console(color=False),
            )
            state.session_log.write_text("")
            with patch("lmloop.repl.agent.act", side_effect=RuntimeError("boom")), \
                 redirect_stdout(io.StringIO()), \
                 redirect_stderr(io.StringIO()) as err:
                ok = _run_turn(state, "hello", lambda _: False)
            self.assertFalse(ok)
            self.assertIn("RuntimeError", err.getvalue())
            self.assertEqual(state.messages[-1]["role"], "user")

    def test_cli_skill_rejects_author(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = main(["skill", "_author"])
        self.assertEqual(code, 1)
        self.assertIn("No skill", err.getvalue())


class RestoreArgTests(unittest.TestCase):
    def test_bare_index_is_session(self):
        from lmloop.repl import parse_restore_arg
        self.assertEqual(parse_restore_arg("2"), ("session", "2", False))
        self.assertEqual(parse_restore_arg("2 fresh"), ("session", "2", True))
        self.assertEqual(parse_restore_arg("session 1"), ("session", "1", False))
        self.assertEqual(parse_restore_arg("session 2 fresh"), ("session", "2", True))
        self.assertEqual(parse_restore_arg("checkpoint latest"), ("checkpoint", "latest", False))
        self.assertEqual(parse_restore_arg("latest"), ("session", "latest", False))
        self.assertIsNone(parse_restore_arg(""))
        self.assertIsNone(parse_restore_arg("session 1 extra"))


class RestoreCommandTests(unittest.TestCase):
    def _state(self, root: Path, current: Path):
        from lmloop.repl import SessionState
        from lmloop.ui import Console, fresh_stats
        return SessionState(
            cfg={},
            model="m",
            messages=[{"role": "system", "content": "sys"},
                      {"role": "user", "content": "current work"}],
            session_log=current,
            stats=fresh_stats(),
            console=Console(color=False),
        )

    def _write_session(self, path: Path, rows: list) -> None:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_restore_session_reloads_last_result_without_act(self):
        from lmloop.repl import _cmd_restore

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions = root / "sessions"
            prior = sessions / "20250814-090000.jsonl"
            current = sessions / "20250814-110000.jsonl"
            self._write_session(prior, [
                {"role": "user", "content": "when can I get an epidural"},
                {"role": "assistant", "content": "ACOG: request is sufficient."},
                {"role": "system", "content": "interrupted"},
            ])
            current.write_text("")
            state = self._state(root, current)

            with patch("lmloop.repl.agent.act") as act, \
                 patch("lmloop.repl.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 redirect_stdout(io.StringIO()) as buf:
                ok = _cmd_restore(state, "1")

            self.assertTrue(ok)
            act.assert_not_called()
            self.assertEqual(state.session_log, prior)
            self.assertEqual(
                [(m["role"], m["content"]) for m in state.messages],
                [
                    ("system", "sys"),
                    ("user", "when can I get an epidural"),
                    ("assistant", "ACOG: request is sufficient."),
                ],
            )
            out = buf.getvalue()
            self.assertIn("ACOG: request is sufficient.", out)
            self.assertIn("restored session", out)
            self.assertIn("interrupted", out)

    def test_restore_bare_two_matches_history_index(self):
        from lmloop.repl import _cmd_restore, parse_restore_arg
        self.assertEqual(parse_restore_arg("2"), ("session", "2", False))

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions = root / "sessions"
            s1 = sessions / "20250814-010000.jsonl"
            s2 = sessions / "20250814-020000.jsonl"
            current = sessions / "20250814-030000.jsonl"
            self._write_session(s1, [
                {"role": "user", "content": "session one"},
                {"role": "assistant", "content": "result one"},
            ])
            self._write_session(s2, [
                {"role": "user", "content": "find lmloop"},
                {"role": "assistant", "content": "Found the codebase."},
                {"role": "tool", "content": "run_shell(ls) -> lmloop"},
            ])
            current.write_text("")
            state = self._state(root, current)

            with patch("lmloop.repl.agent.act") as act, \
                 patch("lmloop.repl.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 redirect_stdout(io.StringIO()) as buf:
                _cmd_restore(state, "2")

            act.assert_not_called()
            self.assertEqual(state.session_log, s2)
            self.assertEqual(state.messages[-1]["content"], "Found the codebase.")
            self.assertNotIn("run_shell", buf.getvalue())
            self.assertIn("Found the codebase.", buf.getvalue())

    def test_restore_fresh_copies_into_new_log(self):
        from lmloop.repl import _cmd_restore

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions = root / "sessions"
            prior = sessions / "20250814-090000.jsonl"
            current = sessions / "20250814-110000.jsonl"
            self._write_session(prior, [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ])
            current.write_text("")
            state = self._state(root, current)

            with patch("lmloop.repl.agent.act") as act, \
                 patch("lmloop.repl.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 redirect_stdout(io.StringIO()):
                _cmd_restore(state, "session 1 fresh")

            act.assert_not_called()
            self.assertNotEqual(state.session_log, prior)
            self.assertNotEqual(state.session_log, current)
            self.assertTrue(state.session_log.exists())
            self.assertEqual(state.messages[-1]["content"], "hi there")

    def test_restore_checkpoint_does_not_call_act(self):
        from lmloop.repl import _cmd_restore
        from lmloop.memory import save_checkpoint

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            current = root / "sessions" / "now.jsonl"
            current.parent.mkdir(parents=True)
            current.write_text("")
            state = self._state(root, current)
            with patch("lmloop.memory.project_dir", return_value=root):
                save_checkpoint("handoff", "We were fixing restore.")
            with patch("lmloop.repl.agent.act") as act, \
                 patch("lmloop.repl.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 redirect_stdout(io.StringIO()) as buf:
                _cmd_restore(state, "checkpoint latest")
            act.assert_not_called()
            self.assertIn("We were fixing restore.", buf.getvalue())
            self.assertEqual(state.messages[-1]["role"], "assistant")


class RetroIsolationTests(unittest.TestCase):
    def test_retro_does_not_mutate_live_thread(self):
        from lmloop.repl import SessionState, _cmd_retro
        from lmloop.ui import Console, fresh_stats
        import json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            log = root / "sessions" / "live.jsonl"
            log.parent.mkdir(parents=True)
            with log.open("w") as f:
                f.write(json.dumps({"role": "user", "content": "do work"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": "done"}) + "\n")
            original = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "do work"},
                {"role": "assistant", "content": "done"},
            ]
            state = SessionState(
                cfg={},
                model="m",
                messages=list(original),
                session_log=log,
                stats=fresh_stats(),
                console=Console(color=False),
            )
            captured = {}

            def fake_act(cfg, model, messages, **kwargs):
                captured["messages"] = list(messages)
                captured["log"] = kwargs.get("session_log")
                return messages

            with patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.repl.agent.get_context_limit", return_value=0), \
                 patch("lmloop.repl.agent.load_skill", return_value="# Skill: retro"), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 redirect_stdout(io.StringIO()) as buf:
                _cmd_retro(state, "", lambda _: False)

            self.assertIn("memory mine", buf.getvalue())
            self.assertEqual(state.messages, original)
            self.assertEqual(state.session_log, log)
            self.assertIn("retro", captured["messages"][-1]["content"].lower())
            self.assertIn("[user] do work", captured["messages"][-1]["content"])
            self.assertNotEqual(captured["log"], log)


class CompactSaveUndoTests(unittest.TestCase):
    def _state(self, messages, log):
        from lmloop.repl import SessionState
        from lmloop.ui import Console, fresh_stats
        return SessionState(
            cfg={},
            model="m",
            messages=list(messages),
            session_log=log,
            stats=fresh_stats(),
            console=Console(color=False),
        )

    def test_save_skips_checkpoint_on_interrupt(self):
        from lmloop.repl import _cmd_save

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            log.write_text("")
            state = self._state(
                [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "hi"},
                 {"role": "assistant", "content": "old reply"}],
                log,
            )

            def boom(*_a, **_k):
                raise KeyboardInterrupt()

            with patch("lmloop.loop.agent.act", side_effect=boom), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=Path(d)), \
                 patch("lmloop.memory.save_checkpoint") as save, \
                 redirect_stdout(io.StringIO()):
                _cmd_save(state, "title", lambda _: False)
            save.assert_not_called()
            self.assertEqual(
                [(m["role"], m["content"]) for m in state.messages],
                [("system", "sys"), ("user", "hi"), ("assistant", "old reply")],
            )
            self.assertEqual(state.session_log, log)

    def test_save_does_not_mutate_live_thread(self):
        from lmloop.repl import _cmd_save

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            log.write_text("")
            original = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "old reply"},
            ]
            state = self._state(original, log)

            def fake_act(cfg, model, messages, **kwargs):
                messages.append({"role": "assistant", "content": "checkpoint body"})
                return messages

            with patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=Path(d)), \
                 redirect_stdout(io.StringIO()):
                _cmd_save(state, "title", lambda _: False)

            self.assertEqual(state.messages, original)
            self.assertEqual(state.session_log, log)
            cps = list((Path(d) / "checkpoints").glob("*.md"))
            self.assertEqual(len(cps), 1)
            self.assertIn("checkpoint body", cps[0].read_text())

    def test_compact_does_not_mutate_until_confirm(self):
        from lmloop.repl import _cmd_compact

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            log.write_text("")
            original = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "keep me"},
                {"role": "assistant", "content": "ok"},
            ]
            state = self._state(original, log)

            def fake_act(cfg, model, messages, **kwargs):
                messages.append({"role": "assistant", "content": "## Goal\nDone."})
                return messages

            with patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.repl.agent.load_skill", return_value="# Skill: compact"), \
                 patch("lmloop.memory.project_dir", return_value=Path(d)), \
                 patch("lmloop.repl.ask_yes_no", return_value=False), \
                 redirect_stdout(io.StringIO()):
                _cmd_compact(state, "", lambda _: False)

            self.assertEqual(state.messages, original)

    def test_compact_replace_starts_new_log(self):
        from lmloop.repl import _cmd_compact

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            log.write_text("")
            original = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "keep me"},
                {"role": "assistant", "content": "ok"},
            ]
            state = self._state(original, log)

            def fake_act(cfg, model, messages, **kwargs):
                messages.append({"role": "assistant", "content": "## Goal\nDone."})
                return messages

            with patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.repl.agent.load_skill", return_value="# Skill: compact"), \
                 patch("lmloop.memory.project_dir", return_value=Path(d)), \
                 patch("lmloop.repl.ask_yes_no", return_value=True), \
                 redirect_stdout(io.StringIO()):
                _cmd_compact(state, "", lambda _: False)

            self.assertNotEqual(state.session_log, log)
            self.assertIn("compacted", state.messages[1]["content"])
            self.assertIn("Done.", state.messages[1]["content"])
            logged = state.session_log.read_text()
            self.assertIn("compacted", logged)
            self.assertNotIn("keep me", logged)

    def test_compact_uses_live_messages_not_undone_log(self):
        from lmloop.repl import _cmd_compact, _cmd_undo
        import json

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            rows = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "one"},
                {"role": "user", "content": "undo me"},
                {"role": "assistant", "content": "two"},
            ]
            with log.open("w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            state = self._state(
                [{"role": "system", "content": "sys"}] + rows, log,
            )
            captured = {}

            def fake_act(cfg, model, messages, **kwargs):
                captured["task"] = messages[-1]["content"]
                messages.append({"role": "assistant", "content": "summary"})
                return messages

            with patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.repl.agent.load_skill", return_value="# Skill: compact"), \
                 patch("lmloop.memory.project_dir", return_value=Path(d)), \
                 patch("lmloop.repl.ask_yes_no", return_value=False), \
                 redirect_stdout(io.StringIO()):
                _cmd_undo(state, "")
                _cmd_compact(state, "", lambda _: False)

            self.assertNotIn("undo me", captured["task"])
            self.assertIn("first", captured["task"])

    def test_new_clears_thinking_history(self):
        from lmloop.repl import _cmd_new

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            log.write_text("")
            state = self._state([{"role": "system", "content": "sys"}], log)
            state.push_thinking("old thoughts")
            with patch("lmloop.repl.agent.system_prompt", return_value="sys"), \
                 patch("lmloop.memory.project_dir", return_value=Path(d)), \
                 redirect_stdout(io.StringIO()):
                _cmd_new(state, "")
            self.assertEqual(state.thinking_history, [])


class MemoryMineAndUntilTests(unittest.TestCase):
    def _state(self, root: Path, log: Path, messages=None):
        from lmloop.repl import SessionState
        from lmloop.ui import Console, fresh_stats
        return SessionState(
            cfg={},
            model="m",
            messages=messages or [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "do work"},
                {"role": "assistant", "content": "done"},
            ],
            session_log=log,
            stats=fresh_stats(),
            console=Console(color=False),
        )

    def test_memory_mine_cli_uses_count(self):
        with patch("lmloop.cli.cmd_memory_mine", return_value=0) as mine:
            code = main(["memory", "mine", "5"])
        self.assertEqual(code, 0)
        self.assertEqual(mine.call_args[0][1], 5)

    def test_retro_cli_alias_hints_and_mines(self):
        with patch("lmloop.cli.cmd_memory_mine", return_value=0) as mine:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["retro"])
        self.assertEqual(code, 0)
        self.assertEqual(mine.call_args[0][1], 3)
        self.assertIn("memory mine", buf.getvalue())

    def test_memory_mine_n_uses_prior_files(self):
        from lmloop.repl import _cmd_memory_mine
        import json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions = root / "sessions"
            sessions.mkdir()
            prior1 = sessions / "20250814-010000.jsonl"
            prior2 = sessions / "20250814-020000.jsonl"
            current = sessions / "20250814-030000.jsonl"
            for p, text in ((prior1, "one"), (prior2, "two")):
                p.write_text(json.dumps({"role": "user", "content": text}) + "\n")
            current.write_text("")
            state = self._state(root, current)
            with patch("lmloop.repl.mine_sessions") as mine, \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 redirect_stdout(io.StringIO()):
                _cmd_memory_mine(state, "2", lambda _: False)
            mine.assert_called_once()
            paths = mine.call_args[0][2]
            self.assertEqual([p.resolve() for p in paths], [prior1.resolve(), prior2.resolve()])

    def test_until_cli_runs_goal(self):
        fake = Mock()
        fake.path = Path("/tmp/until.jsonl")
        fake.is_paused.return_value = False
        with patch("lmloop.cli.agent.ensure_server", return_value="m"), \
             patch("lmloop.cli.loop_mod.UntilRun.create", return_value=fake) as create, \
             patch("lmloop.cli.loop_mod.run_until") as run, \
             patch("lmloop.cli.loop_mod.UntilRun.load", return_value=fake), \
             patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(io.StringIO()):
            code = main(["until", "--check", "true", "green"])
        self.assertEqual(code, 0)
        create.assert_called_once_with("green", check_cmd="true")
        run.assert_called_once()

    def test_until_empty_resumes_open_run(self):
        fake = Mock()
        fake.path = Path("/tmp/until.jsonl")
        fake.is_paused.return_value = True
        with patch("lmloop.cli.agent.ensure_server", return_value="m"), \
             patch("lmloop.cli.loop_mod.latest_open_until_run", return_value=fake), \
             patch("lmloop.cli.loop_mod.run_until") as run, \
             patch("lmloop.cli.loop_mod.UntilRun.load", return_value=fake), \
             patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(io.StringIO()):
            code = main(["until"])
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_until_cli_tty_enters_repl(self):
        fake = Mock()
        fake.path = Path("/tmp/until.jsonl")
        fake.is_paused.return_value = False
        with patch("lmloop.cli.agent.ensure_server", return_value="m"), \
             patch("lmloop.cli.loop_mod.UntilRun.create", return_value=fake), \
             patch("lmloop.cli.loop_mod.run_until"), \
             patch("lmloop.cli.loop_mod.UntilRun.load", return_value=fake), \
             patch("lmloop.cli.loop_mod.superseded_until_hint", return_value=None), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("lmloop.cli.run_repl", return_value=0) as repl, \
             redirect_stdout(io.StringIO()):
            code = main(["until", "green"])
        self.assertEqual(code, 0)
        repl.assert_called_once()
        self.assertIs(repl.call_args.kwargs["until_run"], fake)

    def test_until_empty_errors_without_open_run(self):
        err = io.StringIO()
        with patch("lmloop.cli.loop_mod.latest_open_until_run", return_value=None), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = main(["until"])
        self.assertEqual(code, 1)
        self.assertIn("usage", err.getvalue())

    def test_continue_resumes_paused_until(self):
        from lmloop.loop import UntilRun
        from lmloop.repl import _cmd_continue

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root):
                run = UntilRun.create("ship it")
                run.append("pause", "paused")
            log = root / "s.jsonl"
            log.write_text("")
            state = self._state(root, log)
            state.until_run = run.path
            with patch("lmloop.repl._advance_until") as adv, \
                 redirect_stdout(io.StringIO()):
                _cmd_continue(state, "", lambda _: False)
            adv.assert_called_once()
            self.assertEqual(adv.call_args[0][1].path, run.path)

    def test_continue_without_until_runs_turn(self):
        from lmloop.repl import _cmd_continue

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s.jsonl"
            log.write_text("")
            state = self._state(Path(d), log)
            with patch("lmloop.repl._run_turn") as turn, \
                 redirect_stdout(io.StringIO()):
                _cmd_continue(state, "", lambda _: False)
            turn.assert_called_once()

    def test_until_appends_handoff_to_live_thread(self):
        from lmloop.loop import UntilRun
        from lmloop.repl import _UNTIL_FOLLOWUP, attach_until_result

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            log = root / "s.jsonl"
            log.write_text("")
            with patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root):
                run = UntilRun.create("ship it")
                run.append("eval", "pass", handoff="wrote findings.md")
                run.append("done", "pass")
                state = self._state(root, log)
                prior = len(state.messages)
                with redirect_stdout(io.StringIO()):
                    attach_until_result(state, run)
            self.assertEqual(len(state.messages), prior + 2)
            self.assertIn("ship it", state.messages[-2]["content"])
            self.assertIn("wrote findings.md", state.messages[-2]["content"])
            self.assertEqual(state.messages[-1]["content"], _UNTIL_FOLLOWUP)
            self.assertIsNone(state.until_run)


    def test_graph_cli_requires_name_or_open_run(self):
        err = io.StringIO()
        with patch("lmloop.cli.graph_mod.latest_open_graph_run", return_value=None), \
             patch("lmloop.cli.graph_mod.list_graphs", return_value=["company"]), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = main(["graph"])
        self.assertEqual(code, 1)
        self.assertIn("usage", err.getvalue())

    def test_graph_empty_resumes_open_run(self):
        fake = Mock()
        fake.path = Path("/tmp/graphs/company/run.jsonl")
        fake.name = "company"
        fake.is_paused.return_value = True
        defn = Mock()
        with patch("lmloop.cli.agent.ensure_server", return_value="m"), \
             patch("lmloop.cli.graph_mod.latest_open_graph_run", return_value=fake), \
             patch("lmloop.cli.graph_mod.load_graph", return_value=defn), \
             patch("lmloop.cli.graph_mod.run_graph") as run, \
             patch("lmloop.cli.graph_mod.GraphRun.load", return_value=fake), \
             patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(io.StringIO()):
            code = main(["graph"])
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_graph_cli_runs_named(self):
        fake = Mock()
        fake.path = Path("/tmp/graphs/company/run.jsonl")
        fake.name = "company"
        fake.is_paused.return_value = False
        defn = Mock()
        with patch("lmloop.cli.agent.ensure_server", return_value="m"), \
             patch("lmloop.cli.graph_mod.load_graph", return_value=defn), \
             patch("lmloop.cli.graph_mod.GraphRun.create", return_value=fake) as create, \
             patch("lmloop.cli.graph_mod.superseded_graph_hint", return_value=None), \
             patch("lmloop.cli.graph_mod.run_graph") as run, \
             patch("lmloop.cli.graph_mod.GraphRun.load", return_value=fake), \
             patch("sys.stdin.isatty", return_value=False), \
             redirect_stdout(io.StringIO()):
            code = main(["graph", "company"])
        self.assertEqual(code, 0)
        create.assert_called_once_with("company")
        run.assert_called_once()

    def test_continue_resumes_graph_over_until(self):
        from lmloop.graph import GraphRun
        from lmloop.loop import UntilRun
        from lmloop.repl import _cmd_continue

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.graph.project_dir", return_value=root), \
                 patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root):
                grun = GraphRun.create("company")
                grun.append("pause", "paused", node="ceo")
                urun = UntilRun.create("ship it")
                urun.append("pause", "paused")
            log = root / "s.jsonl"
            log.write_text("")
            state = self._state(root, log)
            state.graph_run = grun.path
            state.until_run = urun.path
            with patch("lmloop.repl.graph_mod.load_graph") as load, \
                 patch("lmloop.repl._advance_graph") as adv, \
                 redirect_stdout(io.StringIO()):
                _cmd_continue(state, "", lambda _: False)
            load.assert_called_once_with("company")
            adv.assert_called_once()
            self.assertEqual(adv.call_args[0][1].path, grun.path)


class RegistryTests(unittest.TestCase):
    def test_every_slash_meta_has_a_handler(self):
        from lmloop.commands import slash_command_metas
        cmds = _build_slash_commands(lambda _: False)
        names = {c.name.lstrip("/") for c in cmds}
        for meta in slash_command_metas():
            self.assertIn(meta.name, names, f"missing handler for /{meta.name}")

    def test_cli_subcommands_match_registry(self):
        from lmloop.cli import cli_handlers
        from lmloop.commands import cli_subcommand_names
        self.assertEqual(set(cli_subcommand_names()), set(cli_handlers()))

    def test_completion_script_lists_cli_stems(self):
        from lmloop.commands import cli_subcommand_names
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["completion", "zsh"])
        self.assertEqual(code, 0)
        script = out.getvalue()
        for name in cli_subcommand_names():
            self.assertIn(f"'{name}:", script)


class EnsureServerTests(unittest.TestCase):
    def test_uses_configured_model_when_loaded(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "mine",
               "auto_start_server": False}
        with patch("lmloop.agent.list_models", return_value=["other", "mine"]):
            self.assertEqual(agent.ensure_server(cfg, echo=lambda *_a: None), "mine")

    def test_falls_back_when_configured_model_missing(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "missing",
               "auto_start_server": False}
        notes = []
        with patch("lmloop.agent.list_models", return_value=["loaded"]):
            self.assertEqual(agent.ensure_server(cfg, echo=notes.append), "loaded")
        self.assertTrue(any("missing" in n and "loaded" in n for n in notes))

    def test_starts_server_when_empty(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "m",
               "auto_start_server": True}
        with patch("lmloop.agent.list_models", side_effect=[[], ["m"]]), \
             patch("lmloop.agent.shutil.which", return_value="/usr/bin/lms"), \
             patch("lmloop.agent._run_lms", return_value=True) as run, \
             patch("lmloop.agent.time.sleep"):
            self.assertEqual(agent.ensure_server(cfg, echo=lambda *_a: None), "m")
        run.assert_called()

    def test_raises_when_no_models(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "model": "",
               "auto_start_server": False}
        with patch("lmloop.agent.list_models", return_value=[]), \
             patch("lmloop.agent.shutil.which", return_value=None):
            with self.assertRaises(agent.ServerError) as ctx:
                agent.ensure_server(cfg, echo=lambda *_a: None)
            self.assertIn("No models available", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
