"""Tests for streaming chat assembly, display rules, and act() integration."""

import io
import json
import unittest
import urllib.error
from unittest import mock

from lmloop import agent
from lmloop import display
from lmloop import stream as stream_mod
from lmloop.status import NUDGE_CONTINUE_TEXT


def _sse(*chunks) -> bytes:
    parts = []
    for c in chunks:
        if c is None:
            parts.append(b"data: [DONE]\n\n")
        else:
            parts.append(f"data: {json.dumps(c)}\n\n".encode())
    return b"".join(parts)


class FakeResp:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def readline(self):
        return self._buf.readline()

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class GeneratingIndicatorTests(unittest.TestCase):
    def test_clear_stops_further_ticks(self):
        """Usage/trailing SSE chunks must not resurrect the spinner."""
        ind = display._GeneratingIndicator()
        with mock.patch("sys.stdout") as out:
            out.isatty.return_value = True
            ind.tick()
            self.assertTrue(ind._shown)
            ind.clear()
            self.assertFalse(ind._shown)
            self.assertTrue(ind._stopped)
            out.write.reset_mock()
            ind.tick()
            out.write.assert_not_called()

    def test_clear_without_show_still_stops(self):
        ind = display._GeneratingIndicator()
        ind.clear()
        self.assertTrue(ind._stopped)
        with mock.patch("sys.stdout") as out:
            out.isatty.return_value = True
            ind.tick()
            out.write.assert_not_called()


