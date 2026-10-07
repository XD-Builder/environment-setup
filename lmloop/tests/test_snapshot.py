"""Git workspace snapshots (temp-index refs under refs/lmloop/*)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from lmloop import snapshot


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("base\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "init")


class SnapshotTests(unittest.TestCase):
    def test_off_and_non_git(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "file.txt").write_text("x")
            off = snapshot.take_snapshot(
                {"autonomous_snapshot": "off"},
                root,
                run_label="run1",
                step=1,
            )
            self.assertEqual(off.note, "snapshots off")
            self.assertEqual(off.ref, "")
            nogit = snapshot.take_snapshot({}, root, run_label="run1", step=1)
            self.assertEqual(nogit.note, "not a git repository")

    def test_untracked_captured_index_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_repo(root)
            (root / "new.txt").write_text("hello")
            status_before = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            result = snapshot.take_snapshot({}, root, run_label="abc", step=1)
            self.assertTrue(result.ref.startswith(snapshot.REF_PREFIX))
            show = subprocess.run(
                ["git", "-C", str(root), "show", f"{result.ref}:new.txt"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(show.stdout, "hello")
            status_after = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(status_before, status_after)
            self.assertTrue((root / "new.txt").exists())

    def test_clean_tree_records_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_repo(root)
            result = snapshot.take_snapshot({}, root, run_label="clean", step=1)
            self.assertEqual(result.ref, "HEAD")
            listed = subprocess.run(
                ["git", "-C", str(root), "for-each-ref", snapshot.REF_PREFIX],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(listed, "")

    def test_oversize_untracked_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _init_repo(root)
            big = root / "big.bin"
            big.write_bytes(b"x" * (snapshot.SNAPSHOT_MAX_FILE_MB * 1024 * 1024 + 1))
            (root / "small.txt").write_text("ok")
            result = snapshot.take_snapshot({}, root, run_label="size", step=1)
            self.assertIn("big.bin", result.skipped)
            show_small = subprocess.run(
                ["git", "-C", str(root), "show", f"{result.ref}:small.txt"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(show_small.stdout, "ok")
            show_big = subprocess.run(
                ["git", "-C", str(root), "show", f"{result.ref}:big.bin"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(show_big.returncode, 0)


if __name__ == "__main__":
    unittest.main()
