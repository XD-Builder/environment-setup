"""Tests for always-on steering load order and the live clock."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lmloop import agent, steer as steer_mod
from lmloop.steer import (
    _invalid_steer_name,
    clock_block,
    clock_snapshot,
    load_steering,
    steer_dirs,
    steering_block,
)


class SteerNameTests(unittest.TestCase):
    def test_rejects_path_like_and_private_names(self):
        self.assertTrue(_invalid_steer_name(""))
        self.assertTrue(_invalid_steer_name("../etc"))
        self.assertTrue(_invalid_steer_name("foo/bar"))
        self.assertTrue(_invalid_steer_name("foo\\bar"))
        self.assertTrue(_invalid_steer_name(".hidden"))
        self.assertTrue(_invalid_steer_name("_secret"))
        self.assertTrue(_invalid_steer_name("a..b"))
        self.assertFalse(_invalid_steer_name("time"))
        self.assertFalse(_invalid_steer_name("development"))


class ClockTests(unittest.TestCase):
    def test_clock_snapshot_lookbacks(self):
        fixed = datetime(2026, 8, 14, 17, 6, 0, tzinfo=timezone.utc)
        snap = clock_snapshot(now=fixed)
        self.assertEqual(snap["utc"], "2026-08-14T17:06:00Z")
        self.assertEqual(snap["today"], "2026-08-14")
        self.assertEqual(snap["7_days_ago"], "2026-08-07")
        self.assertEqual(snap["28_days_ago"], "2026-07-17")
        self.assertEqual(snap["90_days_ago"], "2026-05-16")
        self.assertIn("local", snap)

    def test_naive_datetime_treated_as_utc(self):
        snap = clock_snapshot(now=datetime(2026, 8, 14, 17, 6, 0))
        self.assertEqual(snap["utc"], "2026-08-14T17:06:00Z")

    def test_clock_block_contains_patched_now(self):
        fixed = datetime(2026, 8, 14, 17, 6, 0, tzinfo=timezone.utc)
        text = clock_block(now=fixed)
        self.assertTrue(text.startswith("## Clock"))
        self.assertIn("2026-08-14T17:06:00Z", text)
        self.assertIn("2026-08-14", text)
        self.assertIn("current_time", text)


class SteerLoadTests(unittest.TestCase):
    def test_steer_dirs_order(self):
        dirs = steer_dirs()
        self.assertEqual(dirs[0], steer_mod.STEER_DIR)
        self.assertEqual(dirs[1], steer_mod.USER_STEER_DIR)
        self.assertEqual(len(dirs), 2)
        root = Path("/tmp/ws")
        with_proj = steer_dirs(root)
        self.assertEqual(with_proj[2], root / ".lmloop" / "steer")

    def test_skip_missing_dirs(self):
        missing = Path("/no-such-lmloop-steer-dir")
        with patch.object(steer_mod, "USER_STEER_DIR", missing):
            text = load_steering(Path("/no-such-workspace"))
        self.assertIn("# Time — relative windows", text)
        self.assertIn("# Memory — the current question wins", text)

    def test_concat_order_packaged_user_project(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pkg = root / "pkg"
            user = root / "user"
            ws = root / "ws"
            project = ws / ".lmloop" / "steer"
            pkg.mkdir()
            user.mkdir()
            project.mkdir(parents=True)
            (pkg / "a.md").write_text("# Packaged A\n")
            (user / "b.md").write_text("# User B\n")
            (project / "c.md").write_text("# Project C\n")
            with patch.object(steer_mod, "STEER_DIR", pkg), \
                 patch.object(steer_mod, "USER_STEER_DIR", user):
                text = load_steering(ws)
            self.assertLess(text.find("Packaged A"), text.find("User B"))
            self.assertLess(text.find("User B"), text.find("Project C"))

    def test_same_name_files_are_additive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pkg = root / "pkg"
            user = root / "user"
            pkg.mkdir()
            user.mkdir()
            (pkg / "time.md").write_text("# Packaged time\n")
            (user / "time.md").write_text("# User time\n")
            with patch.object(steer_mod, "STEER_DIR", pkg), \
                 patch.object(steer_mod, "USER_STEER_DIR", user):
                text = load_steering()
            self.assertIn("Packaged time", text)
            self.assertIn("User time", text)
            self.assertLess(text.find("Packaged time"), text.find("User time"))

    def test_skips_private_and_empty_files(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "pkg"
            pkg.mkdir()
            (pkg / "_secret.md").write_text("# Hidden should skip\n")
            (pkg / "empty.md").write_text("  \n")
            (pkg / "keep.md").write_text("# Keep me\n")
            with patch.object(steer_mod, "STEER_DIR", pkg), \
                 patch.object(steer_mod, "USER_STEER_DIR", Path(d) / "missing"):
                text = load_steering()
            self.assertEqual(text, "# Keep me")
            self.assertNotIn("Hidden", text)

    def test_empty_when_no_files(self):
        with tempfile.TemporaryDirectory() as d:
            empty = Path(d)
            with patch.object(steer_mod, "STEER_DIR", empty / "pkg"), \
                 patch.object(steer_mod, "USER_STEER_DIR", empty / "user"):
                self.assertEqual(load_steering(), "")
                self.assertEqual(steering_block(), "")

    def test_steering_block_wraps_heading(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "pkg"
            pkg.mkdir()
            (pkg / "time.md").write_text("# Time rule\n")
            with patch.object(steer_mod, "STEER_DIR", pkg), \
                 patch.object(steer_mod, "USER_STEER_DIR", Path(d) / "missing"):
                block = steering_block()
            self.assertTrue(block.startswith("## Steering (always apply)"))
            self.assertIn("# Time rule", block)


class SystemPromptSteerTests(unittest.TestCase):
    def test_system_prompt_includes_clock_and_packaged_steer(self):
        missing = Path("/no-such-lmloop-steer-user")
        with patch.object(steer_mod, "USER_STEER_DIR", missing), \
             patch("lmloop.steer.clock_block", return_value="## Clock\n\nNOW"), \
             patch("lmloop.memory.context_block", return_value=""):
            text = agent.system_prompt({})
        self.assertIn("## Clock\n\nNOW", text)
        self.assertIn("## Steering (always apply)", text)
        self.assertIn("# Time — relative windows", text)
        self.assertIn("`current_time`", text)

    def test_system_prompt_passes_workspace_root(self):
        root = Path("/tmp/proj")
        with patch("lmloop.steer.clock_block", return_value="## Clock\n\nx"), \
             patch("lmloop.steer.steering_block", return_value="") as sb, \
             patch("lmloop.memory.context_block", return_value=""):
            agent.system_prompt({}, workspace_root=root)
        sb.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main()
