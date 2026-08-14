"""Tests for markdown transcript helpers."""

import unittest

from lmloop.markdown_view import make_console, messages_to_markdown, render_markdown_ansi


class MarkdownViewTests(unittest.TestCase):
    def test_messages_to_markdown_skips_system(self):
        md = messages_to_markdown([
            {"role": "system", "content": "secret"},
            {"role": "user", "content": "hello **world**"},
            {"role": "assistant", "content": "# Title\n\n- one"},
        ])
        self.assertIn("# Session transcript", md)
        self.assertIn("## User", md)
        self.assertIn("hello **world**", md)
        self.assertIn("## Assistant", md)
        self.assertIn("# Title", md)
        self.assertNotIn("secret", md)

    def test_render_markdown_ansi_produces_output(self):
        out = render_markdown_ansi("# Hello\n\n**bold**", width=80)
        self.assertTrue(out.strip())
        self.assertIn("Hello", out)

    def test_render_markdown_wraps_instead_of_cropping(self):
        from lmloop.markdown_view import _rich_available
        if not _rich_available():
            self.skipTest("rich not installed")
        text = (
            "First sentence is here. Second sentence continues the thought "
            "and should remain visible on screen even though it is long. "
            "Third sentence is the one users miss."
        )
        out = render_markdown_ansi(text, width=40, color=False)
        self.assertIn("Third sentence", out)
        self.assertIn("continues the thought", out)
        self.assertGreater(out.count("\n"), 1)

    def test_make_console_soft_wrap_default_off_for_live(self):
        from io import StringIO
        from lmloop.markdown_view import _rich_available
        if not _rich_available():
            self.skipTest("rich not installed")
        console = make_console(color=False, file=StringIO(), force_terminal=True)
        self.assertFalse(console.soft_wrap)
        reflow = make_console(
            color=False, file=StringIO(), force_terminal=True, soft_wrap=True,
        )
        self.assertTrue(reflow.soft_wrap)


if __name__ == "__main__":
    unittest.main()
