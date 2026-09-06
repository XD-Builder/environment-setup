"""The agent loop: OpenAI-compatible multi-round tool calling against LM Studio.

Chat HTTP lives in ``chat.py``. The loop is a gather/answer state machine: the
model may call tools for up to ``max_rounds`` gather steps, then one tools-off
answer. Gather ends early when the model stops requesting tools, or when it
repeats a tool set already run this turn (exact name+args). Tool errors are
reported back as text so it can self-correct instead of crashing the session.

Stream assembly lives in ``stream.py``. Live printers live in ``display.py``.
Skill playbooks live in ``skills.py``. LMS bring-up lives in ``server.py``.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import extract, memory, server, skills, tools
from . import status as status_mod
from .chat import _chat, _chat_stream
from .display import (
    _GeneratingIndicator,
    _ThinkingLive,
    _emit_assistant_content,
    _open_live_display,
)
from .ui import estimate_context_tokens

CONTEXT_PRESSURE_RATIO = 0.80
CONTINUE_NUDGE_MAX_CHARS = 400


def generate_skill_draft(cfg: dict, model: str, name: str, brief: str) -> str:
    """One-shot (no tools) generation of a new skill markdown body."""
    author = skills.load_skill("_author")
    brief = brief.strip() or f"A reusable playbook named '{name}'."
    user = (
        f"Author a new lmloop skill named `{name}`.\n\n"
        f"User brief:\n{brief}\n\n"
        f"Existing skills (do not duplicate; complement them): "
        f"{', '.join(skills.list_skills()) or '(none)'}\n"
    )
    messages = [
        {"role": "system", "content": author},
        {"role": "user", "content": user},
    ]
    msg, _usage = _chat(cfg, model, messages, tool_specs=None)
    return skills.extract_skill_markdown(msg.get("content") or "")


# ---------------------------------------------------------------- chat call
# HTTP lives in chat.py. Imported as _chat / _chat_stream for tests that patch
# this module.

def _accumulate_usage(stats: "dict | None", usage: dict) -> None:
    if not stats or not usage:
        return
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
    stats["total_tokens"] = stats.get("total_tokens", 0) + int(usage.get("total_tokens") or 0)
    stats["last_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    stats["last_completion_tokens"] = int(usage.get("completion_tokens") or 0)


# Soft-stop fallback: models lack a "meant to tool-call" signal, so we
# treat first-line / last-line prefixes as intent-only narration.
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


def _should_nudge_continue(content: str, turn_tools: int, nudges: int,
                           max_nudges: int) -> bool:
    """True when the model soft-stopped mid-task with intent-only narration.

    An empty final message after tools is treated as done — many models end a
    successful tool turn with no trailing prose. Also nudges first-round
    intent-only narration (turn_tools == 0) — classic local-model hang.
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
    # After tools, or first-round plan-only (no tools yet).
    return True


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


def _named_tool_calls(tool_calls: list) -> list:
    """Drop tool_calls with an empty function name (incomplete stream)."""
    out = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        name = (call.get("function") or {}).get("name") or ""
        if str(name).strip():
            out.append(call)
    return out


def _tool_call_fingerprint(call: dict) -> tuple:
    """Stable (name, args) so JSON key order / whitespace do not evade a match."""
    fn = call.get("function") or {}
    name = str(fn.get("name") or "").strip()
    raw = fn.get("arguments")
    if raw is None:
        args = ""
    elif isinstance(raw, str):
        args = raw.strip()
        try:
            parsed = json.loads(args) if args else {}
            args = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    else:
        args = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return (name, args)


def _tool_calls_fingerprint(tool_calls: list) -> tuple:
    """Ordered fingerprints for one gather round. Same set = no new information."""
    return tuple(_tool_call_fingerprint(c) for c in (tool_calls or []))