class StreamPrinterTests(unittest.TestCase):
    def _collect(self):
        out = []
        return out, display._StreamPrinter(out.append)

    def test_skips_whitespace_only_rounds(self):
        out, p = self._collect()
        p.feed("\n\n")
        p.feed("  \n")
        self.assertFalse(p.finish())
        self.assertEqual(out, [])

    def test_strips_leading_blank_lines(self):
        out, p = self._collect()
        p.feed("\n\nHello")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "Hello\n")

    def test_collapses_trailing_newlines_to_one(self):
        out, p = self._collect()
        p.feed("Hello\n\n\n")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "Hello\n")

    def test_preserves_paragraph_break_across_chunks(self):
        """SSE often sends '\\n' as its own token; do not collapse those."""
        out, p = self._collect()
        p.feed("Hello\n")
        p.feed("\n")
        p.feed("World")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "Hello\n\nWorld\n")

    def test_open_live_display_modes(self):
        p, mode = display._open_live_display(False)
        self.assertEqual(mode, "silent")
        self.assertIsNone(p._write)
        p, mode = display._open_live_display(True)
        self.assertEqual(mode, "plain")
        with mock.patch("lmloop.display._rich_live_available", return_value=True):
            p, mode = display._open_live_display(None)
            self.assertEqual(mode, "markdown")
            self.assertIsInstance(p, display._MarkdownLivePrinter)
        with mock.patch("lmloop.display._rich_live_available", return_value=False):
            p, mode = display._open_live_display(None)
            self.assertEqual(mode, "plain")

    def test_emit_silent_calls_echo_once(self):
        echoed = []
        p = display._StreamPrinter(None)
        p.feed("**hi**")
        display._emit_assistant_content(echoed.append, "**hi**", p, "silent")
        self.assertEqual(echoed, ["**hi**"])

    def test_emit_live_plain_skips_echo(self):
        echoed = []
        out = []
        p = display._StreamPrinter(out.append)
        p.feed("hi")
        display._emit_assistant_content(echoed.append, "hi", p, "plain")
        self.assertEqual(echoed, [])
        self.assertEqual("".join(out), "hi\n")

    def test_emit_markdown_live_skips_echo(self):
        echoed = []
        p = display._MarkdownLivePrinter()
        with mock.patch.object(p, "_ensure_live"), \
             mock.patch.object(p, "_refresh"), \
             mock.patch.object(p, "finish", return_value=True):
            display._emit_assistant_content(echoed.append, "**hi**", p, "markdown")
        self.assertEqual(echoed, [])

    def test_emit_live_echoes_when_nothing_streamed(self):
        """Reasoning→content fallback never feeds the printer; must still show."""
        echoed = []
        p = display._StreamPrinter(lambda *_a: None)
        display._emit_assistant_content(
            echoed.append,
            "Let me dive deeper into the source code.",
            p,
            "plain",
        )
        self.assertEqual(
            echoed, ["Let me dive deeper into the source code."],
        )

    def test_markdown_live_crops_tail_not_ellipsis(self):
        """ellipsis keeps the start of a tall answer — new tokens vanish."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        p = display._MarkdownLivePrinter()
        fake_live = mock.MagicMock()
        fake_live.auto_refresh = False
        with mock.patch("lmloop.display._load_rich") as load_rich, \
             mock.patch("lmloop.markdown_view.make_console") as make_console, \
             mock.patch("lmloop.display.terminal_size", return_value=(80, 40)):
            Console = mock.MagicMock()
            make_console.return_value.soft_wrap = False
            make_console.return_value.width = 79
            Live = mock.MagicMock(return_value=fake_live)
            Markdown = mock.MagicMock(side_effect=lambda t: t)
            load_rich.return_value = (Console, Live, Markdown)
            p.feed("hello")
            self.assertTrue(Live.called)
            kwargs = Live.call_args.kwargs
            self.assertEqual(kwargs.get("vertical_overflow"), "crop")
            self.assertTrue(kwargs.get("transient"))
            self.assertTrue(make_console.call_args.kwargs.get("force_terminal"))
            self.assertEqual(make_console.call_args.kwargs.get("height"), 38)
            self.assertEqual(make_console.call_args.kwargs.get("width"), 79)
            updated = fake_live.update.call_args.args[0]
            self.assertIsInstance(updated, display._TailMarkdown)
            self.assertGreaterEqual(updated.min_rows, 0)

    def test_tail_markdown_keeps_newest_lines(self):
        """Live preview must show the end of a long answer, not the opening."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf, width=40, height=5, force_terminal=True,
            soft_wrap=False, highlight=False, color_system=None,
        )
        text = "# First heading\n\n" + ("filler paragraph\n\n" * 20) + "# Last heading\n"
        console.print(display._TailMarkdown(text))
        out = buf.getvalue()
        self.assertIn("Last heading", out)
        self.assertNotIn("First heading", out)

    def test_tail_markdown_keeps_opening_when_it_fits(self):
        """Short answers must grow in Live, not sit in a 16-line tail window."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf, width=40, height=20, force_terminal=True,
            soft_wrap=False, highlight=False, color_system=None,
        )
        console.print(display._TailMarkdown("# First heading\n\nA short paragraph.\n"))
        out = buf.getvalue()
        self.assertIn("First heading", out)
        self.assertIn("short paragraph", out)

    def test_live_preview_height_tracks_terminal(self):
        from lmloop.display import _live_preview_height
        with mock.patch("lmloop.display.terminal_size", return_value=(80, 40)):
            self.assertEqual(_live_preview_height(), 38)
        with mock.patch("lmloop.display.terminal_size", return_value=(80, 5)):
            self.assertEqual(_live_preview_height(), 4)

    def test_live_preview_width_leaves_wrap_margin(self):
        from lmloop.display import _live_preview_width
        with mock.patch("lmloop.display.terminal_size", return_value=(80, 40)):
            self.assertEqual(_live_preview_width(), 79)
        with mock.patch("lmloop.display.terminal_size", return_value=(1, 24)):
            self.assertEqual(_live_preview_width(), 1)

    def test_tail_markdown_never_shrinks_below_min_rows(self):
        """A reflow that renders fewer lines must still occupy the high-water rows."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf, width=40, height=20, force_terminal=True,
            soft_wrap=False, highlight=False, color_system=None,
        )
        tall = "# Title\n\n" + ("paragraph of content\n\n" * 4)
        n = display._TailMarkdown(tall).row_count(console)
        self.assertGreater(n, 3)
        short = display._TailMarkdown("# Short\n", min_rows=n)
        self.assertGreaterEqual(short.row_count(console), n)
        console.print(short)
        self.assertIn("Short", buf.getvalue())

    def test_markdown_live_high_water_does_not_shrink(self):
        """Printer keeps Live's occupied rows when markdown reflow gets shorter."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf, width=40, height=20, force_terminal=True,
            soft_wrap=False, highlight=False, color_system=None,
        )
        p = display._MarkdownLivePrinter(color=False, console=console)
        p.feed("# Title\n\n" + ("paragraph of content here\n\n" * 4))
        high = p._live_rows
        self.assertGreater(high, 1)
        p._parts = ["# Short\n"]
        p._refresh()
        self.assertGreaterEqual(p._live_rows, high)
        p.finish()

    def test_markdown_live_keeps_wrapped_sentences(self):
        """Live + word-wrap must not crop the next sentence at terminal width."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf, width=40, height=12, force_terminal=True,
            soft_wrap=False, highlight=False, color_system=None,
        )
        p = display._MarkdownLivePrinter(color=False, console=console)
        text = (
            "First sentence is here. Second sentence continues the thought "
            "and should remain visible on screen even though it is long. "
            "Third sentence is the one users miss."
        )
        p.feed(text)
        p.finish()
        out = buf.getvalue()
        self.assertIn("Third sentence", out)
        self.assertIn("continues the thought", out)

    def test_tail_markdown_keeps_list_tails(self):
        """Live list items must fold, not crop at terminal width."""
        if not display._load_rich():
            self.skipTest("rich not installed")
        from io import StringIO
        from rich.console import Console

        buf = StringIO()
        console = Console(
            file=buf, width=40, height=24, force_terminal=True,
            soft_wrap=False, highlight=False, color_system=None,
        )
        text = (
            "Saved:\n\n"
            "1. pitfall_ambiguous_numeric_abbreviation — \"59K\" is ambiguous "
            "(delivery count vs. $59,000 price). Always disambiguate early to "
            "avoid wasted searches.\n"
            "2. operational_geographic_split_queries — For geographic research, "
            "split web searches by region (e.g., \"West Coast California\" vs "
            "\"East Coast Florida\").\n"
        )
        console.print(display._TailMarkdown(text))
        plain = " ".join(buf.getvalue().split())
        self.assertIn("wasted searches", plain)
        self.assertIn("East Coast Florida", plain)

    def test_thinking_counts_wrapped_visual_rows(self):
        t = display._ThinkingLive(color=False)
        ts = type("TS", (), {"columns": 20, "lines": 24})()
        with mock.patch("sys.stdout") as out, \
             mock.patch("lmloop.display.terminal_size", return_value=(ts.columns, ts.lines)):
            out.isatty.return_value = True
            t._write_line("x" * 45)
        # "  ▎" prefix + 45 = 48 cells → 3 rows at width 20
        self.assertEqual(t._lines_shown, 3)

    def test_retire_for_round_promotes_thinking_only(self):
        t = display._ThinkingLive(color=False)
        with mock.patch("sys.stdout") as out:
            out.isatty.return_value = False
            t.feed("plan the next step\n")
            content, retired = t.retire_for_round([], "", "")
        self.assertEqual(content.strip(), "plan the next step")
        self.assertIn("plan the next step", retired)
        self.assertFalse(t.active)

    def _thinking_writes(self, feeds):
        t = display._ThinkingLive(color=False)
        writes = []
        ts = type("TS", (), {"columns": 80, "lines": 24})()
        with mock.patch("sys.stdout") as out, \
             mock.patch("lmloop.display.terminal_size", return_value=(ts.columns, ts.lines)):
            out.write.side_effect = lambda s: writes.append(s)
            out.isatty.return_value = True
            for piece in feeds:
                t.feed(piece)
            t.finish_keep()
        return "".join(writes), t

    def test_thinking_streams_live_text(self):
        text, t = self._thinking_writes([
            "first thought\n", "second thought\n",
        ])
        self.assertIn("first thought", text)
        self.assertIn("second thought", text)
        self.assertIn("first thought", t.text())

    def test_thinking_skips_consecutive_duplicate_lines(self):
        text, _t = self._thinking_writes([
            "same thought\n", "same thought\n", "same thought\n", "new thought\n",
        ])
        self.assertEqual(text.count("same thought"), 1)
        self.assertIn("new thought", text)

    def test_thinking_skips_near_duplicate_paraphrases(self):
        text, _t = self._thinking_writes([
            "Let me try the searches once more with different phrasing and also try fetching known authority sites directly.\n",
            "I'll retry the searches and try fetching known authority sites directly.\n",
            "Let me retry the searches with different phrasing and try fetching known authority sites.\n",
        ])
        self.assertEqual(text.count("authority sites"), 1)

    def test_thinking_skips_repeated_plan_block(self):
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        text, t = self._thinking_writes([block, block, block])
        self.assertEqual(text.count("Let me fetch the first 5 now:"), 1)
        self.assertEqual(t.text().count("Let me fetch the first 5 now:"), 3)

    def test_thinking_skips_consecutive_blank_lines(self):
        text, _t = self._thinking_writes(["idea\n", "\n", "\n", "\n", "next\n"])
        self.assertEqual(text.count(display.THINK_LINE_PREFIX + "\n"), 1)

    def test_thinking_carriage_return_rewrites_line(self):
        text, t = self._thinking_writes(["draft\rfinal\n"])
        self.assertIn("final", text)
        self.assertNotIn("\r", t.text())

    def test_thinking_erase_clears_rows(self):
        t = display._ThinkingLive(color=False)
        writes = []
        ts = type("TS", (), {"columns": 80, "lines": 24})()
        with mock.patch("sys.stdout") as out, \
             mock.patch("lmloop.display.terminal_size", return_value=(ts.columns, ts.lines)):
            out.write.side_effect = lambda s: writes.append(s)
            out.isatty.return_value = True
            t.feed("line one\nline two\n")
            saved = t.erase()
        joined = "".join(writes)
        self.assertIn("line one", joined)
        self.assertIn("\033[1A", joined)
        self.assertIn("line one", saved)
        self.assertFalse(t.active)

    def test_overlap_keeps_paragraph_break_newlines(self):
        parts = []
        stream_mod._ingest_text_delta(parts, "Hello\n")
        self.assertEqual(stream_mod._ingest_text_delta(parts, "\n"), "\n")
        self.assertEqual(stream_mod._ingest_text_delta(parts, "World\n"), "World\n")
        self.assertEqual("".join(parts), "Hello\n\nWorld\n")


