"""SSE assembly: cumulative snapshots, overlap, and repeat-halt.

Display code imports the think-line helpers so live thinking and the stream
halt share one similarity predicate. This module must not import agent or display.
"""

import json
from difflib import SequenceMatcher

# Some servers send cumulative snapshots instead of true deltas.
_REPEAT_WINDOW = 400
_REPEAT_TIMES = 3
_REPEAT_LINE_TIMES = 6
_REPEAT_NEAR_TIMES = 4
_REPEAT_NEAR_WINDOW = 8
_REPEAT_BLOCK_TIMES = 2
_REPEAT_BLOCK_MIN_LINES = 5
_REPEAT_BLOCK_MAX_LINES = 32
# After a halt, keep reading this many extra chars for a late tool/answer
# before closing the SSE body. No max_tokens is set, so a thinking loop
# can otherwise run until the context window fills.
_HALT_DRAIN_CHARS = 2048
_THINK_SIMILAR_MIN = 24
_THINK_SIMILAR_RATIO = 0.72

_THINK_FILLER = frozenset({
    "the", "a", "an", "and", "or", "also", "again", "once", "more", "just",
    "now", "then", "still", "slightly", "different", "well-known", "known",
    "directly", "specific", "major", "some", "both", "approach", "approaches",
    "if", "that", "this", "with", "from", "for", "to",
})
SSE_DATA_PREFIX = "data:"
SSE_DONE = "[DONE]"
REASONING_FIELDS = ("reasoning_content", "reasoning")


class StreamError(RuntimeError):
    """Unrecoverable SSE body (empty stream, etc.)."""