def _rollback_incomplete_messages(messages: list, start: int) -> None:
    """Pop trailing incomplete tool rounds / nudge prompts after abort.

    Keeps completed assistant+tool pairs and final text-only assistant replies.
    """
    while len(messages) > start:
        last = messages[-1]
        role = last.get("role")
        if role == "user" and status_mod.is_control_message(last):
            messages.pop()
            continue
        if role == "tool":
            i = len(messages) - 1
            while i >= start and messages[i].get("role") == "tool":
                i -= 1
            if i < start or messages[i].get("role") != "assistant":
                messages.pop()
                continue
            n_tools = len(messages) - 1 - i
            n_calls = len(messages[i].get("tool_calls") or [])
            if n_calls and n_tools < n_calls:
                del messages[i:]
                continue
            break
        if role == "assistant" and (last.get("tool_calls") or []):
            # Assistant requested tools but none (or not all) were recorded yet.
            messages.pop()
            continue
        break


# ---------------------------------------------------------------- act loop

@dataclass
class GatherTurn:
    """Mutable gather/answer state for one ``act()`` call."""

    max_gather: int
    max_nudges: int
    checkpoint: int
    gathering: bool = True
    seen_fps: set = field(default_factory=set)
    turn_rounds: int = 0
    turn_tools: int = 0
    nudge_count: int = 0
    context_warned: bool = False

    def is_answer_round(self, round_idx: int, echo_status) -> bool:
        if self.gathering and round_idx >= self.max_gather:
            self.gathering = False
            echo_status(status_mod.msg_gather_budget())
        return not self.gathering

    def warn_context(self, messages: list, stats: "dict | None",
                     context_limit: int, context_reserve: int,
                     echo_status) -> None:
        if not context_limit or self.context_warned:
            return
        used = 0
        if stats is not None:
            used = int(stats.get("last_prompt_tokens") or 0)
        if not used:
            used = estimate_context_tokens(messages)
        effective = max(context_limit - max(context_reserve, 0), 1)
        ratio = min(used / effective, 1.0)
        if ratio >= CONTEXT_PRESSURE_RATIO:
            echo_status(status_mod.msg_context_pressure(int(ratio * 100)))
            self.context_warned = True

    def note_tools(self, fp: tuple) -> bool:
        """Record a tool-set fingerprint. True if this set already ran."""
        if fp in self.seen_fps:
            return True
        self.seen_fps.add(fp)
        return False

    def after_empty_gather(self, content: str, halted: bool, round_idx: int,
                           echo_status, messages: list) -> str:
        """No tool_calls this gather round. Returns ``nudge`` or ``stop``."""
        can_nudge = (
            self.nudge_count < self.max_nudges and round_idx + 1 < self.max_gather
        )
        if halted:
            if can_nudge and _should_nudge_halt(
                content, self.nudge_count, self.max_nudges,
            ):
                self.nudge_count += 1
                echo_status(status_mod.msg_halted_continuing())
                messages.append(status_mod.nudge_message())
                return "nudge"
            echo_status(status_mod.msg_stopped_unfinished())
            return "stop"
        soft_stop = _should_nudge_continue(
            content, self.turn_tools, self.nudge_count, self.max_nudges,
        )
        if soft_stop and round_idx + 1 < self.max_gather:
            self.nudge_count += 1
            echo_status(status_mod.msg_paused_continuing())
            messages.append(status_mod.nudge_message())
            return "nudge"
        if soft_stop or (
            self.nudge_count >= self.max_nudges
            and content
            and _has_continue_intent(content)
            and not _looks_grounded(content)
        ):
            echo_status(status_mod.msg_stopped_unfinished())
        return "stop"

    def commit_stats(self, stats: "dict | None") -> None:
        if stats is None:
            return
        stats["turns"] = stats.get("turns", 0) + 1
        stats["rounds"] = stats.get("rounds", 0) + self.turn_rounds
        stats["tool_calls"] = stats.get("tool_calls", 0) + self.turn_tools
        stats["interrupted"] = False

    @staticmethod
    def mark_interrupted(stats: "dict | None") -> None:
        if stats is not None:
            stats["interrupted"] = True


