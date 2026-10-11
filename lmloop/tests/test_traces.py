"""Local trace spans, the eval queue, and the golden inbox."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lmloop import agent, traces
from lmloop.contracts import anonymize, check_span


def _span(**overrides) -> dict:
    row = {
        "ts": "2026-01-01T00:00:00Z",
        "trace_id": "trace",
        "span_id": "span",
        "parent_span_id": "",
        "name": "agent.act",
        "attributes": {"ok": True},
    }
    row.update(overrides)
    return row


class SpanContractTests(unittest.TestCase):
    def test_parent_span_shape_passes(self):
        self.assertEqual(check_span(_span()), [])

    def test_secret_in_attributes_fails(self):
        failures = check_span(_span(attributes={"output_excerpt": "api_key=supersecretvalue"}))
        self.assertTrue(any("secret" in item for item in failures))

    def test_anonymize_redacts_values_and_secret_keys(self):
        cleaned = anonymize({
            "api_key": "supersecretvalue",
            "note": "token=abcdef123456",
            "nested": ["sk-abcdefghij"],
        })
        self.assertEqual(cleaned["api_key"], "[redacted]")
        self.assertNotIn("abcdef123456", json.dumps(cleaned))
        self.assertNotIn("sk-abcdefghij", json.dumps(cleaned))


class QueueTests(unittest.TestCase):
    def test_drain_flags_bad_spans_and_clears_the_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.jsonl"
            inbox = Path(tmp) / "inbox.jsonl"
            good = _span(span_id="good")
            bad = _span(span_id="bad", attributes={"output_excerpt": "api_key=supersecretvalue"})
            queue.write_text(
                json.dumps(good) + "\n" + json.dumps(bad) + "\nnot-json\n",
                encoding="utf-8",
            )
            report = traces.drain(queue, inbox)
            self.assertEqual(report["drained"], 2)
            self.assertGreaterEqual(report["failed"], 2)
            self.assertEqual(queue.read_text(encoding="utf-8"), "")
            rows = traces.read_inbox(inbox)
            blob = json.dumps(rows)
            self.assertNotIn("supersecretvalue", blob)
            self.assertTrue(any(row.get("flagged") for row in rows))

    def test_finish_turn_writes_parent_and_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "traces.jsonl"
            queue_path = Path(tmp) / "queue.jsonl"
            with mock.patch.object(traces, "TRACE_PATH", trace_path), \
                 mock.patch.object(traces, "QUEUE_PATH", queue_path):
                traces.start_turn()
                traces.note_tool("read_file", "1: hello", round_idx=0)
                traces.note_tool("read_file", "ERROR: missing", round_idx=0)
                traces.finish_turn(
                    rounds=1, tools=2, interrupted=False, ok=True,
                    readonly=False, no_tools=False,
                )
            rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["name"] for row in rows], ["tool", "tool", "agent.act"])
            parent = rows[-1]["span_id"]
            self.assertEqual(rows[0]["parent_span_id"], parent)
            self.assertEqual(rows[1]["parent_span_id"], parent)
            self.assertFalse(rows[1]["attributes"]["ok"])
            self.assertEqual(rows[-1]["attributes"]["tool_names"], ["read_file", "read_file"])
            queued = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(queued["name"], "agent.act")
            self.assertEqual(check_span(queued), [])

    def test_trace_opt_out_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "traces.jsonl"
            with mock.patch.object(traces, "TRACE_PATH", trace_path), \
                 mock.patch.dict(os.environ, {"LMLOOP_TRACE": "0"}):
                traces.start_turn()
                traces.note_tool("read_file", "ok", round_idx=0)
                traces.finish_turn(
                    rounds=1, tools=1, interrupted=False, ok=True,
                    readonly=False, no_tools=False,
                )
            self.assertFalse(trace_path.exists())

    def test_content_excerpt_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "traces.jsonl"
            queue_path = Path(tmp) / "queue.jsonl"
            with mock.patch.object(traces, "TRACE_PATH", trace_path), \
                 mock.patch.object(traces, "QUEUE_PATH", queue_path), \
                 mock.patch.dict(os.environ, {"LMLOOP_TRACE_CONTENT": "1"}):
                traces.start_turn()
                traces.finish_turn(
                    rounds=1, tools=0, interrupted=False, ok=True,
                    readonly=True, no_tools=True,
                    output="done api_key=supersecretvalue",
                )
            row = json.loads(trace_path.read_text(encoding="utf-8"))
            excerpt = row["attributes"]["output_excerpt"]
            self.assertIn("done", excerpt)
            self.assertNotIn("supersecretvalue", excerpt)


class ActTraceTests(unittest.TestCase):
    def test_act_enqueues_a_parent_span(self):
        def fake_chat(_cfg, _model, _messages, tool_specs, **_kwargs):
            if tool_specs is not None:
                return ({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "list_dir", "arguments": "{}"},
                    }],
                }, {})
            return ({"role": "assistant", "content": "done"}, {})

        cfg = {
            "base_url": "http://127.0.0.1:1234/v1",
            "temperature": 0,
            "timeout_s": 5,
            "stream": False,
            "confirm_shell": False,
            "max_rounds": 4,
        }
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "traces.jsonl"
            queue_path = Path(tmp) / "queue.jsonl"
            with mock.patch.object(traces, "TRACE_PATH", trace_path), \
                 mock.patch.object(traces, "QUEUE_PATH", queue_path), \
                 mock.patch.object(agent, "_chat", side_effect=fake_chat), \
                 mock.patch("lmloop.tools.dispatch", return_value="listed"), \
                 mock.patch("lmloop.display._rich_live_available", return_value=False):
                agent.act(
                    cfg, "m", [{"role": "user", "content": "hi"}],
                    echo=lambda *_args: None, echo_delta=False,
                    echo_tool=lambda *_args, **_kwargs: None,
                )
            rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            names = [row["name"] for row in rows]
            self.assertIn("tool", names)
            self.assertEqual(names[-1], "agent.act")
            self.assertTrue(rows[-1]["attributes"]["ok"])
            self.assertIn("list_dir", rows[-1]["attributes"]["tool_names"])
            self.assertEqual(rows[-1]["parent_span_id"], "")


if __name__ == "__main__":
    unittest.main()