class ContinueNudgeTests(unittest.TestCase):
    def test_detects_intent_only_after_tools(self):
        text = (
            "Let me dive deeper into the source code to understand "
            "the architecture and find potential improvements."
        )
        self.assertTrue(agent._should_nudge_continue(text, turn_tools=3, nudges=0, max_nudges=2))
        # First-round plan-only (no tools yet) should also nudge.
        self.assertTrue(agent._should_nudge_continue(text, turn_tools=0, nudges=0, max_nudges=2))
        self.assertFalse(agent._should_nudge_continue(text, turn_tools=3, nudges=2, max_nudges=2))

    def test_empty_after_tools_is_done_not_nudge(self):
        """Many models finish a successful tool turn with an empty final message."""
        self.assertFalse(agent._should_nudge_continue("", turn_tools=3, nudges=0, max_nudges=2))
        self.assertFalse(agent._should_nudge_continue("   ", turn_tools=3, nudges=0, max_nudges=2))

    def test_skips_real_answers(self):
        answer = (
            "Here are the top 3 improvements:\n"
            "1. Add caching\n2. Split the API layer\n3. Tighten types"
        )
        self.assertFalse(
            agent._should_nudge_continue(answer, turn_tools=5, nudges=0, max_nudges=2),
        )

    def test_skips_grounded_looking_at_answers(self):
        answer = (
            "Looking at the logs, the crash is a nil deref in main.go:42."
        )
        self.assertFalse(
            agent._should_nudge_continue(answer, turn_tools=3, nudges=0, max_nudges=2),
        )

    def test_detects_intent_after_status_preamble(self):
        """Local models often say 'No prior memory… Let me search' then stop."""
        text = (
            "No prior memory. Let me do fresh, thorough research. I'll search for:\n"
            "1. Karpathy's post about raising a child researcher\n"
            "2. Fairfax County public library early resources (0-3)\n"
            "3. Fairfax County public schools early childhood programs\n"
            "4. Herndon-specific resources\n"
            "\n"
            "Let me start with web searches."
        )
        self.assertTrue(
            agent._should_nudge_continue(text, turn_tools=2, nudges=0, max_nudges=2),
        )

    def test_act_nudges_when_model_narrates_without_tools(self):
        round1 = _sse(
            {"choices": [{"delta": {"content": "Looking"}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": "{}"},
            }]}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }},
            None,
        )
        # Soft-stop: intent narration, no tools (the bug from qwen sessions).
        round2 = _sse(
            {"choices": [{"delta": {
                "content": "Let me dive deeper into the source code.",
            }}]},
            {"choices": [], "usage": {
                "prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28,
            }},
            None,
        )
        round3 = _sse(
            {"choices": [{"delta": {"content": "Top idea: add tests."}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34,
            }},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True,
            "confirm_shell": False, "max_continue_nudges": 2,
        }
        messages = [{"role": "user", "content": "improve this"}]
        echoed = []
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[FakeResp(round1), FakeResp(round2), FakeResp(round3)],
        ), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", messages,
                echo=echoed.append, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
            )
        self.assertTrue(any("paused mid-task" in str(x) for x in echoed))
        self.assertTrue(any(m.get("content") == NUDGE_CONTINUE_TEXT for m in messages))
        self.assertIn("Top idea: add tests.", messages[-1].get("content", ""))