class RoundDisplay:
    """Printer / thinking / spinner wiring for one model round."""

    def __init__(self, echo_delta, *, color: bool, use_stream: bool,
                 on_thinking=None):
        self.use_stream = use_stream
        self.on_thinking = on_thinking
        self.printer, self.live_mode = _open_live_display(echo_delta, color=color)
        self.thinking = _ThinkingLive(color=color) if use_stream else None
        self.indicator = None
        if use_stream and self.live_mode in ("silent", "markdown"):
            self.indicator = _GeneratingIndicator()
        self.thinking_cleared = False
        self.printer_finished = False

    def retire_thinking(self, *, keep: bool = False) -> str:
        if self.thinking_cleared or self.thinking is None:
            return ""
        text = self.thinking.finish_keep() if keep else self.thinking.erase()
        self.thinking_cleared = True
        if text.strip() and self.on_thinking:
            self.on_thinking(text)
        return text

    def on_delta(self, piece: str) -> None:
        self.retire_thinking()
        if self.indicator is not None:
            self.indicator.clear()
        was = self.printer.visible
        self.printer.feed(piece)
        if (
            self.indicator is not None
            and self.live_mode == "markdown"
            and self.printer.visible
            and not was
        ):
            self.indicator.clear()

    def on_reasoning(self, piece: str) -> None:
        if self.thinking is None:
            return
        # Content Live owns the TTY. Painting thinking alongside it
        # desyncs Live and stacks the preamble in scrollback.
        if self.printer.visible:
            return
        if self.indicator is not None:
            self.indicator.clear()
        self.thinking.feed(piece)

    def on_tools(self) -> None:
        self.retire_thinking()
        # Close Live before tool arguments stream (write_file can take
        # minutes). Leaving Live up redraws the same preamble and
        # leaks copies into scrollback.
        if (
            self.live_mode in ("plain", "markdown")
            and self.printer is not None
            and not self.printer_finished
        ):
            self.printer.finish()
            self.printer_finished = True
        if self.indicator is not None:
            self.indicator.resume()

    def chat_kwargs(self) -> dict:
        return {
            "on_delta": self.on_delta if self.use_stream else None,
            "on_activity": self.indicator.tick if self.indicator else None,
            "on_reasoning": self.on_reasoning if self.use_stream else None,
            "on_tools": self.on_tools if self.use_stream else None,
        }

    def settle(self, msg: dict, is_answer: bool) -> "tuple[list, str]":
        """Clear the spinner; return (tool_calls, content) for this round."""
        if self.indicator is not None:
            self.indicator.clear()
        halted = bool(msg.get("_halted"))
        tool_calls = (
            [] if is_answer
            else _named_tool_calls(msg.get("tool_calls") or [])
        )
        content = (msg.get("content") or "").strip()
        reasoning_text = (msg.get("_reasoning") or "").strip()
        # A halt is an unfinished round, not an answer. Do not promote
        # looping reasoning into content (that made gather stop).
        if halted and not tool_calls:
            if self.thinking is not None and self.thinking.active:
                retired = self.thinking.erase()
                if retired.strip() and self.on_thinking:
                    self.on_thinking(retired)
        else:
            if not content and not tool_calls and reasoning_text:
                content = reasoning_text
            if self.thinking is not None:
                content, retired = self.thinking.retire_for_round(
                    tool_calls, content, reasoning_text,
                )
                if retired.strip() and self.on_thinking:
                    self.on_thinking(retired)
        return tool_calls, content

    def emit(self, echo, content: str, session_log: "Path | None") -> None:
        # Finish Live/stream display before any other stdout writes
        # (round breadcrumb, tool lines). Printing while rich.Live is
        # active bypasses its cursor control and corrupts the frame.
        if content:
            if not self.printer_finished:
                _emit_assistant_content(
                    echo, content,
                    self.printer if self.use_stream else None,
                    self.live_mode,
                )
                self.printer_finished = True
            if session_log:
                memory.log_event(session_log, "assistant", content)
        elif self.use_stream and not self.printer_finished:
            self.printer.finish()
            self.printer_finished = True

    def close(self) -> None:
        if self.indicator is not None:
            self.indicator.clear()
        if self.thinking is not None and self.thinking.active:
            self.thinking.erase()
        if not self.printer_finished and self.printer is not None:
            self.printer.finish()


