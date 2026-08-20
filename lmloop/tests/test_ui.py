"""Tests for lmloop terminal UI helpers."""

import unittest
from pathlib import Path
from unittest.mock import patch

from lmloop.ui import Console, context_bar, context_fill, fresh_stats


class UiTests(unittest.TestCase):
    def test_context_fill_uses_last_prompt(self):
        stats = fresh_stats()
        stats["last_prompt_tokens"] = 8000
        used, effective, ratio = context_fill(stats, [], 32000, 2048)
        self.assertEqual(used, 8000)
        self.assertEqual(effective, 32000 - 2048)
        self.assertGreater(ratio, 0.2)
        self.assertLess(ratio, 0.3)

    def test_context_bar_plain(self):
        bar = context_bar(8000, 32000, 0.25, theme=None)
        self.assertIn("█", bar)
        self.assertIn("8.0k", bar)

    def test_status_line_hidden_at_start(self):
        console = Console(color=False)
        self.assertIsNone(console.status_line(fresh_stats(), 0, 32000, [], 2048))

    def test_status_inline_omits_outer_brackets(self):
        console = Console(color=False)
        stats = fresh_stats()
        stats["total_tokens"] = 2200
        stats["last_prompt_tokens"] = 2200
        inline = console.status_line(stats, 1, 262100, [], 2048, inline=True)
        block = console.status_line(stats, 1, 262100, [], 2048, inline=False)
        self.assertIsNotNone(inline)
        self.assertIsNotNone(block)
        self.assertTrue(block.startswith("[") and block.endswith("]"))
        self.assertEqual(block[1:-1], inline)

    def test_visible_len_strips_ansi(self):
        from lmloop.ui import visible_len, strip_ansi
        raw = "\033[33m2.2k\033[0m"
        self.assertEqual(strip_ansi(raw), "2.2k")
        self.assertEqual(visible_len(raw), 4)

    def test_truncate_status_keeps_context_bar(self):
        console = Console(color=False)
        long_tail = "[████] 2.2k/262k (1%) · " + "x" * 80
        out = console._truncate_status(long_tail, 40)
        self.assertIn("[", out)
        self.assertLessEqual(len(out), 41)

    def test_prompt_is_readline_safe(self):
        from lmloop.ui import format_input_prompt, short_path

        cwd = Path("/home/u/demo")
        with patch("lmloop.ui.Path.cwd", return_value=cwd), \
             patch("lmloop.ui.Path.home", return_value=Path("/home/u")), \
             patch.object(Path, "resolve", lambda self: self):
            text = Console().prompt("org/my-model")
            self.assertEqual(short_path(Path("/home/u/proj")), "~/proj")
            self.assertEqual(format_input_prompt("org/my-model"), text)
        self.assertEqual(text, "~/demo · my-model › ")
        self.assertNotIn("\n", text)
        self.assertTrue(text.endswith(" › "))

    def test_confirm_prompt_embeds_command(self):
        console = Console(color=False)
        self.assertEqual(console.confirm_prompt(), "  run it? [y/N] ")
        line = console.confirm_prompt('git push -u origin HEAD && gh pr create')
        self.assertIn("git push", line)
        self.assertTrue(line.endswith("[y/N] "))

    def test_ask_yes_no_uses_plain_input(self):
        from lmloop.ui import ask_yes_no
        with patch("builtins.input", return_value="y") as mocked:
            self.assertTrue(ask_yes_no("  run it? [y/N] "))
            mocked.assert_called_once_with("  run it? [y/N] ")

    def test_ask_until_gate_interrupt_propagates(self):
        from lmloop.ui import ask_until_gate
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ask_until_gate("Blocked. Continue? [y/N] ")

    def test_terminal_size_never_raises(self):
        from lmloop.ui import terminal_size
        with patch("lmloop.ui.shutil.get_terminal_size", side_effect=OSError):
            cols, lines = terminal_size()
        self.assertGreater(cols, 0)
        self.assertGreater(lines, 0)

    def test_estimate_context_tokens_counts_content_and_tools(self):
        from lmloop.ui import estimate_context_tokens
        n = estimate_context_tokens([
            {"role": "user", "content": "abcd"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        ])
        self.assertGreaterEqual(n, 1)

    def test_estimate_skips_image_base64(self):
        from lmloop.ui import IMAGE_TOKEN_ESTIMATE, estimate_context_tokens
        n = estimate_context_tokens([{
            "role": "user",
            "content": [
                {"type": "text", "text": "ab"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 10000}},
            ],
        }])
        self.assertGreaterEqual(n, IMAGE_TOKEN_ESTIMATE)
        self.assertLess(n, IMAGE_TOKEN_ESTIMATE + 500)


if __name__ == "__main__":
    unittest.main()
