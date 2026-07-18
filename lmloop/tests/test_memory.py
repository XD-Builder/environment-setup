"""Tests for lmloop memory helpers."""

import json
import tempfile
import unittest
from pathlib import Path

from lmloop.memory import (
    all_sessions,
    list_checkpoints,
    read_session,
    resolve_checkpoint,
    resolve_session,
    save_checkpoint,
    session_preview,
)


class MemoryTests(unittest.TestCase):
    def test_read_session_keeps_whole_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.jsonl"
            rows = [{"role": "user", "content": "x" * 100} for _ in range(50)]
            with path.open("w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            text = read_session(path, max_chars=500)
            self.assertTrue(text.startswith("[user]"))
            for line in text.splitlines():
                self.assertTrue(line.startswith("[user]"))

    def test_resolve_checkpoint_by_index_and_latest(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.memory.project_dir", return_value=root):
                save_checkpoint("first", "body one")
                save_checkpoint("second", "body two")
                files = list_checkpoints()
                self.assertEqual(len(files), 2)
                self.assertEqual(resolve_checkpoint("1"), files[0])
                self.assertEqual(resolve_checkpoint("latest"), files[-1])

    def test_resolve_session_global_index(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sessions = root / "sessions"
            sessions.mkdir()
            for i in range(5):
                (sessions / f"20250705-12000{i}.jsonl").write_text("")
            with patch("lmloop.memory.project_dir", return_value=root):
                all_rows = all_sessions()
                self.assertEqual(len(all_rows), 5)
                self.assertEqual(resolve_session("5"), all_rows[-1])
                self.assertEqual(resolve_session("1"), all_rows[0])

    def test_session_preview_first_user_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.jsonl"
            rows = [
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "fix the bug in auth"},
            ]
            with path.open("w") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            self.assertIn("fix the bug", session_preview(path))


if __name__ == "__main__":
    unittest.main()