def _dispatch_tools(
    turn: GatherTurn, tool_calls: list, *, round_idx: int, impls: dict,
    echo_tool, stats, session_log, workspace_root, messages: list,
    cfg: dict, model: str,
) -> None:
    round_media = []
    prepared = []
    for call_idx, call in enumerate(tool_calls):
        turn.turn_tools += 1
        fn = call.get("function", {})
        name, args = fn.get("name", ""), fn.get("arguments", "")
        arg_preview = tools.format_tool_preview(
            name, args, workspace_root=workspace_root,
        )
        echo_tool(name, arg_preview, stats)
        prepared.append((call, call_idx, name, args, arg_preview))
    raw_results = tools.run_tool_calls(
        impls, [(name, args) for _call, _idx, name, args, _prev in prepared],
    )
    for (call, call_idx, name, _args, arg_preview), raw_result in zip(
        prepared, raw_results,
    ):
        result, attachments = tools.unwrap_tool_result(raw_result)
        if session_log:
            memory.log_event(
                session_log, "tool",
                f"{name}({arg_preview}) -> {result[:500]}",
            )
        messages.append({
            "role": "tool",
            "tool_call_id": call.get("id") or f"call_{round_idx}_{call_idx}",
            "content": result,
        })
        round_media.extend(attachments)
    if round_media and server.model_has_vision(model, cfg):
        messages.append({
            "role": "user",
            "content": extract.image_user_content(
                "[lmloop] Image from tool result",
                round_media,
            ),
        })


