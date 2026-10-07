"""Git autonomous snapshots (DESIGN_SANDBOX_AND_VERIFICATION §1.6, task R1)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lmloop import snapshot


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


class SnapshotTests(unittest.TestCase):
    def _init_repo(self, root: Path) -> None:
        _git(root, "init")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        (root / "tracked.txt").write_text("v1\n")
        _git(root, "add", "tracked.txt")
        _git(root, "commit", "-m", "init")

    def test_snapshot_includes_untracked_not_only_head(self):
        """Revision-2 regression: untracked files must appear in the snapshot commit."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root)
            (root / "tracked.txt").write_text("v2\n")
            (root / "new.txt").write_text("brand new\n")
            cfg = {"autonomous_snapshot": "git"}
            before_status = _git(root, "status", "--porcelain").stdout
            result = snapshot.take_snapshot(root, cfg, "20260101-120000", 1)
            after_status = _git(root, "status", "--porcelain").stdout
            self.assertEqual(before_status, after_status)
            self.assertTrue(result.snapshot_ref.startswith("refs/lmloop/"))
            proc = _git(root, "ls-tree", "-r", result.tree)
            names = {line.split()[-1] for line in proc.stdout.splitlines()}
            self.assertIn("tracked.txt", names)
            self.assertIn("new.txt", names)

    def test_clean_tree_records_head_without_new_ref(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root)
            cfg = {"autonomous_snapshot": "git"}
            result = snapshot.take_snapshot(root, cfg, "run1", 1)
            self.assertEqual(result.snapshot_ref, "HEAD")
            proc = _git(
                root,
                "for-each-ref",
                "--format=%(refname)",
                snapshot.REF_PREFIX,
            )
            self.assertEqual((proc.stdout or "").strip(), "")

    def test_oversize_untracked_is_skipped_and_listed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root)
            big = root / "huge.bin"
            big.write_bytes(b"x" * (snapshot.SNAPSHOT_MAX_FILE_MB * 1024 * 1024 + 1))
            cfg = {"autonomous_snapshot": "git"}
            result = snapshot.take_snapshot(root, cfg, "run2", 1)
            self.assertIn("huge.bin", result.skipped)
            proc = _git(root, "ls-tree", "-r", result.tree)
            self.assertNotIn("huge.bin", proc.stdout)

    def test_non_git_workspace_note(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = snapshot.take_snapshot(root, {"autonomous_snapshot": "git"}, "r", 1)
            self.assertIn("not a git", result.note)

    def test_autonomous_snapshot_off(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._init_repo(root)
            result = snapshot.take_snapshot(root, {"autonomous_snapshot": "off"}, "r", 1)
            self.assertIn("off", result.note)
            self.assertEqual(result.snapshot_ref, "")


if __name__ == "__main__":
    unittest.main()
