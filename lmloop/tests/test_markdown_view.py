"""Tests for markdown transcript helpers."""

import unittest

from lmloop.markdown_view import messages_to_markdown, render_markdown_ansi


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


if __name__ == "__main__":
    unittest.main()
