"""Tests for lmloop CLI routing, skills listing, and completion."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from lmloop import agent
from lmloop.cli import main
from lmloop.repl import _build_slash_commands


class SkillsDiscoveryTests(unittest.TestCase):
    def test_list_skills_excludes_system(self):
        names = agent.list_skills()
        self.assertIn("ceo", names)
        self.assertIn("investigate", names)
        self.assertNotIn("system", names)
        self.assertNotIn("_author", names)

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
        self.assertIn("/retro", names)
        # compact/retro stay fixed (special handlers), not duplicated
        self.assertEqual(sum(1 for c in cmds if c.name == "/compact"), 1)
        self.assertEqual(sum(1 for c in cmds if c.name == "/retro"), 1)


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

    def test_cli_skill_rejects_author(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["skill", "_author"])
        self.assertEqual(code, 1)
        self.assertIn("No skill", err.getvalue())


if __name__ == "__main__":
    unittest.main()
