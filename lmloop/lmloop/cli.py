"""lmloop CLI.

    lmloop                          interactive REPL
    lmloop "prompt"                 one-shot task
    lmloop skills [names]           list skill prompts (names = one per line)
    lmloop skills new <name> [brief]  draft a skill with AI, review, then save
    lmloop skill <name> [task]      start with a skill
    lmloop memory [query]           peek curated learnings (read-only)
    lmloop decisions                peek durable project decisions (read-only)
    lmloop history                  list past session transcript files
    lmloop retro [N]                mine last N sessions into learnings (writes)
    lmloop models                   list models on the server
    lmloop config get|set|show      settings
    lmloop completion zsh           print zsh completion script

Project memory commands:

    memory     peek curated learnings (read-only)
    decisions  peek durable project decisions (read-only)
    history    list past session transcript files
    retro [N]  mine last N sessions into learnings (writes memory)
"""

import argparse
import sys

from . import agent, memory
from .commands import cli_subcommand_names
from .config import CONFIG_PATH, DEFAULTS, load_config, save_config
from .repl import mine_sessions, run_repl
from .ui import Console, ask_yes_no, make_confirm_gate

EPILOG = """
project memory commands:
  memory [query]   peek curated learnings (read-only)
  decisions        peek durable project decisions (read-only)
  history          list past session transcript files
  retro [N]        mine last N sessions into learnings (writes memory)

examples:
  lmloop
  lmloop "why does setup.sh fail?"
  lmloop skills
  lmloop skills new deploy "roll out staging safely"
  lmloop skill investigate "vim plug install hangs"
  lmloop skill review
  lmloop retro 3
"""


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
    return mine_sessions(cfg, model, sessions, console, make_confirm_gate(console))


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
    # Self-contained zsh completion; skill names refreshed via `lmloop skills --names`.
    script = r"""#compdef lmloop

_lmloop() {
  local -a subs
  subs=(
    'skills:list skill prompts'
    'skill:start with a skill prompt'
    'memory:peek curated learnings (read-only)'
    'decisions:peek durable project decisions (read-only)'
    'history:list past session transcript files'
    'retro:mine last N sessions into learnings (writes memory)'
    'models:list models on the server'
    'config:get/set/show settings'
    'completion:print shell completion script'
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
        retro|memory|decisions|history|models)
          ;;
      esac
      ;;
  esac
}

compdef _lmloop lmloop
""".replace("__CONFIG_KEYS__", keys)
    sys.stdout.write(script)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmloop",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model", help="override model for this run")
    parser.add_argument("words", nargs="*", help="task / subcommand")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.model:
        cfg["model"] = args.model
    console = Console(cfg.get("color", True))

    words = list(args.words)
    sub = words[0] if words else ""

    if sub == "config":
        if len(words) >= 3 and words[1] == "set":
            key, value = words[2], " ".join(words[3:])
            if key not in DEFAULTS:
                console.error(f"unknown key '{key}'. Keys: {', '.join(DEFAULTS)}")
                return 1
            default = DEFAULTS[key]
            try:
                if isinstance(default, bool):
                    cfg[key] = value.lower() in ("1", "true", "yes", "y")
                elif isinstance(default, int):
                    cfg[key] = int(value)
                elif isinstance(default, float):
                    cfg[key] = float(value)
                else:
                    cfg[key] = value
            except ValueError:
                console.error(f"invalid value for {key}: expected {type(default).__name__}")
                return 1
            save_config(cfg)
            console.info(f"{key} = {cfg[key]}")
            return 0
        if len(words) >= 3 and words[1] == "get":
            key = words[2]
            if key not in DEFAULTS:
                console.error(f"unknown key '{key}'. Keys: {', '.join(DEFAULTS)}")
                return 1
            console.info(str(cfg.get(key, DEFAULTS[key])))
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

    if sub == "skills":
        rest = words[1:]
        if rest and rest[0] == "new":
            if len(rest) < 2:
                console.error("usage: lmloop skills new <name> [brief…]")
                return 1
            return cmd_skills_new(cfg, rest[1], " ".join(rest[2:]), console)
        names_only = bool(rest) and rest[0] in ("names", "--names")
        return cmd_skills(console, names_only=names_only)

    if sub == "skill":
        if len(words) < 2:
            console.error("usage: lmloop skill <name> [task]")
            console.info(f"  skills: {', '.join(agent.list_skills()) or '(none)'}")
            return 1
        name = words[1]
        try:
            agent.load_skill(name, public_only=True)
        except FileNotFoundError as e:
            console.error(str(e))
            return 1
        task = " ".join(words[2:]) or None
        return run_repl(cfg, console=console, skill=name, first_task=task)

    if sub == "completion":
        shell = words[1] if len(words) > 1 else ""
        if not shell:
            console.error("usage: lmloop completion zsh")
            return 1
        return cmd_completion(shell, console)

    if sub in cli_subcommand_names():
        console.error(f"internal error: no handler for '{sub}'")
        return 1

    # Free-form task: first word is not a reserved subcommand
    task = " ".join(words) if words else None
    return run_repl(cfg, console=console, skill=None, first_task=task)


if __name__ == "__main__":
    raise SystemExit(main())