def act(cfg: dict, model: str, messages: list, session_log: "Path | None" = None,
        confirm_gate=None, echo=print, echo_tool=None, echo_delta=None,
        echo_status=None, stats: "dict | None" = None, echo_round=None,
        on_thinking=None, context_limit: int = 0,
        context_reserve: int = 2048,
        workspace_root: "Path | None" = None,
        readonly: bool = False,
        no_tools: bool = False,
        max_rounds: "int | None" = None,
        extra_readable: "list | None" = None) -> list:
    """Run the multi-round tool loop. Mutates and returns `messages`.

    ``extra_readable`` is a turn-scoped list of paths the user attached with
    ``@``; ``read_file`` / ``list_dir`` may open those even outside the
    workspace. Writes to those outside paths require confirmation. Do not
    copy attached files into the workspace.

    ``no_tools=True`` omits the tools array (and ``tool_choice``) on every
    round — used by ``/save``. ``readonly`` still applies when tools are on.
    ``max_rounds`` overrides ``cfg['max_rounds']`` when set (eval budget).

    When streaming is enabled (config ``stream``, default True), the HTTP body
    is SSE and tokens are shown live:
    - ``echo_delta=None`` (default): rich.Live markdown when available, else plain
    - ``echo_delta=True``: live plain tokens
    - ``echo_delta=False``: spinner only; ``echo`` once when the round completes
    - callable: custom live writer
    Live modes do not also call ``echo`` (avoids plain+rich doubles).
    When tool_calls start, Live is closed so a long argument stream cannot
    redraw the preamble; the spinner resumes until the round finishes.

    Optional callbacks:
    - ``echo_status(text)`` for lmloop status lines (defaults to ``echo``)
    - ``echo_round(round_idx, stats, messages)`` after each model round
    - ``on_thinking(text)`` when thinking is stored for Ctrl+O

    Gather may run tools for up to ``max_rounds`` steps. A tools-off answer
    follows when gather repeats a tool set already run this turn, or hits that
    budget. A gather round with no tool_calls is the final reply (no extra
    answer call).

    On interrupt or server error, display is cleaned up and any incomplete
    trailing tool round is rolled back; completed rounds in this turn are kept.
    """
    if echo_status is None:
        echo_status = echo
    if echo_tool is None:
        echo_tool = lambda name, preview, stats=None: echo(f"  ⚙ {name}({preview})")
    color = bool(cfg.get("color", True))
    if no_tools:
        tool_specs, impls = None, {}
    else:
        tool_specs, impls = tools.build_tools(
            cfg, confirm_gate=confirm_gate, workspace_root=workspace_root,
            readonly=readonly, extra_readable=extra_readable,
        )
    use_stream = bool(cfg.get("stream", True))
    gather_budget = max_rounds if max_rounds is not None else cfg.get("max_rounds", 60)
    turn = GatherTurn(
        max_gather=max(1, int(gather_budget)),
        max_nudges=max(0, int(cfg.get("max_continue_nudges", 2))),
        checkpoint=len(messages),
    )
    prev_session = memory.set_active_session(session_log)

    def _finish() -> list:
        turn.commit_stats(stats)
        return messages

    try:
        for round_idx in range(turn.max_gather + 1):
            is_answer = turn.is_answer_round(round_idx, echo_status)
            if is_answer and not (
                messages and status_mod.is_answer_message(messages[-1])
            ):
                messages.append(status_mod.answer_message())
            turn.warn_context(
                messages, stats, context_limit, context_reserve, echo_status,
            )

            round_ui = RoundDisplay(
                echo_delta, color=color, use_stream=use_stream,
                on_thinking=on_thinking,
            )
            try:
                round_specs = None if (is_answer or no_tools) else tool_specs
                msg, usage = _chat(
                    cfg, model, messages, round_specs,
                    **round_ui.chat_kwargs(),
                )
                _accumulate_usage(stats, usage)
                turn.turn_rounds += 1
                tool_calls, content = round_ui.settle(msg, is_answer or no_tools)
                halted = bool(msg.get("_halted"))

                assistant_entry = {"role": "assistant", "content": content or ""}
                if tool_calls:
                    assistant_entry["tool_calls"] = tool_calls
                messages.append(assistant_entry)
                round_ui.emit(echo, content, session_log)

                if echo_round is not None:
                    echo_round(
                        round_idx + 1, stats, messages,
                        context_limit, context_reserve,
                    )

                if is_answer:
                    return _finish()

                if not tool_calls:
                    if turn.after_empty_gather(
                        content, halted, round_idx, echo_status, messages,
                    ) == "nudge":
                        continue
                    return _finish()

                if turn.note_tools(_tool_calls_fingerprint(tool_calls)):
                    echo_status(status_mod.msg_repeated_tools())
                    messages[-1].pop("tool_calls", None)
                    turn.gathering = False
                    continue

                _dispatch_tools(
                    turn, tool_calls, round_idx=round_idx, impls=impls,
                    echo_tool=echo_tool, stats=stats, session_log=session_log,
                    workspace_root=workspace_root, messages=messages,
                    cfg=cfg, model=model,
                )
                if round_idx + 1 >= turn.max_gather:
                    echo_status(status_mod.msg_gather_budget())
                    turn.gathering = False
            finally:
                round_ui.close()

        echo_status(status_mod.msg_hit_max_rounds())
        return _finish()
    except (KeyboardInterrupt, Exception):
        GatherTurn.mark_interrupted(stats)
        _rollback_incomplete_messages(messages, turn.checkpoint)
        raise
    finally:
        memory.set_active_session(prev_session)
