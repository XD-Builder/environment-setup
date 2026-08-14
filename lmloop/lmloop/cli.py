"""lmloop CLI.

    lmloop                          interactive REPL
    lmloop "prompt"                 task, then REPL prompt when stdin is a TTY
    lmloop until [--check cmd] goal work until a check or evaluator passes; REPL on a TTY
    lmloop skills [names]           list skill prompts (names = one per line)
    lmloop skills new <name> [brief]  draft a skill with AI, review, then save
    lmloop skill <name> [task]      start with a skill
    lmloop memory [query]           peek curated learnings (read-only)
    lmloop memory mine [N]          mine last N sessions into learnings (writes)
    lmloop decisions                peek durable project decisions (read-only)
    lmloop history                  list past session transcript files
    lmloop models                   list models on the server
    lmloop config get|set|show      settings
    lmloop completion zsh           print zsh completion script

Project memory commands:

    memory            peek curated learnings (read-only)
    memory mine [N]   mine last N sessions into learnings (writes memory)
    decisions         peek durable project decisions (read-only)
    history           list past session transcript files
"""

import argparse
import sys
from pathlib import Path

from . import agent, loop as loop_mod, memory
from .commands import cli_subcommand_metas, cli_subcommand_names
from .config import CONFIG_PATH, DEFAULTS, coerce_config_value, load_config, save_config
from .repl import mine_sessions, run_repl
from .ui import Console, ask_until_gate, ask_yes_no, make_confirm_gate

EPILOG = """
project memory commands:
  memory [query]   peek curated learnings (read-only)
  memory mine [N]  mine last N sessions into learnings (writes memory)
  decisions        peek durable project decisions (read-only)
  history          list past session transcript files

examples:
  lmloop
  lmloop "why does setup.sh fail?"
  lmloop until --check 'pytest -q' make tests pass
  lmloop skills
  lmloop skills new deploy "roll out staging safely"
  lmloop skill investigate "vim plug install hangs"
  lmloop skill review
  lmloop memory mine 3
"""


def cmd_memory_mine(cfg: dict, count: int, console: Console) -> int:
    sessions = memory.list_sessions(limit=count)
    if not sessions:
        console.info("no sessions recorded yet")
        return 0
    try:
        model = agent.ensure_server(cfg, echo=console.info)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1
    try:
        return mine_sessions(cfg, model, sessions, console, make_confirm_gate(console))
    except KeyboardInterrupt:
        console.hint("\n[interrupted]")
        return 1


def cmd_retro(cfg: dict, count: int, console: Console) -> int:
    console.hint("[memory mine]")
    return cmd_memory_mine(cfg, count, console)


def cmd_skills(console: Console, names_only: bool = False, *, repl: bool = False) -> int:
    names = agent.list_skills()
    if names_only:
        for name in names:
            print(name)
        return 0
    if not names:
        console.info("(no skills yet)")
        return 0
    for name in names:
        blurb = agent.skill_blurb(name)
        line = f"  /{name}"
        if blurb:
            line += f"  — {blurb}"
        path = agent.skill_path(name)
        if path and path.parent == agent.USER_SKILLS_DIR:
            line += "  (user)"
        console.info(line)
    if repl:
        console.hint("  create: /skills new <name> [brief]")
    else:
        console.hint("  run: lmloop skill <name> [task]   or in REPL: /name")
        console.hint("  create: lmloop skills new <name> [brief]")
    return 0


def cmd_skills_new(cfg: dict, name: str, brief: str, console: Console) -> int:
    """Generate a skill draft, show it, and save to ~/.lmloop/skills/ on confirm."""
    err = agent.validate_skill_name(name)
    if err:
        console.error(err)
        return 1
    existing = agent.skill_path(name)
    if existing is not None:
        console.warn(f"skill '{name}' already exists at {existing}")
        if not ask_yes_no("  overwrite? [y/N] "):
            console.info("cancelled")
            return 0

    try:
        model = agent.ensure_server(cfg, echo=console.info)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1

    console.info(f"drafting skill '{name}'…")
    try:
        draft = agent.generate_skill_draft(cfg, model, name, brief)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        console.hint("\n[interrupted — draft not saved]")
        return 1

    if not draft.strip():
        console.error("model returned an empty draft")
        return 1

    console.info("")
    console.info("── draft ─────────────────────────────────────────────")
    console.print_markdown(draft)
    console.info("──────────────────────────────────────────────────────")
    console.warn(f"Save to {agent.USER_SKILLS_DIR / (name + '.md')}?")
    if not ask_yes_no("  save skill? [y/N] "):
        console.info("cancelled — draft not saved")
        return 0

    try:
        path = agent.save_user_skill(name, draft)
    except ValueError as e:
        console.error(str(e))
        return 1
    console.info(f"saved {path}")
    console.hint(f"  try: lmloop skill {name}   or in REPL: /{name}")
    return 0


