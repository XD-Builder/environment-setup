"""Tests for lmloop terminal UI helpers."""

import unittest

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
        self.assertEqual(Console().prompt(), "› ")
        self.assertNotIn("\n", Console().prompt())


if __name__ == "__main__":
    unittest.main()
