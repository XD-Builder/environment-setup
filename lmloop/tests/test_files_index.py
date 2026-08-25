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
    join_typed_path,
    list_fs_paths,
    list_project_paths,
    normalize_typed_path,
)
from lmloop.prompt import (
    LmloopCompleter,
    _current_token,
    _in_skill_arg,
    _lex_prompt_line,
    _short_blurb,
)
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

    def test_normalize_collapses_duplicate_slashes(self):
        self.assertEqual(
            normalize_typed_path("~//Downloads//etc//z.zip"),
            "~/Downloads/etc/z.zip",
        )
        self.assertEqual(normalize_typed_path("~/Downloads/etc/z.zip"), "~/Downloads/etc/z.zip")
        self.assertEqual(normalize_typed_path("//host/share"), "//host/share")
        self.assertEqual(normalize_typed_path("file:///tmp//a"), "file:///tmp/a")

    def test_join_typed_path_does_not_double_slashes(self):
        self.assertEqual(join_typed_path("~/", "/Downloads", True), "~/Downloads/")
        self.assertEqual(join_typed_path("~/", "Downloads", True), "~/Downloads/")
        self.assertEqual(join_typed_path("~/Downloads/", "etc", True), "~/Downloads/etc/")
        self.assertEqual(join_typed_path("~/Downloads/etc/", "z.zip", False), "~/Downloads/etc/z.zip")

    def test_expand_collapses_doubled_home_path(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            nested = home / "Downloads" / "etc"
            nested.mkdir(parents=True)
            target = nested / "z.zip"
            target.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            cwd = Path(d) / "proj"
            cwd.mkdir()
            with patch.dict(os.environ, {"HOME": str(home)}):
                expansion = collect_at_refs("see @~//Downloads//etc//z.zip", cwd=cwd)
            self.assertEqual(len(expansion.refs), 1)
            self.assertEqual(expansion.refs[0].token, "~/Downloads/etc/z.zip")
            self.assertIn("@~/Downloads/etc/z.zip →", expansion.text)
            self.assertNotIn("~//", expansion.text)


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

    def test_lex_at_path_is_colored_without_doubling_slashes(self):
        text = "see @~/Downloads/etc/z.zip please"
        frags = _lex_prompt_line(text)
        self.assertEqual("".join(t for _, t in frags), text)
        at = [(s, t) for s, t in frags if t.startswith("@")]
        self.assertEqual(len(at), 1)
        self.assertEqual(at[0][1], "@~/Downloads/etc/z.zip")
        self.assertEqual(at[0][0], "class:at-file")
        self.assertNotIn("//", "".join(t for _, t in frags))

    def test_lex_slash_and_at_dir(self):
        frags = _lex_prompt_line("/skill look @src/")
        joined = "".join(t for _, t in frags)
        self.assertEqual(joined, "/skill look @src/")
        styles = {t: s for s, t in frags}
        self.assertEqual(styles.get("/skill"), "class:slash")
        self.assertEqual(styles.get("@src/"), "class:at-dir")


if __name__ == "__main__":
    unittest.main()