class MergeToolCallDeltaTests(unittest.TestCase):
    def test_accumulates_arguments_by_index(self):
        acc = {}
        stream_mod._merge_tool_call_delta(acc, [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_shell", "arguments": ""},
        }])
        stream_mod._merge_tool_call_delta(acc, [{
            "index": 0,
            "function": {"arguments": '{"command":'},
        }])
        stream_mod._merge_tool_call_delta(acc, [{
            "index": 0,
            "function": {"arguments": '"mkdir -p docs"}'},
        }])
        self.assertEqual(acc[0]["function"]["name"], "run_shell")
        self.assertEqual(acc[0]["function"]["arguments"], '{"command":"mkdir -p docs"}')


class IngestTextDeltaTests(unittest.TestCase):
    def test_incremental_appends(self):
        parts = []
        self.assertEqual(stream_mod._ingest_text_delta(parts, "Hello"), "Hello")
        self.assertEqual(stream_mod._ingest_text_delta(parts, " world"), " world")
        self.assertEqual("".join(parts), "Hello world")

    def test_cumulative_keeps_suffix_only(self):
        parts = []
        stream_mod._ingest_text_delta(parts, "Hello")
        self.assertEqual(stream_mod._ingest_text_delta(parts, "Hello world"), " world")
        self.assertEqual("".join(parts), "Hello world")

    def test_stale_shorter_snapshot_ignored(self):
        parts = []
        long = "Hello world, this is a longer cumulative snapshot body!!"
        stream_mod._ingest_text_delta(parts, long)
        # Near-complete prefix (>= 80% and >= 64 chars) is ignored as stale.
        stale = long[: max(64, len(long) * 4 // 5)]
        self.assertEqual(stream_mod._ingest_text_delta(parts, stale), "")
        self.assertEqual("".join(parts), long)

    def test_medium_prefix_replay_ignored(self):
        parts = []
        body = "Let me search the web for Bay Area history now.\n"
        stream_mod._ingest_text_delta(parts, body)
        stream_mod._ingest_text_delta(parts, body + "Then I'll summarize.\n")
        self.assertEqual(stream_mod._ingest_text_delta(parts, body), "")
        self.assertEqual("".join(parts).count(body.strip()), 1)

    def test_overlap_window_keeps_only_new_suffix(self):
        parts = []
        stream_mod._ingest_text_delta(parts, "AAA\nBBB\n")
        self.assertEqual(stream_mod._ingest_text_delta(parts, "BBB\nCCC\n"), "CCC\n")
        self.assertEqual("".join(parts), "AAA\nBBB\nCCC\n")

    def test_overlap_multiline_sliding_window(self):
        parts = []
        stream_mod._ingest_text_delta(parts, "AAA\nBBB\nCCC\n")
        self.assertEqual(
            stream_mod._ingest_text_delta(parts, "BBB\nCCC\nDDD\n"), "DDD\n",
        )
        self.assertEqual("".join(parts), "AAA\nBBB\nCCC\nDDD\n")

    def test_small_prefix_chunk_still_appends(self):
        parts = []
        stream_mod._ingest_text_delta(parts, "xxxxxxxxxxxxxxxxxxxx")
        # Short chunk that is a prefix must still append (incremental stream).
        self.assertEqual(stream_mod._ingest_text_delta(parts, "xxxx"), "xxxx")
        self.assertEqual("".join(parts), "xxxxxxxxxxxxxxxxxxxxxxxx")

    def test_tail_repeats(self):
        unit = "x" * stream_mod._REPEAT_WINDOW
        self.assertFalse(stream_mod._tail_repeats(unit * 2))
        self.assertTrue(stream_mod._tail_repeats(unit * stream_mod._REPEAT_TIMES))

    def test_repeated_trailing_lines(self):
        line = "The user asked about Bay Area history in detail."
        self.assertFalse(stream_mod._repeated_trailing_lines("\n".join([line] * 3)))
        self.assertTrue(stream_mod._repeated_trailing_lines("\n".join([line] * 6)))

    def test_think_line_similar_paraphrases(self):
        a = (
            "Let me try the searches once more with different phrasing "
            "and also try fetching known authority sites directly."
        )
        b = "I'll retry the searches and try fetching known authority sites directly."
        c = (
            "Let me retry the searches with different phrasing and try "
            "fetching known authority sites."
        )
        self.assertTrue(stream_mod._think_line_similar(a, b))
        self.assertTrue(stream_mod._think_line_similar(a, c))
        self.assertTrue(stream_mod._think_line_similar(b, c))
        self.assertFalse(stream_mod._think_line_similar(
            "New York works well for first-time visitors to the East Coast.",
            "Miami is better in winter for travelers who want beach weather.",
        ))

    def test_repeated_near_trailing_lines(self):
        paraphrases = [
            "Let me try the searches once more with different phrasing and also try fetching known authority sites directly.",
            "I'll retry the searches and try fetching known authority sites directly.",
            "Let me retry the searches with different phrasing and try fetching known authority sites.",
            "I'll retry the searches with slightly different phrasing and also try fetching known authority sites directly.",
        ]
        self.assertTrue(stream_mod._repeated_near_trailing_lines("\n".join(paraphrases)))
        distinct = [
            "I'll search for East Coast destinations.",
            "New York works well for first-time visitors.",
            "Boston is compact and walkable.",
            "Miami is better in winter.",
        ]
        self.assertFalse(stream_mod._repeated_near_trailing_lines("\n".join(distinct)))

    def test_alternating_near_trailing_lines(self):
        a = "Let me try the searches once more with different phrasing and also try fetching known authority sites directly."
        b = "I'll retry the searches and try fetching known authority sites directly."
        # Alternating A/B still fails exact-line halt; near-dup should catch it.
        text = "\n".join([a, b, a, b, a, b])
        self.assertTrue(stream_mod._repeated_near_trailing_lines(text))

    def test_repeated_trailing_block_plan_loop(self):
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        self.assertFalse(stream_mod._repeated_trailing_block(block))
        self.assertFalse(stream_mod._looping_text(block))
        self.assertTrue(stream_mod._repeated_trailing_block(block + block))
        self.assertTrue(stream_mod._looping_text(block + block))
        # A single numbered plan is not a loop.
        once = "\n".join(f"{i}. item {i} description here" for i in range(1, 10))
        self.assertFalse(stream_mod._repeated_trailing_block(once))
        # Whole-block chunk matching the tail is a loop, not a snapshot no-op.
        self.assertTrue(stream_mod._dropped_piece_is_loop(block, block))
        self.assertFalse(stream_mod._dropped_piece_is_loop(block, "1. Reuters Business\n"))


class PrinterResetTests(unittest.TestCase):
    def test_markdown_printer_reset_clears_parts(self):
        p = display._MarkdownLivePrinter()
        p._parts = ["a", "b"]
        p._started = True
        p._live_rows = 12
        p.reset()
        self.assertEqual(p._parts, [])
        self.assertFalse(p._started)
        self.assertEqual(p._live_rows, 0)

    def test_stream_printer_finish_resets(self):
        out = []
        p = display._StreamPrinter(out.append)
        p.feed("hi")
        self.assertTrue(p.finish())
        self.assertFalse(p.visible)
        p.feed("again")
        self.assertTrue(p.finish())
        self.assertEqual("".join(out), "hi\nagain\n")


class ChatStreamTests(unittest.TestCase):
    def test_streams_content_and_usage(self):
        body = _sse(
            {"choices": [{"delta": {"role": "assistant", "content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            None,
        )
        deltas = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5, "stream": True}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, usage = agent._chat_stream(cfg, "m", [{"role": "user", "content": "hi"}], None,
                                            on_delta=deltas.append)
        self.assertEqual(msg["content"], "Hello world")
        self.assertEqual("".join(deltas), "Hello world")
        self.assertEqual(usage.get("prompt_tokens"), 10)

    def test_streams_tool_calls(self):
        body = _sse(
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": ""},
            }]}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": '{"path":"."}'},
            }]}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "list_dir")

    def test_reasoning_content_fallback(self):
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": "think…"}}]},
            {"choices": [{"delta": {"reasoning_content": " more"}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], "think… more")
        self.assertEqual(msg.get("_reasoning"), "think… more")

    def test_reasoning_fallback_when_only_empty_named_tools(self):
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": "full answer"}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "", "arguments": "{}"},
            }]}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], "full answer")

    def test_cumulative_content_deltas(self):
        body = _sse(
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": "Hello world"}}]},
            None,
        )
        deltas = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_delta=deltas.append,
            )
        self.assertEqual(msg["content"], "Hello world")
        self.assertEqual("".join(deltas), "Hello world")

    def test_stale_shorter_content_snapshot(self):
        base = "Hello world — cumulative snapshot body for stale-prefix test!!"
        stale = base[: max(64, len(base) * 4 // 5)]
        body = _sse(
            {"choices": [{"delta": {"content": base}}]},
            {"choices": [{"delta": {"content": stale}}]},
            {"choices": [{"delta": {"content": base + "!"}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], base + "!")

    def test_repetition_circuit_breaker(self):
        # Non-periodic unit so successive 50-char chunks differ (identical
        # chunk replays are treated as cumulative no-ops).
        unit = "".join(f"{i:04d}" for i in range(200))[: stream_mod._REPEAT_WINDOW]
        self.assertEqual(len(unit), stream_mod._REPEAT_WINDOW)
        repeated = unit * stream_mod._REPEAT_TIMES
        pieces = [repeated[i:i + 50] for i in range(0, len(repeated), 50)]
        pieces.append("SHOULD_NOT_APPEAR")
        events = [{"choices": [{"delta": {"content": p}}]} for p in pieces]
        events.append({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "c1", "type": "function",
            "function": {"name": "list_dir", "arguments": "{}"},
        }]}}]})
        body = _sse(*events, None)
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], repeated)
        self.assertNotIn("SHOULD_NOT_APPEAR", msg["content"])
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "list_dir")

    def test_on_reasoning_callback(self):
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": "step1"}}]},
            {"choices": [{"delta": {"reasoning_content": " step2"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            None,
        )
        reasoning = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_reasoning=reasoning.append,
            )
        self.assertEqual("".join(reasoning), "step1 step2")
        self.assertEqual(msg["content"], "answer")
        self.assertEqual(msg.get("_reasoning"), "step1 step2")

    def test_reasoning_repeated_lines_halt_further_chunks(self):
        line = "The user asked about Bay Area history in detail.\n"
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": line * 8}}]},
            {"choices": [{"delta": {"reasoning_content": "SHOULD_NOT_APPEAR\n"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            None,
        )
        reasoning = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_reasoning=reasoning.append,
            )
        self.assertNotIn("SHOULD_NOT_APPEAR", "".join(reasoning))
        self.assertNotIn("SHOULD_NOT_APPEAR", msg.get("_reasoning", ""))
        self.assertEqual(msg["content"], "answer")

    def test_reasoning_plan_block_loop_halts_further_chunks(self):
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": block}}]},
            {"choices": [{"delta": {"reasoning_content": block}}]},
            {"choices": [{"delta": {"reasoning_content": "SHOULD_NOT_APPEAR\n"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            None,
        )
        reasoning = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_reasoning=reasoning.append,
            )
        self.assertNotIn("SHOULD_NOT_APPEAR", "".join(reasoning))
        self.assertNotIn("SHOULD_NOT_APPEAR", msg.get("_reasoning", ""))
        self.assertEqual(msg["content"], "answer")

    def test_reasoning_incremental_plan_block_loop_halts(self):
        """User-facing loop: tokens stream in small pieces, not one whole block."""
        preamble = "Good initial results. Let me fetch detailed sources.\n"
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        events = [{"choices": [{"delta": {"reasoning_content": preamble}}]}]
        for copy in (block, block):
            for i in range(0, len(copy), 24):
                events.append({"choices": [{"delta": {"reasoning_content": copy[i:i + 24]}}]})
        events.append({"choices": [{"delta": {"reasoning_content": "SHOULD_NOT_APPEAR\n"}}]})
        events.append({"choices": [{"delta": {"content": "answer"}}]})
        body = _sse(*events, None)
        reasoning = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_reasoning=reasoning.append,
            )
        self.assertNotIn("SHOULD_NOT_APPEAR", "".join(reasoning))
        self.assertEqual(msg["content"], "answer")

    def test_content_plan_block_loop_halts_further_chunks(self):
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        body = _sse(
            {"choices": [{"delta": {"content": block}}]},
            {"choices": [{"delta": {"content": block}}]},
            {"choices": [{"delta": {"content": "SHOULD_NOT_APPEAR\n"}}]},
            None,
        )
        deltas = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_delta=deltas.append,
            )
        self.assertNotIn("SHOULD_NOT_APPEAR", "".join(deltas))
        self.assertNotIn("SHOULD_NOT_APPEAR", msg["content"])

    def test_halt_aborts_sse_after_drain_without_tools(self):
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        extra = "x" * (stream_mod._HALT_DRAIN_CHARS + 10)
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": block}}]},
            {"choices": [{"delta": {"reasoning_content": block}}]},
            {"choices": [{"delta": {"reasoning_content": extra}}]},
            {"choices": [{"delta": {"content": "TOO_LATE"}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertNotIn("TOO_LATE", msg["content"])
        self.assertNotIn("TOO_LATE", msg.get("_reasoning", ""))

    def test_halt_drops_late_tool_calls_after_drain(self):
        """Thinking-loop halt closes the SSE body; tool_calls after drain are dropped."""
        from lmloop import stream as stream_mod
        block = (
            "Let me start with the first batch:\n"
            "1. Reuters Business\n"
            "2. TechCrunch AI section\n"
            "3. Google 2025 Research Breakthroughs\n"
            "4. CNN Business\n"
            "5. Reuters AI News\n"
            "Then the rest:\n"
            "6. Edapt Major AI Breakthroughs\n"
            "8. Medium AI Reality Check\n"
            "9. AP News Business\n"
            "Let me fetch the first 5 now:\n"
        )
        extra = "x" * (stream_mod._HALT_DRAIN_CHARS + 10)
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": block}}]},
            {"choices": [{"delta": {"reasoning_content": block}}]},
            {"choices": [{"delta": {"reasoning_content": extra}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1",
                "function": {"name": "web_search", "arguments": "{}"},
            }]}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertFalse(msg.get("tool_calls"))

    def test_reasoning_paraphrase_loop_halts_further_chunks(self):
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": (
                "Let me try the searches once more with different phrasing "
                "and also try fetching known authority sites directly.\n"
            )}}]},
            {"choices": [{"delta": {"reasoning_content": (
                "I'll retry the searches and try fetching known authority sites directly.\n"
            )}}]},
            {"choices": [{"delta": {"reasoning_content": (
                "Let me retry the searches with different phrasing and try "
                "fetching known authority sites.\n"
            )}}]},
            {"choices": [{"delta": {"reasoning_content": (
                "I'll retry the searches with slightly different phrasing and "
                "also try fetching known authority sites directly.\n"
            )}}]},
            {"choices": [{"delta": {"reasoning_content": "SHOULD_NOT_APPEAR\n"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            None,
        )
        reasoning = []
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(
                cfg, "m", [], None, on_reasoning=reasoning.append,
            )
        self.assertNotIn("SHOULD_NOT_APPEAR", "".join(reasoning))
        self.assertNotIn("SHOULD_NOT_APPEAR", msg.get("_reasoning", ""))
        self.assertEqual(msg["content"], "answer")

    def test_empty_stream_raises(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"")):
            with self.assertRaises(agent.ServerError) as ctx:
                agent._chat_stream(cfg, "m", [], None)
        self.assertIn("Empty stream", str(ctx.exception))

    def test_stream_options_retry_on_400(self):
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5, "stream": True}
        ok = _sse({"choices": [{"delta": {"content": "ok"}}]}, None)
        http_err = urllib.error.HTTPError(
            url="http://x/v1/chat/completions", code=400, msg="bad",
            hdrs=None, fp=io.BytesIO(b"nope"),
        )
        with mock.patch("urllib.request.urlopen", side_effect=[http_err, FakeResp(ok)]):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], "ok")


class ActIntegrationTests(unittest.TestCase):
    def test_act_auto_plain_streams_live_skips_echo(self):
        body = _sse({"choices": [{"delta": {"content": "hello"}}]}, None)
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        echoed, out = [], []
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch("lmloop.display._rich_live_available", return_value=False):
            # Re-bind plain writer through True path for capture
            agent.act(
                cfg, "m", [{"role": "user", "content": "hi"}],
                echo=echoed.append, echo_delta=out.append, echo_tool=lambda n, a, s=None: None,
            )
        self.assertEqual(echoed, [])
        self.assertEqual("".join(out), "hello\n")

    def test_act_silent_echoes_once(self):
        body = _sse({"choices": [{"delta": {"content": "**hello**"}}]}, None)
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        echoed = []
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch.object(agent, "_GeneratingIndicator") as ind_cls:
            ind = mock.Mock()
            ind_cls.return_value = ind
            agent.act(
                cfg, "m", [{"role": "user", "content": "hi"}],
                echo=echoed.append, echo_delta=False, echo_tool=lambda n, a, s=None: None,
            )
        self.assertEqual(echoed, ["**hello**"])
        ind.tick.assert_called()
        ind.clear.assert_called()

    def test_act_markdown_live_feeds_and_skips_echo(self):
        body = _sse(
            {"choices": [{"delta": {"content": "**a**"}}]},
            {"choices": [{"delta": {"content": " b"}}]},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        echoed = []
        feeds = []

        class FakeMD:
            visible = False

            def feed(self, piece):
                feeds.append(piece)
                self.visible = True

            def finish(self):
                return True

            def reset(self):
                self.visible = False

        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch.object(agent, "_open_live_display", return_value=(FakeMD(), "markdown")), \
             mock.patch.object(agent, "_GeneratingIndicator") as ind_cls:
            ind = mock.Mock()
            ind_cls.return_value = ind
            agent.act(
                cfg, "m", [{"role": "user", "content": "hi"}],
                echo=echoed.append, echo_tool=lambda n, a, s=None: None,
            )
        self.assertEqual(echoed, [])
        self.assertEqual(feeds, ["**a**", " b"])

    def test_act_multi_round_no_duplicate_assistant_bodies(self):
        round1 = _sse(
            {"choices": [{"delta": {"content": "Looking up"}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": "{}"},
            }]}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }},
            None,
        )
        round2 = _sse(
            {"choices": [{"delta": {"content": "All done"}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22,
            }},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]
        rounds_seen = []

        def echo_round(n, stats, msgs, *_a):
            rounds_seen.append((n, stats.get("last_prompt_tokens"), len(msgs)))

        stats = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "turns": 0, "rounds": 0, "tool_calls": 0,
            "last_prompt_tokens": 0, "last_completion_tokens": 0,
        }
        with mock.patch("urllib.request.urlopen", side_effect=[FakeResp(round1), FakeResp(round2)]), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", messages,
                echo=lambda *_a: None, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
                echo_round=echo_round,
                stats=stats,
            )
        assistant_bodies = [
            m.get("content") for m in messages if m.get("role") == "assistant"
        ]
        self.assertEqual(assistant_bodies, ["Looking up", "All done"])
        self.assertEqual(len(rounds_seen), 2)
        # Joined history must not contain a duplicated first reply.
        joined = "\n".join(assistant_bodies)
        self.assertEqual(joined.count("Looking up"), 1)

    def test_act_finishes_live_before_echo_round(self):
        """Round breadcrumbs must not print while rich.Live still owns the TTY."""
        body = _sse(
            {"choices": [{"delta": {"content": "Final answer"}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
            }},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]
        events = []

        class TrackingPrinter(display._StreamPrinter):
            def finish(self):
                events.append("finish")
                return super().finish()

        def echo_round(*_a, **_k):
            events.append("echo_round")

        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch.object(
                 agent, "_open_live_display",
                 return_value=(TrackingPrinter(lambda *_a: None), "plain"),
             ), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", messages,
                echo=lambda *_a: None,
                echo_round=echo_round,
            )
        self.assertEqual(events, ["finish", "echo_round"])

    def test_act_clears_spinner_before_echo_round_after_tools(self):
        """Tool-only rounds get a usage chunk after tools — spinner must stay gone."""
        round1 = _sse(
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": "{}"},
            }]}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
            }},
            None,
        )
        round2 = _sse(
            {"choices": [{"delta": {"content": "done"}}]},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        events = []

        class TrackingInd(display._GeneratingIndicator):
            def tick(self):
                if self._stopped:
                    return
                events.append("tick")
                self._shown = True
                self._n += 1

            def clear(self):
                events.append("clear")
                self._stopped = True
                self._shown = False

        class QuietPrinter(display._StreamPrinter):
            def finish(self):
                return bool(self._started)

            def feed(self, piece):
                if piece and piece.strip():
                    self._started = True

        def echo_round(*_a, **_k):
            events.append("echo_round")

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[FakeResp(round1), FakeResp(round2)],
        ), \
             mock.patch.object(
                 agent, "_open_live_display",
                 return_value=(QuietPrinter(None), "markdown"),
             ), \
             mock.patch.object(agent, "_GeneratingIndicator", TrackingInd), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", [{"role": "user", "content": "hi"}],
                echo=lambda *_a: None,
                echo_tool=lambda *_a, **_k: None,
                echo_round=echo_round,
            )
        # First round breadcrumb must come after a spinner clear, with no
        # resurrecting tick in between (usage chunk after tools).
        self.assertIn("clear", events)
        self.assertIn("echo_round", events)
        first_round = events.index("echo_round")
        last_clear_before = max(
            i for i, e in enumerate(events[:first_round]) if e == "clear"
        )
        self.assertTrue(all(
            e != "tick" for e in events[last_clear_before:first_round]
        ))

    def test_act_server_error_rolls_back_incomplete_tool_round(self):
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]
        start_len = len(messages)
        calls = {"n": 0}

        def fake_chat(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"},
                    }]},
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            raise agent.ServerError("boom")

        with mock.patch.object(agent, "_chat", side_effect=fake_chat), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            with self.assertRaises(agent.ServerError):
                agent.act(
                    cfg, "m", messages,
                    echo=lambda *_a: None, echo_delta=False,
                    echo_tool=lambda n, a, *r: None,
                )
        # Complete first tool round kept; no orphan after second failed chat.
        self.assertEqual(messages[start_len]["role"], "assistant")
        self.assertEqual(messages[start_len + 1]["role"], "tool")
        self.assertEqual(len(messages), start_len + 2)

    def test_act_unexpected_error_reraises_without_wrapping(self):
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]

        with mock.patch.object(agent, "_chat", side_effect=RuntimeError("bug")), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            with self.assertRaises(RuntimeError) as ctx:
                agent.act(
                    cfg, "m", messages,
                    echo=lambda *_a: None, echo_delta=False,
                    echo_tool=lambda n, a, *r: None,
                )
        self.assertEqual(str(ctx.exception), "bug")
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])

    def test_act_keyboard_interrupt_rolls_back_orphan_assistant_tools(self):
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]

        def boom(*_a, **_k):
            raise KeyboardInterrupt

        with mock.patch.object(agent, "_chat", side_effect=boom), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})):
            with self.assertRaises(KeyboardInterrupt):
                agent.act(
                    cfg, "m", messages,
                    echo=lambda *_a: None, echo_delta=False,
                    echo_tool=lambda n, a, *r: None,
                )
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])

    def test_act_max_rounds_message(self):
        body = _sse(
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": "{}"},
            }]}}]},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True,
            "confirm_shell": False, "max_rounds": 1,
        }
        echoed = []
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", [{"role": "user", "content": "hi"}],
                echo=echoed.append, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
            )
        self.assertTrue(any("max_rounds" in str(x) for x in echoed))
        self.assertTrue(any("/continue" in str(x) for x in echoed))

    def test_act_skips_empty_tool_names(self):
        body = _sse(
            {"choices": [{"delta": {"content": "done"}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "type": "function",
                "function": {"name": "", "arguments": "{}"},
            }]}}]},
            None,
        )
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch") as disp:
            agent.act(
                cfg, "m", messages,
                echo=lambda *_a: None, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
            )
        disp.assert_not_called()
        self.assertFalse(messages[-1].get("tool_calls"))

    def test_act_stores_reasoning_when_empty_tools_filtered(self):
        """Reasoning kept on screen must also land in messages/session history."""
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True, "confirm_shell": False,
        }
        messages = [{"role": "user", "content": "hi"}]

        def fake_chat(*_a, **_k):
            return (
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "", "arguments": "{}"},
                    }],
                    "_reasoning": "Here is the analysis.",
                },
                {},
            )

        with mock.patch.object(agent, "_chat", side_effect=fake_chat), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})):
            agent.act(
                cfg, "m", messages,
                echo=lambda *_a: None, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
            )
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["content"], "Here is the analysis.")
        self.assertFalse(messages[-1].get("tool_calls"))

    def test_act_nudge_skipped_on_final_round(self):
        """Do not leave an unanswered continue-nudge when max_rounds is exhausted."""
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True,
            "confirm_shell": False, "max_rounds": 2, "max_continue_nudges": 2,
        }
        messages = [{"role": "user", "content": "hi"}]
        echoed = []
        calls = {"n": 0}

        def fake_chat(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"},
                    }]},
                    {},
                )
            return (
                {"role": "assistant",
                 "content": "Let me dive deeper into the source code."},
                {},
            )

        with mock.patch.object(agent, "_chat", side_effect=fake_chat), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", messages,
                echo=echoed.append, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
            )
        self.assertEqual(calls["n"], 2)
        self.assertFalse(any(m.get("content") == NUDGE_CONTINUE_TEXT for m in messages))
        self.assertFalse(any("paused mid-task" in str(x) for x in echoed))
        self.assertTrue(any("stopped without finishing" in str(x) for x in echoed))

    def test_act_empty_final_after_tools_does_not_nudge(self):
        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0.7, "timeout_s": 5, "stream": True,
            "confirm_shell": False, "max_continue_nudges": 2,
        }
        messages = [{"role": "user", "content": "hi"}]
        echoed = []
        calls = {"n": 0}

        def fake_chat(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"},
                    }]},
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )
            return ({"role": "assistant", "content": ""}, {})

        with mock.patch.object(agent, "_chat", side_effect=fake_chat), \
             mock.patch("lmloop.display._rich_live_available", return_value=False), \
             mock.patch.object(agent.tools, "build_tools", return_value=([], {})), \
             mock.patch.object(agent.tools, "dispatch", return_value="ok"):
            agent.act(
                cfg, "m", messages,
                echo=echoed.append, echo_delta=False,
                echo_tool=lambda n, a, *r: None,
            )
        self.assertEqual(calls["n"], 2)
        self.assertFalse(any("paused mid-task" in str(x) for x in echoed))
        self.assertFalse(any(m.get("content") == NUDGE_CONTINUE_TEXT for m in messages))


