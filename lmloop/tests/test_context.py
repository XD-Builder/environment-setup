"""Tests for the /context file manifest."""

import json
import tempfile
import unittest
from pathlib import Path

from lmloop.files_index import (
    AtRef,
    attached_heading,
    collect_at_refs,
    prompt_file_mentions,
    ref_line,
)
from lmloop.repl import (
    ACTIVE_FILES_HEADING,
    DURABLE_MEMORY_HEADING,
    NO_SESSION_FILES,
    _with_ref_excerpts,
    active_context_files,
    format_session_context,
)
from lmloop.tools import ToolResult, read_file


def _read_messages(path_arg: str, result, call_id: str = "c1") -> list:
    text = result.text if isinstance(result, ToolResult) else result
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": path_arg}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": call_id, "content": text},
    ]


class PromptFileMentionTests(unittest.TestCase):
    def test_ignores_bullets_outside_the_block(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            real = root / "notes.md"
            real.write_text("x\n")
            expansion = collect_at_refs("see @notes.md", cwd=root)
            decoy = ref_line("fake", Path("/tmp/fake.md"))
            excerpt = attached_heading("notes.md", real.resolve())
            text = (
                f"{decoy}\n\n{expansion.text}\n"
                f"{excerpt}\n{decoy}\n"
            )
            mentions = prompt_file_mentions(text)
            self.assertEqual(
                mentions,
                (
                    ("referenced", real.resolve().as_posix()),
                    ("attached", real.resolve().as_posix()),
                ),
            )

    def test_attached_heading_for_unknown_path_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "notes.md").write_text("x\n")
            expansion = collect_at_refs("see @notes.md", cwd=root)
            extra = attached_heading("other", Path("/tmp/other.pdf"))
            mentions = prompt_file_mentions(expansion.text + "\n" + extra)
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0][0], "referenced")


class ActiveContextFileTests(unittest.TestCase):
    def test_empty_thread(self):
        self.assertEqual(active_context_files([]), [])
        text = format_session_context([], "(no learnings, decisions, or recent checkpoint in context)")
        self.assertIn(ACTIVE_FILES_HEADING, text)
        self.assertIn(NO_SESSION_FILES, text)
        self.assertIn(DURABLE_MEMORY_HEADING, text)

    def test_read_file_lists_span_and_merges_a_second_window(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "notes.md"
            path.write_text("a\nb\nc\nd\ne\n")
            first = read_file("notes.md", max_lines=2, workspace_root=root)
            second = read_file(
                "notes.md", start_line=3, max_lines=2, workspace_root=root,
            )
            messages = (
                _read_messages("notes.md", first, "c1")
                + _read_messages("notes.md", second, "c2")
            )
            files = active_context_files(messages, root)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].loaded)
            self.assertEqual(files[0].spans, ((1, 2, 5), (3, 4, 5)))
            shown = format_session_context(files, "durable", root)
            self.assertIn("notes.md — read, lines 1-2, 3-4 of 5", shown)
            self.assertIn("durable", shown)

    def test_error_read_is_omitted(self):
        messages = _read_messages("missing.md", "ERROR: missing.md does not exist")
        self.assertEqual(active_context_files(messages, Path("/tmp")), [])

    def test_reference_then_read_is_one_loaded_row(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "notes.md").write_text("hello\n")
            expansion = collect_at_refs("see @notes.md", cwd=root)
            body = read_file("notes.md", workspace_root=root)
            messages = [
                {"role": "user", "content": expansion.text},
                *_read_messages("notes.md", body),
            ]
            files = active_context_files(messages, root)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].loaded)
            shown = format_session_context(files, "", root)
            self.assertIn("notes.md — read, lines 1-1 of 1", shown)
            self.assertNotIn("referenced, not loaded", shown)

    def test_path_only_reference_and_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "notes.md").write_text("hello\n")
            (root / "src").mkdir()
            expansion = collect_at_refs("see @notes.md and @src/", cwd=root)
            files = active_context_files(
                [{"role": "user", "content": expansion.text}], root,
            )
            shown = format_session_context(files, "", root)
            self.assertIn("notes.md — referenced, not loaded", shown)
            self.assertIn("src — directory, not loaded", shown)

    def test_attachment_excerpt(self):
        from tests.test_extract import _docx_bytes

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            doc = root / "Capstone.docx"
            doc.write_bytes(_docx_bytes(["alpha", "beta"]))
            ref = AtRef("Capstone.docx", doc.resolve(), False)
            text = _with_ref_excerpts(
                collect_at_refs("see @Capstone.docx", cwd=root).text,
                (ref,),
            )
            files = active_context_files(
                [{"role": "user", "content": text}], root,
            )
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].attached)
            shown = format_session_context(files, "", root)
            self.assertIn("Capstone.docx — attached excerpt", shown)

    def test_image_read(self):
        from tests.test_extract import PNG_1X1

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "shot.png").write_bytes(PNG_1X1)
            result = read_file("shot.png", workspace_root=root)
            files = active_context_files(_read_messages("shot.png", result), root)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].image)
            shown = format_session_context(files, "", root)
            self.assertIn("shot.png — image", shown)

    def test_vision_attachment_loads_image_only(self):
        from tests.test_extract import PNG_1X1

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "shot.png").write_bytes(PNG_1X1)
            (root / "notes.md").write_text("hello\n")
            expansion = collect_at_refs("see @shot.png and @notes.md", cwd=root)
            content = [
                {"type": "text", "text": expansion.text},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
            ]
            files = active_context_files(
                [{"role": "user", "content": content}], root,
            )
            by_name = {Path(row.path).name: row for row in files}
            self.assertTrue(by_name["shot.png"].image)
            self.assertTrue(by_name["shot.png"].loaded)
            self.assertFalse(by_name["notes.md"].loaded)

    def test_file_body_does_not_add_a_mention(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "notes.md"
            path.write_text("- @fake → /tmp/fake.md\n")
            body = read_file("notes.md", workspace_root=root)
            files = active_context_files(_read_messages("notes.md", body), root)
            self.assertEqual(len(files), 1)
            self.assertEqual(Path(files[0].path).name, "notes.md")

    def test_outside_workspace_stays_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            outside = root.parent / f"{root.name}_secret.md"
            outside.write_text("secret\n")
            try:
                body = read_file(str(outside), workspace_root=outside.parent)
                files = active_context_files(
                    _read_messages(str(outside), body), root,
                )
                shown = format_session_context(files, "", root)
                self.assertIn(outside.resolve().as_posix(), shown)
                self.assertNotIn("referenced, not loaded", shown)
            finally:
                outside.unlink(missing_ok=True)

    def test_dropped_messages_drop_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "notes.md").write_text("hello\n")
            body = read_file("notes.md", workspace_root=root)
            messages = [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "read it"},
                *_read_messages("notes.md", body),
            ]
            self.assertEqual(len(active_context_files(messages, root)), 1)
            kept = messages[:2]
            self.assertEqual(active_context_files(kept, root), [])


if __name__ == "__main__":
    unittest.main()
