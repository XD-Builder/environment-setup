"""Continue-intent and halt nudges for a gather round that did not call tools.

Models have no "meant to tool-call" signal. First-line and last-line prefixes
are the intent lexicon. Grounded replies (a path, a URL, or inline code) are
answers and are not nudged.
"""

from __future__ import annotations

CONTINUE_NUDGE_MAX_CHARS = 400

_INTENT_STARTS = (
    "let me",
    "i'll",
    "i will",
    "i'm going to",
    "i am going to",
    "i'm about to",
    "i am about to",
    "next,",
    "next:",
    "next ",
    "now i'll",
    "now i will",
    "now let me",
    "i need to",
    "looking at",
    "looking into",
    "diving",
    "exploring",
    "checking",
    "investigating",
)
_CODE_EXTS = frozenset({
    "py", "md", "go", "ts", "js", "tsx", "jsx", "rs", "java", "c", "h", "cpp",
})


def _line_has_intent(line: str) -> bool:
    s = (line or "").strip().lower()
    return any(s.startswith(p) for p in _INTENT_STARTS)


def _ends_with_continue_intent(text: str) -> bool:
    """True when the last line or last sentence still narrates a next step."""
    s = (text or "").strip()
    if not s:
        return False
    last_line = s.rsplit("\n", 1)[-1]
    if len(last_line) <= CONTINUE_NUDGE_MAX_CHARS and _line_has_intent(last_line):
        return True
    sentences = [p.strip() for p in s.split(".") if p.strip()]
    if not sentences:
        return False
    last = sentences[-1]
    return len(last) <= CONTINUE_NUDGE_MAX_CHARS and _line_has_intent(last)


def _has_continue_intent(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if _line_has_intent(s):
        return True
    return _ends_with_continue_intent(s)


def _looks_grounded(text: str) -> bool:
    """True when the reply cites a path, URL, or inline code — not just a plan."""
    if not text:
        return False
    if "`" in text or "http://" in text or "https://" in text:
        return True
    for raw in text.replace(",", " ").replace("(", " ").replace(")", " ").split():
        token = raw.strip()
        if token.startswith("/") and "/" in token[1:]:
            return True
        if ":" not in token or "." not in token:
            continue
        path, _, rest = token.partition(":")
        if rest[:1].isdigit() and path.rsplit(".", 1)[-1].lower() in _CODE_EXTS:
            return True
    return False


def _should_nudge_continue(content: str, nudges: int, max_nudges: int) -> bool:
    """True when the model soft-stopped mid-task with intent-only narration.

    An empty final message is treated as done — many models end a successful
    tool turn with no trailing prose. Tool count does not matter: a first-round
    plan and a post-tool plan use the same predicate.
    """
    if nudges >= max_nudges:
        return False
    text = (content or "").strip()
    if not text:
        return False
    if len(text) > CONTINUE_NUDGE_MAX_CHARS:
        return False
    if not _has_continue_intent(text):
        return False
    if _looks_grounded(text):
        return False
    return True


def _is_noise(text: str) -> bool:
    """True when halted content carries no letters or digits (``.``, ``-``, …).

    A repetition halt on punctuation-only output is the model stalling, not
    a draft worth keeping; treat it like an empty round.
    """
    return not any(ch.isalnum() for ch in (text or ""))


def _should_nudge_halt(content: str, nudges: int, max_nudges: int) -> bool:
    """True when a halted stream has not yet produced the user's answer.

    Empty content is the CoT-only hang — nudge so gather can still write a
    file. A long draft with no next-step at the end is already the answer;
    regenerating it from the top is worse than keeping the truncated copy.
    """
    if nudges >= max_nudges:
        return False
    text = (content or "").strip()
    if not text:
        return True
    return _ends_with_continue_intent(text)


def _warn_stopped_unfinished(content: str, nudges: int, max_nudges: int) -> bool:
    """True when gather stops on intent that was not nudged into another round.

    A nudge that still fits the budget warns when the gather round budget is
    already spent. After the nudge budget, a long intent line still warns:
    that branch does not apply the short-text cap.
    """
    if _should_nudge_continue(content, nudges, max_nudges):
        return True
    if nudges < max_nudges or not content:
        return False
    return _has_continue_intent(content) and not _looks_grounded(content)