def cmd_completion(shell: str, console: Console) -> int:
    if shell != "zsh":
        console.error(f"unsupported shell '{shell}' (only zsh)")
        return 1
    keys = " ".join(DEFAULTS)
    subs = "\n".join(
        f"    '{c.name}:{c.desc.replace(chr(39), '')}'"
        for c in cli_subcommand_metas()
    )
    # Self-contained zsh completion; skill names refreshed via `lmloop skills --names`.
    script = r"""#compdef lmloop

_lmloop() {
  local -a subs
  subs=(
__SUBS__
  )

  local curcontext="$curcontext" state
  _arguments -C \
    '--model[override model for this run]:model:' \
    '--help[show help]' \
    '1: :->cmd' \
    '*:: :->args' && return

  case $state in
    cmd)
      _describe -t commands 'lmloop command' subs
      ;;
    args)
      case $words[1] in
        skill)
          if (( CURRENT == 2 )); then
            local -a skills
            skills=(${(f)"$(lmloop skills names 2>/dev/null)"})
            _describe -t skills 'skill' skills
          fi
          ;;
        skills)
          if (( CURRENT == 2 )); then
            compadd - names new
          fi
          ;;
        config)
          if (( CURRENT == 2 )); then
            compadd - show get set
          elif (( CURRENT == 3 )) && [[ $words[2] == get || $words[2] == set ]]; then
            compadd - __CONFIG_KEYS__
          fi
          ;;
        completion)
          # Only offer the supported shell name — avoid _values, which can
          # surface unrelated zsh completion keywords in the menu.
          (( CURRENT == 2 )) && compadd - zsh
          ;;
        memory)
          if (( CURRENT == 2 )); then
            compadd - mine
          fi
          ;;
        retro|until|decisions|history|models)
          ;;
      esac
      ;;
  esac
}

compdef _lmloop lmloop
""".replace("__CONFIG_KEYS__", keys).replace("__SUBS__", subs)
    sys.stdout.write(script)
    return 0


def cmd_config(cfg: dict, words: list, console: Console) -> int:
    if len(words) >= 2 and words[0] == "set":
        key, value = words[1], " ".join(words[2:])
        if key not in DEFAULTS:
            console.error(f"unknown key '{key}'. Keys: {', '.join(DEFAULTS)}")
            return 1
        default = DEFAULTS[key]
        coerced = coerce_config_value(key, value)
        if coerced is None:
            console.error(f"invalid value for {key}: expected {type(default).__name__}")
            return 1
        cfg[key] = coerced
        save_config(cfg)
        console.info(f"{key} = {cfg[key]}")
        return 0
    if len(words) >= 2 and words[0] == "get":
        key = words[1]
        if key not in DEFAULTS:
            console.error(f"unknown key '{key}'. Keys: {', '.join(DEFAULTS)}")
            return 1
        console.info(str(cfg.get(key, DEFAULTS[key])))
        return 0
    console.info(f"# {CONFIG_PATH}")
    for k in DEFAULTS:
        console.info(f"{k} = {cfg[k]}")
    return 0


def cmd_memory(cfg: dict, words: list, console: Console) -> int:
    if words and words[0] == "mine":
        rest = words[1:]
        count = int(rest[0]) if rest and rest[0].isdigit() else 3
        return cmd_memory_mine(cfg, count, console)
    q = " ".join(words)
    rows = memory.get_learnings(query=q, limit=30)
    for r in rows:
        console.info(
            f"- [{r['key']}] ({r['type']}, {r['confidence']}/10, {r['ts'][:10]}) {r['insight']}"
        )
    if not rows:
        console.info("(no learnings yet — run tasks, then `lmloop memory mine`)")
    return 0


def cmd_decisions(cfg: dict, words: list, console: Console) -> int:
    rows = memory.get_decisions(limit=30)
    for d in rows:
        line = f"- [{d['id']}] {d['date'][:10]} {d['decision']}"
        if d.get("rationale"):
            line += f"  (why: {d['rationale']})"
        console.info(line)
    if not rows:
        console.info("(no decisions logged yet)")
    return 0


def cmd_history(cfg: dict, words: list, console: Console) -> int:
    for p in memory.list_sessions(limit=15):
        console.info(p)
    return 0


def cmd_models(cfg: dict, words: list, console: Console) -> int:
    models = agent.list_models(cfg["base_url"])
    console.info("\n".join(models) if models else f"(no server at {cfg['base_url']} or nothing loaded)")
    return 0


