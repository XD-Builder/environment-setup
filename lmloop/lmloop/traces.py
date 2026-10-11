"""Local OpenTelemetry-shaped spans. No collector, no network.

``act()`` writes a parent span and one child per tool call, then appends the
parent to ``eval_queue.jsonl``. ``lmloop eval --drain`` scores that queue
later so evaluation stays off the user-facing turn. Failed spans are
anonymized into ``evals/golden_inbox.jsonl`` for a human to promote into
``evals/golden/`` — nothing is auto-committed.

Opt out with ``LMLOOP_USAGE=0`` (same switch as usage.jsonl) or
``LMLOOP_TRACE=0``. Set ``LMLOOP_TRACE_CONTENT=1`` to keep a redacted
excerpt of the last assistant message.
"""

from __future__ import annotations

import contextvars
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import STATE_ROOT, utc_now
from .contracts import anonymize, check_span, redact
from .memory import append_jsonl
from .usage import enabled as usage_enabled

TRACE_PATH = STATE_ROOT / "traces.jsonl"
QUEUE_PATH = STATE_ROOT / "eval_queue.jsonl"
INBOX_PATH = STATE_ROOT / "evals" / "golden_inbox.jsonl"

_OFF = frozenset({"0", "false", "no", "n", "off"})
_ON = frozenset({"1", "true", "yes", "y", "on"})


def enabled() -> bool:
    if os.environ.get("LMLOOP_TRACE", "1").strip().lower() in _OFF:
        return False
    return usage_enabled()


def content_enabled() -> bool:
    return os.environ.get("LMLOOP_TRACE_CONTENT", "").strip().lower() in _ON


@dataclass
class _Turn:
    trace_id: str
    span_id: str
    children: list[dict] = field(default_factory=list)


@dataclass
class _Frame:
    turn: _Turn | None
    prev: Any


_CURRENT: contextvars.ContextVar = contextvars.ContextVar("lmloop_trace", default=None)


def start_turn() -> None:
    """Open a trace frame. Nested ``act()`` calls restore the outer frame."""
    prev = _CURRENT.get()
    turn = None
    if enabled():
        turn = _Turn(trace_id=uuid.uuid4().hex, span_id=uuid.uuid4().hex[:16])
    _CURRENT.set(_Frame(turn=turn, prev=prev))


def note_tool(name: str, result: str, *, round_idx: int) -> None:
    """Record a child span's attributes. Tool arguments are not stored."""
    frame = _CURRENT.get()
    if not isinstance(frame, _Frame) or frame.turn is None:
        return
    text = result or ""
    ok = not text.lstrip().startswith(("ERROR:", "DENIED:"))
    frame.turn.children.append({
        "tool": name or "",
        "ok": ok,
        "round": int(round_idx),
    })


def output_excerpt(messages: list | None) -> str:
    if not content_enabled():
        return ""
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def _span(turn: _Turn, *, span_id: str, parent: str, name: str, attributes: dict) -> dict:
    return {
        "ts": utc_now(),
        "trace_id": turn.trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "attributes": attributes,
    }


def finish_turn(
    *,
    rounds: int,
    tools: int,
    interrupted: bool,
    ok: bool,
    readonly: bool,
    no_tools: bool,
    output: str = "",
) -> None:
    """Write the turn's spans and enqueue the parent. Swallows ``OSError``."""
    frame = _CURRENT.get()
    if not isinstance(frame, _Frame):
        return
    _CURRENT.set(frame.prev)
    turn = frame.turn
    if turn is None:
        return
    try:
        spans: list[dict] = []
        for child in turn.children:
            spans.append(_span(
                turn,
                span_id=uuid.uuid4().hex[:16],
                parent=turn.span_id,
                name="tool",
                attributes=child,
            ))
        attrs: dict[str, Any] = {
            "rounds": int(rounds),
            "tools": int(tools),
            "interrupted": bool(interrupted),
            "ok": bool(ok),
            "readonly": bool(readonly),
            "no_tools": bool(no_tools),
            "tool_names": [child["tool"] for child in turn.children],
        }
        if output:
            attrs["output_excerpt"] = redact(output)[:240]
        root = _span(
            turn, span_id=turn.span_id, parent="", name="agent.act", attributes=attrs,
        )
        spans.append(root)
        for span in spans:
            append_jsonl(TRACE_PATH, span)
        append_jsonl(QUEUE_PATH, root)
    except OSError:
        return


def _read_rows(path: Path) -> tuple[list[dict], int]:
    if not path.is_file():
        return [], 0
    rows: list[dict] = []
    invalid = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            invalid += 1
    return rows, invalid


def drain(
    queue_path: Path | None = None,
    inbox_path: Path | None = None,
) -> dict[str, Any]:
    """Score queued spans. Failures are anonymized into the golden inbox."""
    queue = queue_path or QUEUE_PATH
    inbox = inbox_path or INBOX_PATH
    rows, invalid = _read_rows(queue)
    failed = 0
    if invalid:
        append_jsonl(inbox, {
            "flagged": True,
            "failures": ["invalid queue row"],
            "invalid_rows": invalid,
        })
        failed += invalid
    for row in rows:
        failures = check_span(row)
        if not failures:
            continue
        failed += 1
        item = anonymize(row)
        item["failures"] = failures
        item["flagged"] = True
        append_jsonl(inbox, item)
    if queue.exists():
        queue.write_text("", encoding="utf-8")
    return {
        "drained": len(rows),
        "failed": failed,
        "invalid": invalid,
        "inbox": str(inbox),
    }


def format_drain(report: dict) -> str:
    return "\n".join([
        "lmloop eval drain",
        f"  drained: {report['drained']}  failed: {report['failed']}"
        f"  invalid: {report['invalid']}",
        f"  inbox: {report['inbox']}",
    ])


def read_inbox(path: Path | None = None) -> list[dict]:
    rows, _invalid = _read_rows(path or INBOX_PATH)
    return rows


def format_inbox(rows: list[dict]) -> str:
    if not rows:
        return "lmloop eval inbox\n  (empty)"
    lines = [f"lmloop eval inbox ({len(rows)})"]
    for row in rows:
        lines.append(json.dumps(row, ensure_ascii=False))
    return "\n".join(lines)
