"""Tests for Ctrl+O prior-thinking history."""

import unittest
from pathlib import Path

from lmloop.repl import SessionState
from lmloop.ui import Console, drain_tty_input, fresh_stats


def _state() -> SessionState:
    return SessionState(
        cfg={},
        model="m",
        messages=[],
        session_log=Path("/tmp/lmloop-test-session.jsonl"),
        stats=fresh_stats(),
        console=Console(color=False),
    )


class PriorThinkingTests(unittest.TestCase):
    def test_prior_excludes_latest_and_is_oldest_first(self):
        st = _state()
        st.push_thinking("thought A")
        st.push_thinking("thought C")
        st.push_thinking("thought D latest")
        body = st.prior_thinking_transcript()
        self.assertIsNotNone(body)
        self.assertIn("thought A", body)
        self.assertIn("thought C", body)
        self.assertNotIn("thought D latest", body)
        # Oldest first: A before C
        self.assertLess(body.index("thought A"), body.index("thought C"))
        self.assertEqual(st.prior_thinking_count(), 2)

    def test_single_thinking_has_no_prior(self):
        st = _state()
        st.push_thinking("only this turn")
        self.assertIsNone(st.prior_thinking_transcript())
        self.assertEqual(st.prior_thinking_count(), 0)

    def test_empty_history(self):
        st = _state()
        self.assertIsNone(st.prior_thinking_transcript())

    def test_empty_push_is_ignored(self):
        st = _state()
        st.push_thinking("  \n")
        self.assertEqual(st.thinking_count(), 0)

    def test_drain_tty_input_safe_when_not_tty(self):
        self.assertEqual(drain_tty_input(), 0)


if __name__ == "__main__":
    unittest.main()
