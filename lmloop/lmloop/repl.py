"""Interactive REPL for lmloop."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import agent, loop as loop_mod, memory
from . import graph as graph_mod
from .display import THINK_LINE_PREFIX
from .commands import slash_command_metas
from .config import project_slug
from .files_index import AtRefExpansion, collect_at_refs
from .status import MSG_RESUME, msg_graph_continue, msg_until_continue, status as status_line
from .ui import Console, ask_until_gate, ask_yes_no, fresh_stats, make_confirm_gate

_THINKING_HISTORY_MAX = 30
_UNTIL_FOLLOWUP = "Ready to continue from this until-run. Ask a follow-up."
_GRAPH_FOLLOWUP = "Ready to continue from this graph-run. Ask a follow-up."


@dataclass
class SlashCommand:
    name: str
    desc: str
    handler: Callable[["SessionState", str], bool]
    arg_hint: str = ""
    accepts_arg: bool = False
    exits: bool = False
    hidden: bool = False


@dataclass
class SessionState:
    cfg: dict
    model: str
    messages: list
    session_log: Path
    stats: dict
    console: Console
    context_limit: int = 0
    prompt_session: object = None
    workspace_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    # Prior-turn thinking for Ctrl+O (newest last). Live thinking is not stored
    # here until the turn retires it.
    thinking_history: list = field(default_factory=list)
    until_run: "Path | None" = None
    graph_run: "Path | None" = None

    @property
    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user")

    @property
    def context_reserve(self) -> int:
        return int(self.cfg.get("context_reserve") or 2048)

    def push_thinking(self, text: str) -> None:
        """Store a thinking block for later review."""
        body = (text or "").rstrip()
        if not body:
            return
        self.thinking_history.append(body)
        if len(self.thinking_history) > _THINKING_HISTORY_MAX:
            self.thinking_history = self.thinking_history[-_THINKING_HISTORY_MAX:]

    def thinking_entries(self) -> list:
        return list(self.thinking_history)

    def prior_thinking_transcript(self) -> "str | None":
        """All thinking except the latest, oldest-first (already seen live).

        Latest thinking was shown while streaming and erased on tools/answer;
        Ctrl+O is for earlier turns, not a newest→oldest cycle.
        """
        entries = self.thinking_history
        if len(entries) < 2:
            return None
        prior = entries[:-1]
        parts = []
        for i, text in enumerate(prior, 1):
            parts.append(f"── thinking (turn {i}/{len(prior)}) ──")
            for line in (text or "").splitlines() or [""]:
                parts.append(f"{THINK_LINE_PREFIX}{line}")
            parts.append("")
        parts.append("── end ──")
        parts.append("(q returns · this is prior-turn thinking, oldest first)")
        return "\n".join(parts) + "\n"

    def thinking_count(self) -> int:
        return len(self.thinking_history)

    def prior_thinking_count(self) -> int:
        return max(0, self.thinking_count() - 1)


def _refresh_context_limit(state: SessionState) -> None:
    state.context_limit = agent.get_context_limit(state.model, state.cfg)


def _reset_session(state: SessionState, keep_stats: bool = False) -> None:
    state.messages = _fresh_messages(state.cfg, state.workspace_root)
    state.session_log = memory.new_session_log()
    state.thinking_history = []
    state.until_run = None
    state.graph_run = None
    if not keep_stats:
        state.stats = fresh_stats()


def _fresh_messages(cfg: dict, workspace_root: "Path | None" = None) -> list:
    return [{"role": "system", "content": agent.system_prompt(cfg, workspace_root)}]


def _echo_assistant(console: Console):
    """Echo callback for model replies (markdown). Status uses echo_status."""
    def echo(text: str) -> None:
        if not text:
            return
        console.print_markdown(text)
    return echo


def _echo_at_refs(state: SessionState, expansion: AtRefExpansion) -> None:
    """Tell the user which @paths resolved (or did not)."""
    for ref in expansion.refs:
        state.console.hint(f"[referenced {ref.token} → {ref.resolved}]")
    for token in expansion.missing:
        state.console.hint(f"[no file at @{token}]")


def _run_turn(state: SessionState, user_text: str, confirm_gate) -> bool:
    """Run one user turn. Returns True if act() completed."""
    # Expand @path refs for every turn (freeform, /skill, /continue, …).
    expansion = collect_at_refs(user_text, cwd=state.workspace_root)
    user_text = expansion.text
    extra_readable = [ref.resolved for ref in expansion.refs]
    _echo_at_refs(state, expansion)
    memory.log_event(state.session_log, "user", user_text)
    state.messages.append({"role": "user", "content": user_text})
    try:
        agent.act(
            state.cfg, state.model, state.messages,
            session_log=state.session_log,
            confirm_gate=confirm_gate,
            stats=state.stats,
            echo=_echo_assistant(state.console),
            echo_status=state.console.hint,
            echo_tool=state.console.tool_call,
            echo_round=state.console.round_usage,
            on_thinking=state.push_thinking,
            context_limit=state.context_limit,
            context_reserve=state.context_reserve,
            workspace_root=state.workspace_root,
            extra_readable=extra_readable,
        )
        footer = state.console.stats_footer(
            state.stats, state.context_limit, state.messages, state.context_reserve,
        )
        if footer:
            print(footer)
        return True
    except agent.ServerError as e:
        memory.log_event(state.session_log, "system", f"error: {e}")
        state.console.error(f"error: {e}")
        state.console.hint(f"  conversation kept — type to steer, or {MSG_RESUME}")
        return False
    except KeyboardInterrupt:
        memory.log_event(state.session_log, "system", "interrupted")
        state.console.hint(
            f"\n[interrupted — conversation kept; type to steer, or {MSG_RESUME}]"
        )
        return False
    except Exception as e:
        memory.log_event(state.session_log, "system", f"error: {type(e).__name__}: {e}")
        state.console.error(f"error: {type(e).__name__}: {e}")
        state.console.hint(f"  conversation kept — type to steer, or {MSG_RESUME}")
        return False


def _cmd_help(state: SessionState, _arg: str) -> bool:
    print(state.console.help_text(SLASH_COMMANDS))
    return True


def _cmd_stats(state: SessionState, _arg: str) -> bool:
    learnings = len(memory.get_learnings(limit=100))
    decisions = len(memory.get_decisions(limit=100))
    print(state.console.stats_detail(
        state.stats, state.model, state.messages, state.session_log,
        learnings, decisions, state.context_limit, state.context_reserve,
    ))
    return True


def _cmd_new(state: SessionState, _arg: str) -> bool:
    _reset_session(state)
    state.console.info("[fresh conversation]")
    return True


def _cmd_model(state: SessionState, arg: str) -> bool:
    if arg:
        state.model = arg
        _refresh_context_limit(state)
        msg = f"[model -> {state.model}]"
        if state.context_limit:
            msg += f" · context {state.context_limit // 1000}k"
        state.console.info(msg)
    else:
        models = agent.list_models(state.cfg["base_url"])
        state.console.info("\n".join(models) or "(none)")
    return True


def _cmd_memory(state: SessionState, arg: str, confirm_gate) -> bool:
    parts = (arg or "").split(None, 1)
    if parts and parts[0] == "mine":
        rest = parts[1] if len(parts) > 1 else ""
        return _cmd_memory_mine(state, rest, confirm_gate)
    if parts and parts[0] == "graph":
        return _cmd_memory_graph(state)
    if parts and parts[0] == "reconcile":
        return _cmd_memory_reconcile(state, confirm_gate)
    rows = memory.get_learnings(query=arg, limit=30)
    for r in rows:
        state.console.info(
            f"- [{r['key']}] ({r['type']}, {r['confidence']}/10) {r['insight']}"
        )
    if not rows:
        state.console.info("(no learnings yet — run tasks, then /memory mine)")
    return True


def _cmd_memory_graph(state: SessionState) -> bool:
    if not state.cfg.get("use_graph"):
        state.console.info("knowledge graph is off — `lmloop config set use_graph true`")
        return True
    memory.ensure_graph(state.cfg)
    state.console.info(memory.graph_stats())
    return True


def _cmd_memory_reconcile(state: SessionState, confirm_gate) -> bool:
    if not state.cfg.get("use_graph"):
        state.console.info("knowledge graph is off — `lmloop config set use_graph true`")
        return True
    memory.ensure_graph(state.cfg)
    cluster = memory.contradiction_clusters()
    if cluster.startswith("(no "):
        state.console.info(cluster)
        return True
    try:
        prompt = agent.load_skill("_reconcile") + "\n\n" + cluster
    except FileNotFoundError as e:
        state.console.error(str(e))
        return True
    state.console.hint("[memory reconcile · current conversation unchanged]")
    _isolated_act(state, prompt, confirm_gate, log_label="/memory reconcile")
    return True


def _cmd_save(state: SessionState, arg: str, confirm_gate) -> bool:
    title = arg or "session-checkpoint"
    transcript = memory.format_messages_transcript(state.messages)
    if not transcript:
        state.console.info("(nothing to checkpoint)")
        return True
    prompt = (
        "Write a checkpoint of this session for a future session to resume from: "
        "what we're working on, key findings and decisions, and remaining work. "
        "Reply with the checkpoint text only — do not call any tools.\n\n"
        "Transcript:\n" + transcript
    )
    state.console.hint("[save · current conversation unchanged]")
    result = _isolated_act(state, prompt, confirm_gate, log_label="/save")
    if result is None:
        state.console.hint("[checkpoint not saved]")
        return True
    body = loop_mod.last_assistant(result)
    if not body:
        state.console.hint("[checkpoint not saved — empty reply]")
        return True
    path = memory.save_checkpoint(title, body)
    state.console.info(f"[checkpoint saved: {path}]")
    return True


def mine_sessions(cfg: dict, model: str, paths, console: Console, confirm_gate,
                  transcript: "str | None" = None) -> int:
    """Run the retro skill without touching a live REPL thread."""
    if transcript is None:
        if not paths:
            console.info("no sessions recorded yet")
            return 0
        transcript = memory.format_sessions_for_retro(paths)
        label = f"/memory mine over {len(paths)} session(s)"
    else:
        if not transcript.strip():
            console.info("no sessions recorded yet")
            return 0
        label = "/memory mine this session"
    try:
        task = agent.load_skill("retro") + "\n\n" + transcript
    except FileNotFoundError as e:
        console.error(str(e))
        return 1
    if cfg.get("use_graph"):
        memory.ensure_graph(cfg)
        try:
            task += "\n\n" + agent.load_skill("_graph_mine")
        except FileNotFoundError:
            pass
    result = loop_mod.isolated_act(
        cfg, model, task,
        confirm_gate=confirm_gate,
        echo=_echo_assistant(console),
        echo_status=console.hint,
        echo_error=console.error,
        echo_tool=console.tool_call,
        echo_round=console.round_usage,
        context_limit=agent.get_context_limit(model, cfg),
        context_reserve=int(cfg.get("context_reserve") or 2048),
        workspace_root=Path.cwd().resolve(),
        log_label=label,
    )
    if result is None:
        return 1
    console.hint(f"[learnings → {memory.learnings_file()}]")
    return 0


def _cmd_memory_mine(state: SessionState, arg: str, confirm_gate) -> bool:
    arg = (arg or "").strip()
    if arg.isdigit() and int(arg) > 0:
        n = int(arg)
        current = state.session_log.resolve()
        paths = [
            p for p in memory.all_sessions()
            if p.resolve() != current
        ][-n:]
        if not paths:
            state.console.info(
                "no prior sessions to mine — try /memory mine for this session"
            )
            return True
        label = f"last {len(paths)} session(s)"
        state.console.hint(f"[memory mine · {label} · current conversation unchanged]")
        try:
            mine_sessions(state.cfg, state.model, paths, state.console, confirm_gate)
        except KeyboardInterrupt:
            state.console.hint(
                f"\n[interrupted — current conversation unchanged; {MSG_RESUME}]"
            )
        return True
    if arg:
        state.console.info("usage: /memory mine [n]")
        state.console.info("  /memory mine       mine this session")
        state.console.info("  /memory mine 3     mine last 3 sessions")
        return True
    transcript = memory.format_messages_transcript(state.messages)
    if not transcript:
        state.console.info("(no session transcript to mine)")
        return True
    state.console.hint("[memory mine · this session · current conversation unchanged]")
    try:
        mine_sessions(
            state.cfg, state.model, None, state.console, confirm_gate,
            transcript=transcript,
        )
    except KeyboardInterrupt:
        state.console.hint(
            f"\n[interrupted — current conversation unchanged; {MSG_RESUME}]"
        )
    return True


def _cmd_retro(state: SessionState, arg: str, confirm_gate) -> bool:
    state.console.hint("[memory mine]")
    return _cmd_memory_mine(state, arg, confirm_gate)


def _run_named_skill(state: SessionState, name: str, task: str, confirm_gate) -> bool:
    try:
        prompt = agent.load_skill(name, public_only=True)
    except FileNotFoundError as e:
        state.console.error(str(e))
        return True
    _run_turn(state, prompt + ("\n\nTask: " + task if task else ""), confirm_gate)
    memory.record_skill_use(name, session=state.session_log, cfg=state.cfg)
    return True


def _cmd_skill(state: SessionState, arg: str, confirm_gate) -> bool:
    rest = arg.split(None, 1)
    if not rest:
        state.console.info("usage: /skill <name> [task]")
        state.console.info(f"  skills: {', '.join(agent.list_skills())}")
        return True
    name = rest[0]
    task = rest[1] if len(rest) > 1 else ""
    return _run_named_skill(state, name, task, confirm_gate)


def _cmd_undo(state: SessionState, _arg: str) -> bool:
    msgs = state.messages
    last_user = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "user":
            last_user = i
            break
    if last_user is None:
        state.console.hint("[nothing to undo]")
        return True
    if last_user == 0 or (last_user == 1 and msgs[0]["role"] == "system"):
        state.console.hint("[cannot undo the first user message]")
        return True
    removed = len(msgs) - last_user
    del msgs[last_user:]
    state.console.info(f"[undid last turn — removed {removed} message(s)]")
    return True


def _cmd_compact(state: SessionState, arg: str, confirm_gate) -> bool:
    transcript = memory.format_messages_transcript(state.messages, max_chars=12000)
    if not transcript:
        state.console.info("(nothing to compact)")
        return True
    try:
        prompt = agent.load_skill("compact") + "\n\nTranscript:\n" + transcript
    except FileNotFoundError as e:
        state.console.error(str(e))
        return True
    extra_readable = []
    if arg:
        expansion = collect_at_refs(arg, cwd=state.workspace_root)
        prompt += "\n\nFocus: " + expansion.text
        extra_readable = [ref.resolved for ref in expansion.refs]
        _echo_at_refs(state, expansion)
    state.console.hint("[compact · current conversation unchanged until you replace it]")
    result = _isolated_act(
        state, prompt, confirm_gate, log_label="/compact",
        extra_readable=extra_readable,
    )
    if result is None:
        return True
    summary = loop_mod.last_assistant(result)
    if not summary:
        return True
    state.console.hint("Replace the in-memory thread with this summary?")
    if ask_yes_no("Replace thread with summary? [y/N] "):
        system = _fresh_messages(state.cfg, state.workspace_root)[0]
        handoff = (
            "Context was compacted. Continue from this handoff summary:\n\n" + summary
        )
        _adopt_thread(
            state,
            [
                system,
                {"role": "user", "content": handoff},
                {
                    "role": "assistant",
                    "content": "Understood — continuing from the compacted handoff.",
                },
            ],
            log=memory.new_session_log(),
            replay=True,
            reset_stats=False,
        )
        state.console.info("[thread replaced with compact summary]")
    return True


def _isolated_act(state: SessionState, user_text: str, confirm_gate,
                  *, log_label: str, extra_readable=None) -> "list | None":
    """Run act() on a fresh thread. Returns messages, or None on failure."""
    try:
        result = loop_mod.isolated_act(
            state.cfg, state.model, user_text,
            confirm_gate=confirm_gate,
            echo=_echo_assistant(state.console),
            echo_status=state.console.hint,
            echo_error=state.console.error,
            echo_tool=state.console.tool_call,
            echo_round=state.console.round_usage,
            context_limit=state.context_limit,
            context_reserve=state.context_reserve,
            workspace_root=state.workspace_root,
            log_label=log_label,
            extra_readable=extra_readable,
        )
    except KeyboardInterrupt:
        return None
    if result is None:
        return None
    messages, _session_log = result
    return messages


def _cmd_skills(state: SessionState, arg: str, confirm_gate) -> bool:
    parts = arg.split(None, 2)
    if parts and parts[0] == "new":
        if len(parts) < 2:
            state.console.info("usage: /skills new <name> [brief]")
            return True
        from .cli import cmd_skills_new
        cmd_skills_new(
            state.cfg, parts[1], parts[2] if len(parts) > 2 else "", state.console,
        )
        # Rebuild slash commands so a newly saved skill gets /name immediately
        global SLASH_COMMANDS
        SLASH_COMMANDS = _build_slash_commands(confirm_gate)
        return True

    from .cli import cmd_skills
    cmd_skills(state.console, repl=True)
    return True


def _cmd_history(state: SessionState, arg: str) -> bool:
    limit = int(arg) if arg.isdigit() and int(arg) > 0 else 10
    all_rows = memory.all_sessions()
    sessions = all_rows[-limit:]
    if not sessions:
        state.console.info("(no sessions yet)")
        return True
    current = state.session_log.resolve()
    base = len(all_rows) - len(sessions) + 1
    for i, path in enumerate(sessions, base):
        tag = " (current)" if path.resolve() == current else ""
        preview = memory.session_preview(path)
        state.console.info(f"  {i:2}. {path.stem}{tag}  — {preview}")
    state.console.hint("  restore with: /restore <#|stem|latest>")
    return True


def _cmd_checkpoints(state: SessionState, arg: str) -> bool:
    limit = int(arg) if arg.isdigit() and int(arg) > 0 else 10
    all_rows = memory.all_checkpoints()
    cps = all_rows[-limit:]
    if not cps:
        state.console.info("(no checkpoints — use /save [title])")
        return True
    base = len(all_rows) - len(cps) + 1
    for i, path in enumerate(cps, base):
        state.console.info(f"  {i:2}. {path.stem}")
    state.console.hint("  restore with: /restore checkpoint <#|stem|latest>")
    return True


def _cmd_decisions(state: SessionState, _arg: str) -> bool:
    rows = memory.get_decisions(limit=30)
    if not rows:
        state.console.info("(no decisions logged yet)")
        return True
    for d in rows:
        line = f"- [{d['id']}] {d['date'][:10]} {d['decision']}"
        if d.get("rationale"):
            line += f"  (why: {d['rationale']})"
        state.console.info(line)
    return True


def _restore_usage(console: Console) -> None:
    console.info("usage: /restore [session|checkpoint] <latest|#|stem> [fresh]")
    console.info("  /restore 2                 reload session 2, show last result")
    console.info("  /restore session 2 fresh   copy into a new session log")
    console.info("  /restore checkpoint latest load checkpoint as a handoff")


def parse_restore_arg(arg: str):
    """Return (kind, query, fresh) or None when usage should be shown."""
    parts = [p for p in (arg or "").split() if p]
    if not parts:
        return None
    fresh = False
    if parts[-1].lower() in ("fresh", "new"):
        fresh = True
        parts = parts[:-1]
        if not parts:
            return None
    head = parts[0].lower()
    if head in ("checkpoint", "session"):
        if len(parts) > 2:
            return None
        query = parts[1] if len(parts) > 1 else "latest"
        return head, query, fresh
    if len(parts) != 1:
        return None
    return "session", parts[0], fresh


def _adopt_thread(state: SessionState, messages: list, *, log: "Path | None",
                  replay: bool, reset_stats: bool = True) -> None:
    """Replace the in-memory thread. ``replay`` copies turns into a new log."""
    state.messages = messages
    state.thinking_history = []
    if reset_stats:
        state.stats = fresh_stats()
    if log is not None:
        state.session_log = log
    if replay:
        for m in messages:
            if m.get("role") == "system":
                continue
            memory.log_event(state.session_log, m["role"], m.get("content") or "")


def _cmd_restore(state: SessionState, arg: str) -> bool:
    parsed = parse_restore_arg(arg)
    if parsed is None:
        _restore_usage(state.console)
        return True
    kind, query, fresh = parsed
    if kind == "checkpoint":
        path = memory.resolve_checkpoint(query)
        if not path:
            state.console.error("checkpoint not found — try /checkpoints")
            return True
        body = memory.read_checkpoint_body(path)
        system = _fresh_messages(state.cfg, state.workspace_root)[0]
        messages = [
            system,
            {"role": "user", "content": f"Resume from checkpoint ({path.stem}):\n\n{body}"},
            {"role": "assistant", "content": "Ready to continue from this checkpoint."},
        ]
        # Checkpoints are summaries, not session files — always a new log.
        _adopt_thread(state, messages, log=memory.new_session_log(), replay=True)
        if body:
            state.console.print_markdown(body)
        state.console.info(f"[restored checkpoint {path.stem} — type to continue]")
        return True
    path = memory.resolve_session(query)
    if not path:
        state.console.error("session not found — try /history")
        return True
    if path.resolve() == state.session_log.resolve() and not fresh:
        state.console.info("[already in this session]")
        return True
    turns = memory.session_messages(path)
    if not turns:
        state.console.error("session is empty — nothing to restore")
        return True
    system = _fresh_messages(state.cfg, state.workspace_root)[0]
    messages = [system] + turns
    if fresh:
        _adopt_thread(state, messages, log=memory.new_session_log(), replay=True)
        tag = f"fresh copy of session {path.stem}"
    else:
        _adopt_thread(state, messages, log=path, replay=False)
        tag = f"session {path.stem}"
    result = memory.session_last_assistant(path)
    if result:
        state.console.print_markdown(result)
    extra = ""
    if memory.session_interrupted(path):
        extra = f"; interrupted — {MSG_RESUME}"
    n = len(turns)
    state.console.info(f"[restored {tag} — {n} turn(s){extra}]")
    return True


def _until_callbacks(state: SessionState, confirm_gate):
    def mine(paths):
        if not paths:
            return
        mine_sessions(state.cfg, state.model, paths, state.console, confirm_gate)

    return dict(
        confirm_gate=confirm_gate,
        echo=_echo_assistant(state.console),
        echo_status=state.console.hint,
        echo_error=state.console.error,
        echo_tool=state.console.tool_call,
        echo_round=state.console.round_usage,
        context_limit=state.context_limit,
        context_reserve=state.context_reserve,
        workspace_root=state.workspace_root,
        ask_gate=ask_until_gate,
        mine=mine if state.cfg.get("until_mine", True) else None,
    )


def attach_until_result(state: SessionState, run: loop_mod.UntilRun) -> None:
    """Append the until outcome to the live thread so follow-ups have context."""
    loaded = loop_mod.UntilRun.load(run.path) if run.path.exists() else run
    if loaded.is_done():
        state.until_run = None
        last = loaded.last_work()
        if last and last.get("role") == "gate" and last.get("status") == "no":
            outcome = "stopped"
        else:
            outcome = "done"
        state.console.hint(msg_until_continue())
    elif loaded.is_paused():
        state.until_run = loaded.path
        outcome = "paused"
    else:
        state.until_run = loaded.path
        outcome = "stopped"
        state.console.hint(status_line(f"until stopped — {MSG_RESUME}"))
    summary = loaded.last_handoff() or loaded.last_maker_handoff() or "(no handoff)"
    user = f"Until-run ({outcome}). Goal: {loaded.goal}\n\n{summary}"
    state.messages.append({"role": "user", "content": user})
    state.messages.append({"role": "assistant", "content": _UNTIL_FOLLOWUP})
    memory.log_event(state.session_log, "user", user)
    memory.log_event(state.session_log, "assistant", _UNTIL_FOLLOWUP)


def _advance_until(state: SessionState, run: loop_mod.UntilRun, confirm_gate) -> None:
    state.until_run = run.path
    loop_mod.run_until(state.cfg, state.model, run=run, **_until_callbacks(state, confirm_gate))
    attach_until_result(state, run)


def _cmd_until(state: SessionState, arg: str, confirm_gate) -> bool:
    goal, check_cmd, err = loop_mod.parse_until_arg_line(arg)
    if err:
        state.console.info(err)
        state.console.info("  /until [--check <cmd>] <goal>")
        state.console.info("  /continue resumes a paused until-run")
        return True
    hint = loop_mod.superseded_until_hint()
    run = loop_mod.UntilRun.create(goal, check_cmd=check_cmd)
    state.console.hint(f"[until · {goal}]")
    if hint:
        state.console.hint(hint)
    _advance_until(state, run, confirm_gate)
    return True


def _graph_callbacks(state: SessionState, confirm_gate):
    def mine(paths):
        if not paths:
            return
        mine_sessions(state.cfg, state.model, paths, state.console, confirm_gate)

    return dict(
        confirm_gate=confirm_gate,
        echo=_echo_assistant(state.console),
        echo_status=state.console.hint,
        echo_error=state.console.error,
        echo_tool=state.console.tool_call,
        echo_round=state.console.round_usage,
        context_limit=state.context_limit,
        context_reserve=state.context_reserve,
        workspace_root=state.workspace_root,
        ask_gate=ask_until_gate,
        mine=mine if state.cfg.get("graph_mine", True) else None,
    )


def attach_graph_result(state: SessionState, run: graph_mod.GraphRun) -> None:
    """Append the graph outcome to the live thread so follow-ups have context."""
    loaded = graph_mod.GraphRun.load(run.path) if run.path.exists() else run
    if loaded.is_done():
        state.graph_run = None
        last = loaded.last_work()
        if last and last.get("role") == "gate" and last.get("status") == "no":
            outcome = "stopped"
        else:
            outcome = "done"
        state.console.hint(msg_graph_continue())
    elif loaded.is_paused():
        state.graph_run = loaded.path
        outcome = "paused"
    else:
        state.graph_run = loaded.path
        outcome = "stopped"
        state.console.hint(status_line(f"graph stopped — {MSG_RESUME}"))
    summary = loaded.last_handoff() or "(no handoff)"
    user = f"Graph-run ({outcome}). Graph: {loaded.name}\n\n{summary}"
    state.messages.append({"role": "user", "content": user})
    state.messages.append({"role": "assistant", "content": _GRAPH_FOLLOWUP})
    memory.log_event(state.session_log, "user", user)
    memory.log_event(state.session_log, "assistant", _GRAPH_FOLLOWUP)


def _advance_graph(state: SessionState, run: graph_mod.GraphRun,
                   defn: graph_mod.GraphDef, confirm_gate) -> None:
    state.graph_run = run.path
    graph_mod.run_graph(
        state.cfg, state.model, run=run, defn=defn,
        **_graph_callbacks(state, confirm_gate),
    )
    attach_graph_result(state, run)


def _cmd_graph(state: SessionState, arg: str, confirm_gate) -> bool:
    name = (arg or "").strip().split()[0] if (arg or "").strip() else ""
    if not name:
        state.console.info("usage: /graph <name>")
        known = ", ".join(graph_mod.list_graphs()) or "(none)"
        state.console.info(f"  graphs: {known}")
        state.console.info("  /continue resumes a paused graph-run")
        return True
    try:
        defn = graph_mod.load_graph(name)
    except graph_mod.GraphError as e:
        state.console.error(str(e))
        return True
    hint = graph_mod.superseded_graph_hint()
    run = graph_mod.GraphRun.create(name)
    state.console.hint(f"[graph · {name}]")
    if hint:
        state.console.hint(hint)
    _advance_graph(state, run, defn, confirm_gate)
    return True


def _cmd_continue(state: SessionState, arg: str, confirm_gate) -> bool:
    if state.graph_run and state.graph_run.exists():
        run = graph_mod.GraphRun.load(state.graph_run)
        if not run.is_done():
            try:
                defn = graph_mod.load_graph(run.name)
            except graph_mod.GraphError as e:
                state.console.error(str(e))
                return True
            state.console.hint("[graph · resume]")
            _advance_graph(state, run, defn, confirm_gate)
            return True
        state.graph_run = None
    if state.until_run and state.until_run.exists():
        run = loop_mod.UntilRun.load(state.until_run)
        if not run.is_done():
            state.console.hint("[until · resume]")
            _advance_until(state, run, confirm_gate)
            return True
        state.until_run = None
    _run_turn(state, arg or "Continue where you left off.", confirm_gate)
    return True


def _cmd_context(state: SessionState, _arg: str) -> bool:
    block = memory.context_block(state.cfg)
    if block:
        state.console.info(block)
    else:
        state.console.info("(no learnings, decisions, or recent checkpoint in context)")
    return True


def _cmd_transcript(state: SessionState, _arg: str) -> bool:
    """Open the rendered session transcript in less (q to return)."""
    from .markdown_view import page_transcript
    state.console.hint("[opening transcript in less — press q to return]")
    page_transcript(state.messages, color=state.console.t._on)
    return True


def _build_slash_commands(confirm_gate) -> list:
    """Bind handlers to CommandMeta rows from commands.py (single name source)."""
    handlers = {
        "help": _cmd_help,
        "stats": _cmd_stats,
        "transcript": _cmd_transcript,
        "skills": lambda s, a: _cmd_skills(s, a, confirm_gate),
        "history": _cmd_history,
        "checkpoints": _cmd_checkpoints,
        "restore": _cmd_restore,
        "decisions": _cmd_decisions,
        "context": _cmd_context,
        "continue": lambda s, a: _cmd_continue(s, a, confirm_gate),
        "undo": _cmd_undo,
        "compact": lambda s, a: _cmd_compact(s, a, confirm_gate),
        "new": _cmd_new,
        "model": _cmd_model,
        "memory": lambda s, a: _cmd_memory(s, a, confirm_gate),
        "until": lambda s, a: _cmd_until(s, a, confirm_gate),
        "graph": lambda s, a: _cmd_graph(s, a, confirm_gate),
        "save": lambda s, a: _cmd_save(s, a, confirm_gate),
        "skill": lambda s, a: _cmd_skill(s, a, confirm_gate),
        "quit": lambda s, a: False,
        "exit": lambda s, a: False,
        "q": lambda s, a: False,
    }
    commands = []
    for meta in slash_command_metas():
        handler = handlers.get(meta.name)
        if handler is None:
            raise RuntimeError(f"no handler for /{meta.name}")
        commands.append(SlashCommand(
            f"/{meta.name}", meta.desc, handler,
            arg_hint=meta.arg_hint, accepts_arg=meta.accepts_arg, exits=meta.exits,
        ))
    commands.append(SlashCommand(
        "/retro", "alias for /memory mine",
        lambda s, a: _cmd_retro(s, a, confirm_gate),
        arg_hint="[n]", accepts_arg=True, hidden=True,
    ))
    taken = {c.name for c in commands}
    for name in agent.list_skills():
        slash = f"/{name}"
        if slash in taken:
            continue
        blurb = agent.skill_blurb(name) or f"run {name} skill"
        commands.append(SlashCommand(
            slash, blurb,
            lambda s, a, n=name: _run_named_skill(s, n, a, confirm_gate),
            "[task]", accepts_arg=True,
        ))
    return commands


# Built at runtime in run_repl so confirm_gate is in closures
SLASH_COMMANDS: list = []


def _command_names() -> list[str]:
    return [c.name for c in SLASH_COMMANDS]


def _is_known_slash(line: str) -> bool:
    if line in _command_names():
        return True
    for cmd in SLASH_COMMANDS:
        if cmd.accepts_arg and (line == cmd.name or line.startswith(cmd.name + " ")):
            return True
    return False


def _suggest_slash_command(line: str) -> "str | None":
    cmd = line.split(None, 1)[0]
    matches = [c.name for c in SLASH_COMMANDS if c.name.startswith(cmd)]
    if len(matches) == 1 and matches[0] != cmd:
        return matches[0]
    return None


def _dispatch_slash(state: SessionState, line: str) -> bool:
    """Handle a slash command. Returns False to exit REPL."""
    for cmd in SLASH_COMMANDS:
        if cmd.exits and line in (cmd.name,):
            return False
        if line == cmd.name:
            return cmd.handler(state, "")
        if cmd.accepts_arg and line.startswith(cmd.name + " "):
            return cmd.handler(state, line[len(cmd.name):].strip())
    return True


def _require_prompt_toolkit(console: Console) -> bool:
    try:
        import prompt_toolkit  # noqa: F401
        return True
    except ImportError:
        console.error(
            "error: prompt_toolkit is required for the interactive REPL.\n"
            "  Run:  bash lmloop/setup-lmloop.sh\n"
            "  Or:   python3 -m pip install -r lmloop/requirements.txt"
        )
        return False


def run_repl(cfg: dict, console: "Console | None" = None,
             skill: "str | None" = None, first_task: "str | None" = None,
             until_run: "loop_mod.UntilRun | None" = None,
             graph_run: "graph_mod.GraphRun | None" = None) -> int:
    console = console or Console(cfg.get("color", True))

    try:
        model = agent.ensure_server(cfg, echo=console.info)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1

    slug = project_slug()
    workspace_root = Path.cwd().resolve()
    state = SessionState(
        cfg=cfg,
        model=model,
        messages=_fresh_messages(cfg, workspace_root),
        session_log=memory.new_session_log(),
        stats=fresh_stats(),
        console=console,
        context_limit=agent.get_context_limit(model, cfg),
        workspace_root=workspace_root,
    )
    confirm_gate = make_confirm_gate(console)

    global SLASH_COMMANDS
    SLASH_COMMANDS = _build_slash_commands(confirm_gate)

    interactive = sys.stdin.isatty()
    prompt_session = None
    ExitREPL = None
    read_line = None
    if interactive:
        if not _require_prompt_toolkit(console):
            return 1
        from .prompt import ExitREPL as _ExitREPL
        from .prompt import build_prompt_session, read_line as _read_line
        ExitREPL = _ExitREPL
        read_line = _read_line
        prompt_session = build_prompt_session(
            commands_provider=lambda: SLASH_COMMANDS,
            state_getter=lambda: state,
            color=bool(cfg.get("color", True)),
        )
        state.prompt_session = prompt_session

    console.banner(slug)

    if graph_run is not None:
        attach_graph_result(state, graph_run)
        if not interactive:
            return 0
    elif until_run is not None:
        attach_until_result(state, until_run)
        if not interactive:
            return 0
    elif skill:
        try:
            prompt_text = agent.load_skill(skill, public_only=True)
        except FileNotFoundError as e:
            console.error(str(e))
            return 1
        text = prompt_text + ("\n\nTask: " + first_task if first_task else "")
        _run_turn(state, text, confirm_gate)
        if not interactive:
            return 0
    elif first_task:
        _run_turn(state, first_task, confirm_gate)
        if not interactive:
            return 0

    _exit_types = (EOFError, KeyboardInterrupt)
    if ExitREPL is not None:
        _exit_types = (ExitREPL, EOFError, KeyboardInterrupt)

    while True:
        try:
            if prompt_session is not None and read_line is not None:
                line = read_line(prompt_session)
            else:
                line = input(console.prompt(state.model)).strip()
        except _exit_types:
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            if not _is_known_slash(line):
                suggestion = _suggest_slash_command(line)
                if suggestion:
                    console.suggest_command(suggestion)
                else:
                    console.unknown_command()
                continue
            if not _dispatch_slash(state, line):
                return 0
            continue
        _run_turn(state, line, confirm_gate)
