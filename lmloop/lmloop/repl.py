"""Interactive REPL for lmloop."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import agent, memory, tools
from .config import project_slug
from .files_index import expand_at_refs
from .ui import Console, ask_yes_no, fresh_stats


@dataclass
class SlashCommand:
    name: str
    desc: str
    handler: Callable[["SessionState", str], bool]
    arg_hint: str = ""
    accepts_arg: bool = False
    exits: bool = False


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

    @property
    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m["role"] == "user")

    @property
    def context_reserve(self) -> int:
        return int(self.cfg.get("context_reserve") or 2048)


def _refresh_context_limit(state: SessionState) -> None:
    state.context_limit = agent.get_context_limit(state.model, state.cfg)


def _reset_session(state: SessionState, keep_stats: bool = False) -> None:
    state.messages = _fresh_messages(state.cfg)
    state.session_log = memory.new_session_log()
    if not keep_stats:
        state.stats = fresh_stats()


def _fresh_messages(cfg: dict) -> list:
    return [{"role": "system", "content": agent.system_prompt(cfg)}]


def _make_confirm_gate(console: Console, session_getter=None):
    # session_getter kept for call-site compat; confirms use plain input()
    # so prompt_toolkit does not redraw over the warning mid-turn.
    del session_getter

    def confirm_gate(command: str) -> bool:
        label = "potentially destructive"
        if tools.needs_shell(command) and not tools.is_destructive(command):
            label = "shell-syntax (pipes/redirections)"
        console.warn(f"\n⚠ {label} command requested:\n    {command}")
        return ask_yes_no(console.confirm_prompt(command))
    return confirm_gate


def _echo_assistant(console: Console):
    """Echo callback: render markdown for model replies; plain for status lines."""
    def echo(text: str) -> None:
        if not text:
            return
        if text.startswith("[lmloop]") or text.startswith("[lml]"):
            console.hint(text)
            return
        console.print_markdown(text)
    return echo


def _run_turn(state: SessionState, user_text: str, confirm_gate) -> None:
    # Expand @path refs for every turn (freeform, /skill, /continue, …).
    user_text = expand_at_refs(user_text)
    memory.log_event(state.session_log, "user", user_text)
    state.messages.append({"role": "user", "content": user_text})
    echo_tool = state.console.tool_call
    try:
        agent.act(
            state.cfg, state.model, state.messages,
            session_log=state.session_log,
            confirm_gate=confirm_gate,
            stats=state.stats,
            echo=_echo_assistant(state.console),
            echo_tool=echo_tool,
        )
        footer = state.console.stats_footer(
            state.stats, state.context_limit, state.messages, state.context_reserve,
        )
        if footer:
            print(footer)
    except agent.ServerError as e:
        state.console.error(f"error: {e}")
    except KeyboardInterrupt:
        state.console.hint("\n[interrupted — conversation kept, type to continue]")


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


def _cmd_memory(state: SessionState, arg: str) -> bool:
    for r in memory.get_learnings(query=arg, limit=20):
        state.console.info(
            f"- [{r['key']}] ({r['type']}, {r['confidence']}/10) {r['insight']}"
        )
    return True


def _cmd_save(state: SessionState, arg: str, confirm_gate) -> bool:
    title = arg or "session-checkpoint"
    _run_turn(state,
              "Write a checkpoint of this session for a future session to resume from: "
              "what we're working on, key findings and decisions, and remaining work. "
              "Reply with the checkpoint text only — do not call any tools.",
              confirm_gate)
    body = next(
        (m["content"] for m in reversed(state.messages)
         if m["role"] == "assistant" and m.get("content")),
        "",
    )
    path = memory.save_checkpoint(title, body)
    state.console.info(f"[checkpoint saved: {path}]")
    return True


def _cmd_retro(state: SessionState, _arg: str, confirm_gate) -> bool:
    _run_turn(state,
              agent.load_skill("retro") + "\n\nTranscript of THIS session so far:\n"
              + memory.read_session(state.session_log),
              confirm_gate)
    return True


def _run_named_skill(state: SessionState, name: str, task: str, confirm_gate) -> bool:
    try:
        prompt = agent.load_skill(name, public_only=True)
    except FileNotFoundError as e:
        state.console.error(str(e))
        return True
    _run_turn(state, prompt + ("\n\nTask: " + task if task else ""), confirm_gate)
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
    transcript = memory.read_session(state.session_log, max_chars=12000)
    prompt = agent.load_skill("compact") + "\n\nTranscript:\n" + transcript
    if arg:
        prompt += "\n\nFocus: " + arg
    _run_turn(state, prompt, confirm_gate)
    return True


def _cmd_skills(state: SessionState, arg: str, confirm_gate) -> bool:
    parts = arg.split(None, 2)
    if parts and parts[0] == "new":
        if len(parts) < 2:
            state.console.info("usage: /skills new <name> [brief]")
            return True
        from .cli import cmd_skills_new
        cmd_skills_new(
            state.cfg, parts[1], parts[2] if len(parts) > 2 else "", state.console,
            prompt_session=state.prompt_session,
        )
        # Rebuild slash commands so a newly saved skill gets /name immediately
        global SLASH_COMMANDS
        SLASH_COMMANDS = _build_slash_commands(confirm_gate)
        return True

    names = agent.list_skills()
    for name in names:
        blurb = agent.skill_blurb(name)
        line = f"  /{name}"
        if blurb:
            line += f"  — {blurb}"
        path = agent.skill_path(name)
        if path and path.parent == agent.USER_SKILLS_DIR:
            line += "  (user)"
        state.console.info(line)
    if not names:
        state.console.info("(no skills yet)")
    state.console.hint("  create: /skills new <name> [brief]")
    return True


def _cmd_history(state: SessionState, arg: str) -> bool:
    limit = int(arg) if arg.isdigit() else 10
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
    state.console.hint("  restore with: /restore session <#|stem|latest>")
    return True


def _cmd_checkpoints(state: SessionState, arg: str) -> bool:
    limit = int(arg) if arg.isdigit() else 10
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


def _cmd_restore(state: SessionState, arg: str, confirm_gate) -> bool:
    parts = arg.split()
    if len(parts) < 2:
        state.console.info("usage: /restore checkpoint|session <latest|#|stem> [fresh]")
        state.console.info("  /restore checkpoint latest   inject latest checkpoint")
        state.console.info("  /restore session 2 fresh     new chat + prior transcript")
        return True
    kind, query = parts[0].lower(), parts[1]
    fresh = len(parts) > 2 and parts[2].lower() in ("fresh", "new")
    if kind == "checkpoint":
        path = memory.resolve_checkpoint(query)
        if not path:
            state.console.error("checkpoint not found — try /checkpoints")
            return True
        body = memory.read_checkpoint_body(path)
        msg = f"Resume from checkpoint ({path.stem}):\n\n{body}"
        if fresh:
            _reset_session(state)
            state.console.info(f"[fresh conversation + checkpoint {path.stem}]")
        _run_turn(state, msg, confirm_gate)
        return True
    if kind == "session":
        path = memory.resolve_session(query)
        if not path:
            state.console.error("session not found — try /history")
            return True
        if path.resolve() == state.session_log.resolve() and not fresh:
            state.console.info("[already in this session]")
            return True
        transcript = memory.read_session(path)
        msg = f"Continue from prior session ({path.stem}):\n\n{transcript}"
        if fresh:
            _reset_session(state)
            state.console.info(f"[fresh conversation + session {path.stem}]")
        _run_turn(state, msg, confirm_gate)
        return True
    state.console.error("usage: /restore checkpoint|session <latest|#|stem> [fresh]")
    return True


def _cmd_continue(state: SessionState, arg: str, confirm_gate) -> bool:
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
    commands = [
        SlashCommand("/help", "show available commands", _cmd_help),
        SlashCommand("/stats", "show token usage and session activity", _cmd_stats),
        SlashCommand("/transcript", "view rendered session in less (q to quit)",
                     _cmd_transcript),
        SlashCommand("/skills", "list skills, or: new <name> [brief] to author one",
                     lambda s, a: _cmd_skills(s, a, confirm_gate),
                     "[new <name> brief]", accepts_arg=True),
        SlashCommand("/history", "list recent session logs",
                     lambda s, a: _cmd_history(s, a), "[n]", accepts_arg=True),
        SlashCommand("/checkpoints", "list saved checkpoints",
                     lambda s, a: _cmd_checkpoints(s, a), "[n]", accepts_arg=True),
        SlashCommand("/restore", "inject checkpoint or prior session transcript",
                     lambda s, a: _cmd_restore(s, a, confirm_gate),
                     "checkpoint|session <query> [fresh]", accepts_arg=True),
        SlashCommand("/decisions", "show active project decisions", _cmd_decisions),
        SlashCommand("/context", "show memory injected into the system prompt", _cmd_context),
        SlashCommand("/continue", "resume after max_rounds or an interruption",
                     lambda s, a: _cmd_continue(s, a, confirm_gate), "[message]", accepts_arg=True),
        SlashCommand("/undo", "drop the last user turn from the in-memory thread", _cmd_undo),
        SlashCommand("/compact", "summarize thread for context recovery",
                     lambda s, a: _cmd_compact(s, a, confirm_gate), "[focus]", accepts_arg=True),
        SlashCommand("/new", "reset conversation (memory context re-injected)", _cmd_new),
        SlashCommand("/model", "switch model, or list models with no argument",
                     lambda s, a: _cmd_model(s, a), "<name>", accepts_arg=True),
        SlashCommand("/memory", "show project learnings",
                     lambda s, a: _cmd_memory(s, a), "[query]", accepts_arg=True),
        SlashCommand("/save", "checkpoint session for later restore",
                     lambda s, a: _cmd_save(s, a, confirm_gate), "[title]", accepts_arg=True),
        SlashCommand("/retro", "extract learnings from this session",
                     lambda s, a: _cmd_retro(s, a, confirm_gate)),
        SlashCommand("/skill", "run any skill prompt by name",
                     lambda s, a: _cmd_skill(s, a, confirm_gate), "<name> [task]", accepts_arg=True),
        SlashCommand("/quit", "exit", lambda s, a: False, exits=True),
        SlashCommand("/exit", "exit", lambda s, a: False, exits=True),
        SlashCommand("/q", "exit", lambda s, a: False, exits=True),
    ]
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


def _command_descriptions() -> dict:
    return {c.name: c.desc for c in SLASH_COMMANDS}


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
             skill: "str | None" = None, first_task: "str | None" = None) -> int:
    console = console or Console(cfg.get("color", True))

    try:
        model = agent.ensure_server(cfg, echo=console.info)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1

    slug = project_slug()
    state = SessionState(
        cfg=cfg,
        model=model,
        messages=_fresh_messages(cfg),
        session_log=memory.new_session_log(),
        stats=fresh_stats(),
        console=console,
        context_limit=agent.get_context_limit(model, cfg),
    )
    confirm_gate = _make_confirm_gate(console, lambda: state.prompt_session)

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

    if skill:
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
            if line in ("/quit", "/exit", "/q"):
                return 0
            if not _dispatch_slash(state, line):
                return 0
            continue
        _run_turn(state, line, confirm_gate)