def cmd_until_cli(cfg: dict, words: list, console: Console) -> int:
    if not words:
        run = loop_mod.latest_open_until_run()
        if run is None:
            console.error("usage: lmloop until [--check <cmd>] <goal>")
            console.info("  no paused until-run to resume")
            return 1
        return _cli_run_until(cfg, console, run)
    goal, check_cmd, err = loop_mod.parse_until_args(words)
    if err:
        console.error(err)
        return 1
    try:
        model = agent.ensure_server(cfg, echo=console.info)
    except agent.ServerError as e:
        console.error(f"error: {e}")
        return 1
    hint = loop_mod.superseded_until_hint()
    run = loop_mod.UntilRun.create(goal, check_cmd=check_cmd)
    console.hint(f"[until · {goal}]")
    if hint:
        console.hint(hint)
    return _cli_run_until(cfg, console, run, model=model)


def _cli_run_until(cfg: dict, console: Console, run: loop_mod.UntilRun,
                   model: "str | None" = None) -> int:
    if model is None:
        try:
            model = agent.ensure_server(cfg, echo=console.info)
        except agent.ServerError as e:
            console.error(f"error: {e}")
            return 1
    confirm_gate = make_confirm_gate(console)

    def mine(paths):
        if not paths:
            return
        mine_sessions(cfg, model, paths, console, confirm_gate)

    loop_mod.run_until(
        cfg, model, run=run,
        confirm_gate=confirm_gate,
        echo=lambda text: console.print_markdown(text) if text else None,
        echo_status=console.hint,
        echo_error=console.error,
        echo_tool=console.tool_call,
        echo_round=console.round_usage,
        context_limit=agent.get_context_limit(model, cfg),
        context_reserve=int(cfg.get("context_reserve") or 2048),
        workspace_root=Path.cwd().resolve(),
        ask_gate=ask_until_gate,
        mine=mine if cfg.get("until_mine", True) else None,
    )
    loaded = loop_mod.UntilRun.load(run.path)
    if sys.stdin.isatty():
        return run_repl(cfg, console=console, until_run=loaded)
    if loaded.is_paused():
        console.hint("paused — run `lmloop until` with no goal to resume")
    return 0


def cmd_retro_cli(cfg: dict, words: list, console: Console) -> int:
    count = int(words[0]) if words and words[0].isdigit() else 3
    return cmd_retro(cfg, count, console)


def cmd_skills_cli(cfg: dict, words: list, console: Console) -> int:
    if words and words[0] == "new":
        if len(words) < 2:
            console.error("usage: lmloop skills new <name> [brief…]")
            return 1
        return cmd_skills_new(cfg, words[1], " ".join(words[2:]), console)
    names_only = bool(words) and words[0] in ("names", "--names")
    return cmd_skills(console, names_only=names_only)


def cmd_skill_cli(cfg: dict, words: list, console: Console) -> int:
    if not words:
        console.error("usage: lmloop skill <name> [task]")
        console.info(f"  skills: {', '.join(agent.list_skills()) or '(none)'}")
        return 1
    name = words[0]
    try:
        agent.load_skill(name, public_only=True)
    except FileNotFoundError as e:
        console.error(str(e))
        return 1
    task = " ".join(words[1:]) or None
    return run_repl(cfg, console=console, skill=name, first_task=task)


def cmd_completion_cli(cfg: dict, words: list, console: Console) -> int:
    shell = words[0] if words else ""
    if not shell:
        console.error("usage: lmloop completion zsh")
        return 1
    return cmd_completion(shell, console)


def cli_handlers() -> dict:
    """Stem -> handler(cfg, remaining_words, console). Generated from CommandMeta."""
    return {
        "config": cmd_config,
        "memory": cmd_memory,
        "until": cmd_until_cli,
        "decisions": cmd_decisions,
        "history": cmd_history,
        "models": cmd_models,
        "retro": cmd_retro_cli,
        "skills": cmd_skills_cli,
        "skill": cmd_skill_cli,
        "completion": cmd_completion_cli,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmloop",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model", help="override model for this run")
    parser.add_argument("cmd", nargs="?", default="", help="task / subcommand")
    # REMAINDER keeps flags like until --check from being eaten as argparse options.
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.model:
        cfg["model"] = args.model
    console = Console(cfg.get("color", True))

    words = ([args.cmd] if args.cmd else []) + list(args.args)
    sub = words[0] if words else ""
    handlers = cli_handlers()
    if sub in handlers:
        return handlers[sub](cfg, words[1:], console)
    if sub in cli_subcommand_names():
        console.error(f"internal error: no handler for '{sub}'")
        return 1

    task = " ".join(words) if words else None
    return run_repl(cfg, console=console, skill=None, first_task=task)


if __name__ == "__main__":
    raise SystemExit(main())
