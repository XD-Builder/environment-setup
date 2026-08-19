"""Tests for project path index and @ref expansion."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.files_index import (
    collect_at_refs,
    clear_path_cache,
    expand_at_refs,
    list_fs_paths,
    list_project_paths,
)
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
            target = root / "src" / "app.ts"
            target.write_text("ok\n")
            out = expand_at_refs("look at @src/app.ts please", cwd=root)
            self.assertIn("look at @src/app.ts please", out)
            self.assertIn("Referenced files (use read_file / list_dir", out)
            self.assertIn(f"- @src/app.ts → {target.resolve().as_posix()}", out)

    def test_expand_dot_relative(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "a.py"
            target.write_text("x\n")
            out = expand_at_refs("see @./a.py", cwd=root)
            self.assertIn(f"- @./a.py → {target.resolve().as_posix()}", out)

    def test_expand_home(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            target = home / "notes.md"
            target.write_text("ok\n")
            cwd = Path(d) / "proj"
            cwd.mkdir()
            with patch.dict(os.environ, {"HOME": str(home)}):
                out = expand_at_refs("see @~/notes.md", cwd=cwd)
            self.assertIn("@~/notes.md →", out)
            self.assertIn(target.resolve().as_posix(), out)

    def test_expand_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d) / "proj"
            cwd.mkdir()
            target = Path(d) / "outside.txt"
            target.write_text("x\n")
            out = expand_at_refs(f"see @{target}", cwd=cwd)
            self.assertIn(target.resolve().as_posix(), out)

    def test_expand_parent_relative(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d) / "proj"
            cwd.mkdir()
            target = Path(d) / "sibling.txt"
            target.write_text("x\n")
            out = expand_at_refs("see @../sibling.txt", cwd=cwd)
            self.assertIn("@../sibling.txt →", out)
            self.assertIn(target.resolve().as_posix(), out)

    def test_expand_quoted_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "my file.txt"
            target.write_text("x\n")
            out = expand_at_refs('see @"my file.txt"', cwd=root)
            self.assertIn("@my file.txt →", out)
            self.assertIn(target.resolve().as_posix(), out)

    def test_expand_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            out = expand_at_refs("see @src/", cwd=root)
            self.assertIn("@src/", out)
            self.assertIn((root / "src").resolve().as_posix(), out)

    def test_expand_skips_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            expansion = collect_at_refs("see @nope.py", cwd=root)
            self.assertEqual(expansion.text, "see @nope.py")
            self.assertEqual(expansion.missing, ("nope.py",))
            self.assertEqual(expansion.refs, ())

    def test_expand_includes_user_named_outside_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root.parent / f"{root.name}_secret.txt"
            outside.write_text("secret\n")
            try:
                rel = Path(os.path.relpath(outside, root)).as_posix()
                expansion = collect_at_refs(f"see @{rel}", cwd=root)
                self.assertIn("Referenced files", expansion.text)
                self.assertIn(outside.resolve().as_posix(), expansion.text)
                self.assertEqual(len(expansion.refs), 1)
                self.assertEqual(expansion.refs[0].resolved, outside.resolve())
            finally:
                outside.unlink(missing_ok=True)

    def test_expand_follows_symlink_named_by_user(self):
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
                expansion = collect_at_refs("see @escape.txt", cwd=root)
                self.assertIn("Referenced files", expansion.text)
                self.assertIn(outside.resolve().as_posix(), expansion.text)
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

    def test_list_fs_paths_home_and_parent(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            (home / "notes.md").write_text("x\n")
            (home / "Documents").mkdir()
            cwd = Path(d) / "proj"
            cwd.mkdir()
            (Path(d) / "sib.py").write_text("x\n")
            with patch.dict(os.environ, {"HOME": str(home)}):
                home_hits = list_fs_paths("~/no", cwd)
                parent_hits = list_fs_paths("../sib", cwd)
            self.assertIn("~/notes.md", home_hits)
            self.assertIn("../sib.py", parent_hits)


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
                c = LmloopCompleter(lambda: [], cwd_provider=lambda: root)
                docs = Document("@re", cursor_position=3)
                matches = list(c.get_completions(docs, None))
            texts = [m.text for m in matches]
            self.assertIn("@readme.md", texts)

    def test_at_home_completions_show_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            target = home / "notes.md"
            target.write_text("x\n")
            cwd = Path(d) / "proj"
            cwd.mkdir()
            with patch.dict(os.environ, {"HOME": str(home)}):
                c = LmloopCompleter(lambda: [], cwd_provider=lambda: cwd)
                docs = Document("@~/no", cursor_position=5)
                matches = list(c.get_completions(docs, None))
            texts = [m.text for m in matches]
            self.assertIn("@~/notes.md", texts)
            meta = {m.text: m.display_meta_text for m in matches}
            self.assertEqual(meta["@~/notes.md"], str(target.resolve()))

    def test_at_parent_completions(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d) / "proj"
            cwd.mkdir()
            target = Path(d) / "other.py"
            target.write_text("x\n")
            c = LmloopCompleter(lambda: [], cwd_provider=lambda: cwd)
            docs = Document("@../oth", cursor_position=7)
            matches = list(c.get_completions(docs, None))
            texts = [m.text for m in matches]
            self.assertIn("@../other.py", texts)
            meta = {m.text: m.display_meta_text for m in matches}
            self.assertEqual(meta["@../other.py"], str(target.resolve()))


if __name__ == "__main__":
    unittest.main()
