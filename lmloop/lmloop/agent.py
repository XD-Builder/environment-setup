"""The agent loop: OpenAI-compatible multi-round tool calling against LM Studio.

Chat HTTP lives in ``model/chat.py``. The loop is a gather/answer state machine:
the model may call tools for up to ``max_rounds`` gather steps, then one
tools-off answer. Gather ends early when the model stops requesting tools, or
when it repeats a tool set already run this turn (exact name+args). Tool errors
are reported back as text so it can self-correct instead of crashing the session.

Stream assembly lives in ``model/stream.py``. Live printers live in
``model/display.py``. Skill playbooks live in ``prompts/skills/``. LMS bring-up
lives in ``model/server.py``.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .capabilities import extract
from . import memory
from .model import server
from .prompts import skills
from .capabilities import tools
from .core.config import Config, DEFAULTS, cfg_bool, cfg_int
from .core import status as status_mod
from .core.continue_policy import (
    _should_nudge_continue,
    _should_nudge_halt,
    _warn_stopped_unfinished,
)
from .model.chat import _chat, _chat_stream
from .model.display import RoundDisplay
from .ui import estimate_context_tokens

CONTEXT_PRESSURE_RATIO = 0.80


def generate_skill_draft(cfg: Config, model: str, name: str, brief: str) -> str:
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
    if not stats:
        return
    if not usage:
        # A halted stream closes before the server's usage chunk. Zero the
        # per-round numbers so the round line / footer do not repeat the
        # previous round; context fill falls back to the estimate.
        stats["last_prompt_tokens"] = 0
        stats["last_completion_tokens"] = 0
        return
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
    stats["total_tokens"] = stats.get("total_tokens", 0) + int(usage.get("total_tokens") or 0)
    stats["last_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    stats["last_completion_tokens"] = int(usage.get("completion_tokens") or 0)



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
        if (
            _should_nudge_continue(content, self.nudge_count, self.max_nudges)
            and round_idx + 1 < self.max_gather
        ):
            self.nudge_count += 1
            echo_status(status_mod.msg_paused_continuing())
            messages.append(status_mod.nudge_message())
            return "nudge"
        if _warn_stopped_unfinished(content, self.nudge_count, self.max_nudges):
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


def _dispatch_tools(
    turn: GatherTurn, tool_calls: list, *, round_idx: int, impls: dict,
    echo_tool, echo_status, stats, session_log, workspace_root, messages: list,
    cfg: Config, model: str,
) -> None:
    from .harness import traces as traces_mod

    round_media = []
    prepared = []
    for call_idx, call in enumerate(tool_calls):
        turn.turn_tools += 1
        fn = call.get("function", {})
        name, args = fn.get("name", ""), fn.get("arguments", "")
        arg_preview = tools.format_tool_preview(
            name, args, workspace_root=workspace_root, cfg=cfg,
        )
        echo_tool(name, arg_preview, stats)
        prepared.append((call, call_idx, name, args, arg_preview))
    raw_results = tools.run_tool_calls(
        impls, [(name, args) for _call, _idx, name, args, _prev in prepared],
        cfg=cfg,
    )
    for (call, call_idx, name, _args, arg_preview), raw_result in zip(
        prepared, raw_results,
    ):
        result, attachments = tools.unwrap_tool_result(raw_result)
        traces_mod.note_tool(name, result, round_idx=round_idx)
        notice = tools.user_notice(name, result)
        if notice:
            echo_status(notice)
        from .spirit import note_tool
        note_tool(
            cfg, name=name, args=_args, result=result, session_log=session_log,
            ok=not str(result).startswith(("error", "ERROR", "refused", "DENIED")),
        )
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


@dataclass
class ActTurn:
    """Knobs ``ActDisplay`` does not carry. ``act()`` still accepts keywords."""

    confirm_gate: object = None
    stats: "dict | None" = None
    session_log: "Path | None" = None
    echo_delta: object = None
    on_thinking: object = None
    readonly: bool = False
    no_tools: bool = False
    max_rounds: "int | None" = None
    extra_readable: "list | None" = None
    tool_only: "frozenset[str] | None" = None


def act(cfg: Config, model: str, messages: list, session_log: "Path | None" = None,
        confirm_gate=None, echo=print, echo_tool=None, echo_delta=None,
        echo_status=None, stats: "dict | None" = None, echo_round=None,
        on_thinking=None, context_limit: int = 0,
        context_reserve: int = DEFAULTS["context_reserve"],
        workspace_root: "Path | None" = None,
        readonly: bool = False,
        no_tools: bool = False,
        max_rounds: "int | None" = None,
        extra_readable: "list | None" = None,
        tool_only: "frozenset[str] | None" = None) -> list:
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
    from .harness import traces as traces_mod
    from .core import usage as usage_mod

    opts = ActTurn(
        confirm_gate=confirm_gate,
        stats=stats,
        session_log=session_log,
        echo_delta=echo_delta,
        on_thinking=on_thinking,
        readonly=readonly,
        no_tools=no_tools,
        max_rounds=max_rounds,
        extra_readable=extra_readable,
        tool_only=tool_only,
    )
    usage_mod.record("agent.act", readonly=opts.readonly, no_tools=opts.no_tools)
    if echo_status is None:
        echo_status = echo
    if echo_tool is None:
        def echo_tool(name, preview, stats=None):
            preview = preview or ""
            path, sep, rest = preview.partition("\n")
            if sep:
                echo(f"  ⚙ {name}")
                if path:
                    echo(f"    {path}")
                if rest:
                    echo(f"    {rest}")
                return
            echo(f"  ⚙ {name}({preview})")
    color = cfg_bool(cfg, "color")
    if opts.no_tools:
        tool_specs, impls = None, {}
    else:
        tool_specs, impls = tools.build_tools(
            cfg, confirm_gate=opts.confirm_gate, workspace_root=workspace_root,
            readonly=opts.readonly, extra_readable=opts.extra_readable,
            only=opts.tool_only,
        )
    use_stream = cfg_bool(cfg, "stream")
    gather_budget = (
        opts.max_rounds if opts.max_rounds is not None else cfg_int(cfg, "max_rounds")
    )
    turn = GatherTurn(
        max_gather=max(1, int(gather_budget)),
        max_nudges=max(0, cfg_int(cfg, "max_continue_nudges")),
        checkpoint=len(messages),
    )
    prev_session = memory.set_active_session(opts.session_log)
    act_ok = False

    def _finish() -> list:
        nonlocal act_ok
        act_ok = True
        turn.commit_stats(opts.stats)
        return messages

    try:
        traces_mod.start_turn()
        for round_idx in range(turn.max_gather + 1):
            is_answer = turn.is_answer_round(round_idx, echo_status)
            if is_answer and not (
                messages and status_mod.is_answer_message(messages[-1])
            ):
                messages.append(status_mod.answer_message())
            turn.warn_context(
                messages, opts.stats, context_limit, context_reserve, echo_status,
            )

            round_ui = RoundDisplay(
                opts.echo_delta, color=color, use_stream=use_stream,
                on_thinking=opts.on_thinking,
            )
            try:
                round_specs = None if (is_answer or opts.no_tools) else tool_specs
                msg, chat_usage = _chat(
                    cfg, model, messages, round_specs,
                    **round_ui.chat_kwargs(),
                )
                _accumulate_usage(opts.stats, chat_usage)
                turn.turn_rounds += 1
                tool_calls, content = round_ui.settle(msg, is_answer or opts.no_tools)
                halted = bool(msg.get("_halted"))

                assistant_entry = {"role": "assistant", "content": content or ""}
                if tool_calls:
                    assistant_entry["tool_calls"] = tool_calls
                messages.append(assistant_entry)
                round_ui.emit(echo, content, opts.session_log)

                if echo_round is not None:
                    echo_round(
                        round_idx + 1, opts.stats, messages,
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
                    echo_tool=echo_tool, echo_status=echo_status, stats=opts.stats,
                    session_log=opts.session_log,
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
        GatherTurn.mark_interrupted(opts.stats)
        _rollback_incomplete_messages(messages, turn.checkpoint)
        raise
    finally:
        usage_mod.record(
            "agent.act.finish",
            readonly=opts.readonly,
            no_tools=opts.no_tools,
            rounds=turn.turn_rounds,
            tools=turn.turn_tools,
            interrupted=bool(opts.stats and opts.stats.get("interrupted")),
            ok=act_ok,
        )
        from .harness.harness_bundle import bundle_id as harness_bundle_id_for

        traces_mod.finish_turn(
            rounds=turn.turn_rounds,
            tools=turn.turn_tools,
            interrupted=bool(opts.stats and opts.stats.get("interrupted")),
            ok=act_ok,
            readonly=opts.readonly,
            no_tools=opts.no_tools,
            output=traces_mod.output_excerpt(messages),
            harness_bundle_id=harness_bundle_id_for(
                cfg, workspace_root=workspace_root,
            ),
        )
        memory.set_active_session(prev_session)
