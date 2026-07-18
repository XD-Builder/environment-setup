"""lmloop CLI.

    lmloop                          interactive REPL
    lmloop "prompt"                 one-shot task
    lmloop --skill investigate "…"  run a skill prompt
    lmloop retro                    mine recent sessions into learnings
    lmloop memory [query]           show learnings
    lmloop decisions                show active decisions
    lmloop history                  list recent sessions
    lmloop models                   list models on the server
    lmloop config get|set|show      settings
"""

import argparse

from . import agent, memory, tools
from .config import CONFIG_PATH, DEFAULTS, load_config, save_config
from .repl import run_repl
from .ui import Console


def _fresh_messages(cfg: dict) -> list:
    return [{"role": "system", "content": agent.system_prompt(cfg)}]


def _session_transcript_for_retro(paths) -> str:
    parts = []
    for p in paths:
        parts.append(f"### Session {p.stem}\n{memory.read_session(p)}")
    return "\n\n".join(parts)


def _make_confirm_gate(console: Console):
    def confirm_gate(command: str) -> bool:
        label = "potentially destructive"
        if tools.needs_shell(command) and not tools.is_destructive(command):
            label = "shell-syntax (pipes/redirections)"
        console.warn(f"\n⚠ {label} command requested:\n    {command}")
        try:
            return input(console.confirm_prompt()).strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            return False
    return confirm_gate


def cmd_retro(cfg: dict, count: int, console: Console) -> int:
    sessions = memory.list_sessions(limit=count)
    if not sessions:
        console.info("no sessions recorded yet")
        return 0
    try:
        model = agent.ensure_server(cfg, echo=console.info)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1
    messages = _fresh_messages(cfg)
    session_log = memory.new_session_log()
    task = agent.load_prompt("retro") + "\n\n" + _session_transcript_for_retro(sessions)
    memory.log_event(session_log, "user", f"/retro over {len(sessions)} sessions")
    messages.append({"role": "user", "content": task})
    agent.act(
        cfg, model, messages, session_log=session_log,
        confirm_gate=_make_confirm_gate(console),
        echo_tool=console.tool_call,
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lmloop", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skill", help="start with a skill prompt (investigate, review, retro)")
    parser.add_argument("--model", help="override model for this run")
    parser.add_argument("words", nargs="*", help="task / subcommand")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.model:
        cfg["model"] = args.model
    console = Console(cfg.get("color", True))

    words = args.words
    sub = words[0] if words else ""

    if sub == "config":
        if len(words) >= 3 and words[1] == "set":
            key, value = words[2], " ".join(words[3:])
            if key not in DEFAULTS:
                console.error(f"unknown key '{key}'. Keys: {', '.join(DEFAULTS)}")
                return 1
            default = DEFAULTS[key]
            if isinstance(default, bool):
                cfg[key] = value.lower() in ("1", "true", "yes", "y")
            elif isinstance(default, int):
                cfg[key] = int(value)
            elif isinstance(default, float):
                cfg[key] = float(value)
            else:
                cfg[key] = value
            save_config(cfg)
            console.info(f"{key} = {cfg[key]}")
            return 0
        if len(words) >= 3 and words[1] == "get":
            console.info(cfg.get(words[2], ""))
            return 0
        console.info(f"# {CONFIG_PATH}")
        for k in DEFAULTS:
            console.info(f"{k} = {cfg[k]}")
        return 0

    if sub == "memory":
        q = " ".join(words[1:])
        rows = memory.get_learnings(query=q, limit=30)
        for r in rows:
            console.info(
                f"- [{r['key']}] ({r['type']}, {r['confidence']}/10, {r['ts'][:10]}) {r['insight']}"
            )
        if not rows:
            console.info("(no learnings yet — run tasks, then `lmloop retro`)")
        return 0

    if sub == "decisions":
        rows = memory.get_decisions(limit=30)
        for d in rows:
            line = f"- [{d['id']}] {d['date'][:10]} {d['decision']}"
            if d.get("rationale"):
                line += f"  (why: {d['rationale']})"
            console.info(line)
        if not rows:
            console.info("(no decisions logged yet)")
        return 0

    if sub == "history":
        for p in memory.list_sessions(limit=15):
            console.info(p)
        return 0

    if sub == "models":
        models = agent.list_models(cfg["base_url"])
        console.info("\n".join(models) if models else f"(no server at {cfg['base_url']} or nothing loaded)")
        return 0

    if sub == "retro":
        count = int(words[1]) if len(words) > 1 and words[1].isdigit() else 3
        return cmd_retro(cfg, count, console)

    task = " ".join(words) if words else None
    return run_repl(cfg, console=console, skill=args.skill, first_task=task)


if __name__ == "__main__":
    raise SystemExit(main())