def _merge_tool_call_delta(acc: dict, deltas) -> None:
    """Accumulate streamed tool_call deltas keyed by index (OpenAI SSE shape).

    Skips malformed entries so one bad SSE chunk cannot kill the turn.
    """
    if not isinstance(deltas, list):
        return
    for tc in deltas:
        if not isinstance(tc, dict):
            continue
        try:
            idx = int(tc.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if idx not in acc:
            acc[idx] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        entry = acc[idx]
        if tc.get("id"):
            entry["id"] = tc["id"]
        if tc.get("type"):
            entry["type"] = tc["type"]
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            continue
        if fn.get("name"):
            entry["function"]["name"] += str(fn["name"])
        if "arguments" in fn and fn["arguments"] is not None:
            entry["function"]["arguments"] += str(fn["arguments"])


def _ingest_text_delta(parts: list, piece: str) -> str:
    """Append a stream text piece; normalize cumulative snapshots to a true suffix.

    Returns the incremental text that was newly added (may be empty).
    Cumulative servers re-send the full text so far; only the new suffix is kept.
    An exact replay (piece == so_far) is ignored as no-new-tokens.
    A shorter prefix of so_far is a stale snapshot (skipped when long enough
    that it is not a tiny incremental token). Overlapping windows keep only
    the new suffix so the same thinking line is not appended again.
    """
    if not piece:
        return ""
    so_far = "".join(parts)
    if not so_far:
        parts.append(piece)
        return piece
    if piece.startswith(so_far):
        suffix = piece[len(so_far):]
        if suffix:
            parts.append(suffix)
        return suffix
    if so_far.startswith(piece) and (
        ("\n" in piece and len(piece.strip()) >= 8)
        or len(piece) >= max(64, len(so_far) * 4 // 5)
    ):
        return ""
    suffix = _overlap_suffix(so_far, piece)
    if suffix != piece:
        if suffix:
            parts.append(suffix)
        return suffix
    parts.append(piece)
    return piece


def _overlap_suffix(so_far: str, piece: str) -> str:
    """If a newline-delimited ``piece`` overlaps the tail of ``so_far``, keep the rest.

    Only real text lines are merged. Bare ``\\n`` / whitespace tokens must still
    append (paragraph breaks), and tiny incremental tokens are untouched.
    Sliding windows of several lines (not just the last one) are merged too —
    otherwise a last-N-lines snapshot re-appends the same plan block.
    """
    if "\n" not in piece:
        return piece
    first_line, rest = piece.split("\n", 1)
    if not first_line.strip():
        return piece
    first = first_line + "\n"
    if so_far.endswith(piece):
        return ""
    if so_far.endswith(first):
        return rest
    if so_far.endswith(first_line):
        return "\n" + rest
    # Longest newline-terminated prefix of piece that is a suffix of so_far.
    # Require two complete lines so a one-line coincidence is not merged.
    idx = piece.rfind("\n", 0, len(piece) - 1)
    while idx >= 0:
        prefix = piece[: idx + 1]
        if prefix.count("\n") >= 2 and so_far.endswith(prefix):
            return piece[idx + 1 :]
        idx = piece.rfind("\n", 0, idx)
    return piece


def _tail_repeats(text: str, window: int = _REPEAT_WINDOW, times: int = _REPEAT_TIMES) -> bool:
    """True when the last `window` chars repeat `times` times consecutively."""
    need = window * times
    if len(text) < need:
        return False
    tail = text[-need:]
    unit = tail[:window]
    return unit * times == tail


def _trailing_nonempty_lines(text: str, limit: int) -> list:
    """Last ``limit`` non-empty stripped lines, oldest first. Scans from the end."""
    if limit <= 0 or not text:
        return []
    out = []
    i = len(text)
    while i > 0 and len(out) < limit:
        j = text.rfind("\n", 0, i)
        raw = text[j + 1 : i] if j != -1 else text[:i]
        i = j
        s = raw.strip()
        if s:
            out.append(s)
        if j == -1:
            break
    out.reverse()
    return out


def _repeated_trailing_lines(text: str, times: int = _REPEAT_LINE_TIMES) -> bool:
    """True when the last ``times`` non-empty lines are identical."""
    lines = _trailing_nonempty_lines(text, times)
    if len(lines) < times:
        return False
    last = lines[-1]
    return all(ln == last for ln in lines)


def _normalize_think_line(line: str) -> str:
    """Collapse filler so retry paraphrases compare as the same thought."""
    s = (line or "").strip().lower().replace("\u2019", "'")
    s = s.replace("i'll", "i will").replace("i'm", "i am")
    s = s.replace("let me", "i will").replace("retry", "try")
    chars = [ch if ch.isalnum() or ch.isspace() else " " for ch in s]
    words = "".join(chars).split()
    return " ".join(w for w in words if w not in _THINK_FILLER)


def _think_line_similar(a: str, b: str) -> bool:
    """True when two thinking lines are the same thought, possibly paraphrased."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < _THINK_SIMILAR_MIN:
        return False
    na, nb = _normalize_think_line(a), _normalize_think_line(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= _THINK_SIMILAR_MIN and shorter in longer:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= _THINK_SIMILAR_RATIO


def _repeated_near_trailing_lines(
    text: str,
    times: int = _REPEAT_NEAR_TIMES,
    window: int = _REPEAT_NEAR_WINDOW,
) -> bool:
    """True when recent thinking lines are the same plan, paraphrased or A/B.

    Exact-copy loops are handled by ``_repeated_trailing_lines``. This catches
    local-model retry narration that rewrites the same sentence each line.
    """
    recent = _trailing_nonempty_lines(text, window)
    if len(recent) < times:
        return False
    last = recent[-1]
    if sum(1 for ln in recent if _think_line_similar(ln, last)) >= times:
        return True
    if len(recent) < 6:
        return False
    a, b = recent[-6], recent[-5]
    if _think_line_similar(a, b):
        return False
    tail = recent[-6:]
    return all(
        _think_line_similar(ln, a if i % 2 == 0 else b)
        for i, ln in enumerate(tail)
    )


def _repeated_trailing_block(
    text: str,
    times: int = _REPEAT_BLOCK_TIMES,
    min_lines: int = _REPEAT_BLOCK_MIN_LINES,
    max_lines: int = _REPEAT_BLOCK_MAX_LINES,
) -> bool:
    """True when the same trailing block of lines repeats ``times`` times.

    Catches plan loops like ``Let me fetch the first 5 now:`` plus a numbered
    list — heterogeneous blocks that never produce 6 identical consecutive
    lines and whose period is not ``_REPEAT_WINDOW`` chars.
    """
    if times < 2 or min_lines < 1:
        return False
    lines = _trailing_nonempty_lines(text, max_lines * times)
    n = len(lines)
    if n < min_lines * times:
        return False
    limit = min(max_lines, n // times)
    for size in range(min_lines, limit + 1):
        block = lines[-size:]
        if all(lines[n - size * (t + 1) : n - size * t] == block for t in range(times)):
            return True
    return False


def _looping_text(text: str) -> bool:
    """True when assembled stream text is stuck repeating."""
    return (
        _tail_repeats(text)
        or _repeated_trailing_lines(text)
        or _repeated_near_trailing_lines(text)
        or _repeated_trailing_block(text)
    )


def _dropped_piece_is_loop(assembled: str, piece: str) -> bool:
    """True when ingest dropped ``piece`` as a snapshot, but it is a repeated block.

    Cumulative servers re-send the text so far (or its tail). Incremental
    servers may emit the same plan block as one chunk — that also looks like
    a tail replay, so halt never sees a second copy unless we count it here.
    """
    if not piece or not assembled or "\n" not in piece:
        return False
    if piece != assembled and not assembled.endswith(piece):
        return False
    return _looping_text(assembled + piece)


def _read_sse(resp, on_delta=None, on_activity=None, on_reasoning=None,
              on_tools=None) -> "tuple[dict, dict]":
    """Parse an OpenAI-style SSE body into (assistant message, usage)."""
    content_parts: list = []
    reasoning_parts: list = []
    tool_acc: dict = {}
    usage: dict = {}
    saw_data = False
    content_halted = False
    reasoning_halted = False
    tools_started = False
    halt_drain = 0
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith(SSE_DATA_PREFIX):
            continue
        data = line[len(SSE_DATA_PREFIX):].strip()
        if data == SSE_DONE:
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        saw_data = True
        if on_activity:
            on_activity()
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            if content_halted:
                halt_drain += len(piece)
            else:
                halt_drain = 0
                incr = _ingest_text_delta(content_parts, piece)
                assembled = "".join(content_parts)
                if incr:
                    if on_delta:
                        on_delta(incr)
                    if _looping_text(assembled):
                        content_halted = True
                elif _dropped_piece_is_loop(assembled, piece):
                    content_halted = True
        # Qwen / some LM Studio builds stream chain-of-thought separately.
        reasoning = None
        for field in REASONING_FIELDS:
            if delta.get(field):
                reasoning = delta[field]
                break
        if reasoning:
            if reasoning_halted:
                halt_drain += len(reasoning)
            else:
                incr_r = _ingest_text_delta(reasoning_parts, reasoning)
                assembled_r = "".join(reasoning_parts)
                if incr_r:
                    if on_reasoning:
                        on_reasoning(incr_r)
                    if _looping_text(assembled_r):
                        reasoning_halted = True
                elif _dropped_piece_is_loop(assembled_r, reasoning):
                    reasoning_halted = True
        tc_deltas = delta.get("tool_calls")
        if tc_deltas:
            halt_drain = 0
            if not tools_started:
                tools_started = True
                if on_tools:
                    on_tools()
            _merge_tool_call_delta(tool_acc, tc_deltas)
        if (
            (content_halted or reasoning_halted)
            and halt_drain >= _HALT_DRAIN_CHARS
            and not tool_acc
        ):
            # Close the body so a thinking/content loop cannot run until
            # context-full. Late tool_calls after this point are dropped.
            break

    if not saw_data:
        raise StreamError("Empty stream from LM Studio (no SSE data)")

    content = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    halted = content_halted or reasoning_halted
    has_named_tools = any(
        ((tool_acc[i].get("function") or {}).get("name") or "").strip()
        for i in tool_acc
    )
    if not content.strip() and not has_named_tools and reasoning_text and not halted:
        # Fallback when the server only streamed reasoning (or empty-name
        # tool stubs) and no final content. A halt is not a finished answer.
        content = reasoning_text
    msg: dict = {"role": "assistant", "content": content}
    if tool_acc:
        msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    if reasoning_text:
        msg["_reasoning"] = reasoning_text
    if halted:
        msg["_halted"] = True
    return msg, usage
