"""Tests for project path index and @ref expansion."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.files_index import clear_path_cache, expand_at_refs, list_project_paths
from lmloop.prompt import LmloopCompleter, _current_token, _in_skill_arg, _short_blurb
from lmloop.repl import SlashCommand
from prompt_toolkit.document import Document


class FilesIndexTests(unittest.TestCase):
    def setUp(self):
        clear_path_cache()

    def test_walk_fallback_lists_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x\n")
            (root / "sub").mkdir()
            (root / "sub" / "b.py").write_text("y\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "x.js").write_text("z\n")
            with patch("lmloop.files_index._git_paths", return_value=None):
                paths = list_project_paths(root)
            self.assertIn("a.py", paths)
            self.assertIn("sub/b.py", paths)
            self.assertNotIn("node_modules/x.js", paths)

    def test_expand_at_refs_appends_existing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "app.ts").write_text("ok\n")
            out = expand_at_refs("look at @src/app.ts please", cwd=root)
            self.assertIn("look at @src/app.ts please", out)
            self.assertIn("Referenced files (use read_file):", out)
            self.assertIn("- src/app.ts", out)

    def test_expand_skips_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            out = expand_at_refs("see @nope.py", cwd=root)
            self.assertEqual(out, "see @nope.py")

    def test_expand_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root.parent / f"{root.name}_secret.txt"
            outside.write_text("secret\n")
            try:
                rel = Path(os.path.relpath(outside, root)).as_posix()
                out = expand_at_refs(f"see @{rel}", cwd=root)
                self.assertEqual(out, f"see @{rel}")
                self.assertNotIn("Referenced files", out)
            finally:
                outside.unlink(missing_ok=True)

    def test_expand_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root.parent / f"{root.name}_link_target.txt"
            outside.write_text("secret\n")
            link = root / "escape.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks not supported")
            try:
                out = expand_at_refs("see @escape.txt", cwd=root)
                self.assertEqual(out, "see @escape.txt")
            finally:
                link.unlink(missing_ok=True)
                outside.unlink(missing_ok=True)

    def test_expand_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x\n")
            once = expand_at_refs("look @a.py", cwd=root)
            twice = expand_at_refs(once, cwd=root)
            self.assertEqual(once, twice)
            self.assertEqual(once.count("Referenced files"), 1)


class CompleterTests(unittest.TestCase):
    def test_in_skill_arg(self):
        self.assertTrue(_in_skill_arg("/skill inv", "inv"))
        self.assertTrue(_in_skill_arg("/skill ", ""))
        self.assertFalse(_in_skill_arg("/skill", "/skill"))
        self.assertFalse(_in_skill_arg("/skills", "/skills"))
        self.assertFalse(_in_skill_arg("/ceo", "/ceo"))

    def test_filter_keeps_c_prefix(self):
        from lmloop.prompt import _filter_names
        names = ["/ceo", "/compact", "/help", "/checkpoints"]
        got = _filter_names(names, "/c")
        self.assertEqual(got, ["/ceo", "/compact", "/checkpoints"])

    def test_current_token_and_blurb(self):
        self.assertEqual(_current_token("see /ce"), "/ce")
        self.assertEqual(_current_token("@src/"), "@src/")
        self.assertEqual(
            _short_blurb("/ceo", "Skill: ceo — strategy and plan review"),
            "strategy and plan review",
        )

    def test_slash_completions_include_meta(self):
        cmds = [
            SlashCommand("/ceo", "strategy review", lambda *_: True),
            SlashCommand("/help", "show help", lambda *_: True),
        ]
        c = LmloopCompleter(lambda: cmds)
        docs = Document("/ce", cursor_position=3)
        matches = list(c.get_completions(docs, None))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].text, "/ceo")
        self.assertEqual(matches[0].display_meta_text, "strategy review")

    def test_at_completions(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "readme.md").write_text("x\n")
            clear_path_cache()
            with patch("lmloop.prompt.list_project_paths", return_value=["readme.md", "src/"]):
                c = LmloopCompleter(lambda: [])
                docs = Document("@re", cursor_position=3)
                matches = list(c.get_completions(docs, None))
            texts = [m.text for m in matches]
            self.assertIn("@readme.md", texts)


if __name__ == "__main__":
    unittest.main()