class MalformedToolDeltaTests(unittest.TestCase):
    def test_bad_index_skipped(self):
        acc = {}
        stream_mod._merge_tool_call_delta(acc, [{"index": "nope", "function": {"name": "x"}}])
        stream_mod._merge_tool_call_delta(acc, "not-a-list")
        stream_mod._merge_tool_call_delta(acc, [None, {"index": 0, "id": "c1",
            "function": {"name": "list_dir", "arguments": "{}"}}])
        self.assertEqual(acc[0]["function"]["name"], "list_dir")

    def test_stream_survives_bad_tool_chunk(self):
        body = _sse(
            {"choices": [{"delta": {"tool_calls": "broken"}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": "x", "function": {"name": "nope"},
            }]}}]},
            {"choices": [{"delta": {"content": "ok"}}]},
            None,
        )
        cfg = {"base_url": "http://127.0.0.1:1234/v1", "temperature": 0.7, "timeout_s": 5}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
            msg, _usage = agent._chat_stream(cfg, "m", [], None)
        self.assertEqual(msg["content"], "ok")

    def test_rollback_helper_pops_incomplete_round(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "list_dir", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "function": {"name": "list_dir", "arguments": "{}"}},
            ]},
        ]
        agent._rollback_incomplete_messages(msgs, start=1)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[-1]["role"], "tool")


if __name__ == "__main__":
    unittest.main()
