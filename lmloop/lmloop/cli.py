"""lmloop CLI.

    lmloop                          interactive REPL
    lmloop "prompt"                 task, then REPL prompt when stdin is a TTY
    lmloop until [--check cmd] [--keep cmd] goal  work until checks or an evaluator pass; REPL on a TTY
    lmloop until                    resume latest open until-run
    lmloop graph <name>             run an authored workflow graph; REPL on a TTY
    lmloop graph                    resume latest open graph-run
    lmloop skills [names]           list skill prompts (names = one per line)
    lmloop skills new <name> [brief]  draft a skill with AI, review, then save
    lmloop skill <name> [task]      start with a skill
    lmloop memory [query]           peek dashboard or search learnings
    lmloop memory list              same dashboard (top learnings + decisions)
    lmloop memory decisions         top active decisions
    lmloop memory dump              readable view of injected memory
    lmloop memory mine [N]          mine last N sessions into learnings (writes)
    lmloop memory kg                knowledge-graph stats (use_graph)
    lmloop memory index             memory index status
    lmloop memory reindex           rebuild the memory index from JSONL
    lmloop memory canvas [query]    text knowledge canvas (use_graph)
    lmloop memory audit [task]      run the learn skill (REPL: side session)
    lmloop memory reconcile         review contradicts clusters (use_graph)
    lmloop retro [N]                deprecated — use memory mine
    lmloop decisions                deprecated — use memory decisions
    lmloop history [n]              list past session transcript files (bare paths)
    lmloop models                   list models on the server
    lmloop config get|set|show      settings
    lmloop completion zsh           print zsh completion script
    lmloop eval [--json | --design] local usage evals and improvement gaps
                                    (--design-doc is an alias for --design)

Project memory commands:

    memory            peek dashboard; list | decisions | dump | kg | mine | reconcile
    memory mine [N]   mine last N sessions into learnings (N > 0, default 3)
    memory kg         knowledge-graph stats (requires use_graph)
    memory index      memory index status
    memory reindex    rebuild the memory index from JSONL
    memory canvas     text knowledge canvas (requires use_graph)
    memory audit      run the learn skill
    memory reconcile  review contradicts clusters (requires use_graph)
    retro [N]         deprecated — use memory mine
    decisions         deprecated — use memory decisions
    history [n]       list past session transcript files (bare paths)
"""

import argparse
import json
import sys
from pathlib import Path

from . import agent, knowledge_graph, loop as loop_mod, memory, server, skills
from . import graph as graph_mod
from .commands import (
    EVAL_FLAGS,
    GRAPH_PROPOSE_VERB,
    HISTORY_DEFAULT_CLI,
    MEMORY_ARG_CHOICES,
    MSG_DEPRECATE_DECISIONS,
    MSG_DEPRECATE_RETRO,
    MSG_RECONCILE_INCOMPLETE,
    cli_subcommand_metas,
    cli_subcommand_names,
    count_usage,
    parse_count_words,
    parse_eval_words,
    parse_flow_words,
    parse_graph_words,
    parse_memory_words,
)
from .config import (
    CONFIG_PATH,
    DEFAULTS,
    cfg_bool,
    cfg_int,
    coerce_config_value,
    load_config,
    save_config,
)
from .repl import make_session_miner, mine_sessions, run_repl
from .ui import Console, ask_until_gate, ask_yes_no, make_confirm_gate
from . import usage

EPILOG = """
project memory commands:
  memory [query]     peek dashboard or search learnings
  memory list        same dashboard (HUD + top learnings/decisions)
  memory decisions   top active decisions
  memory dump        readable view of injected memory
  memory mine [N]    mine last N sessions (N > 0, default 3)
  memory kg          knowledge-graph stats (requires use_graph)
  memory index       memory index status; reindex rebuilds it
  memory canvas      text knowledge canvas (requires use_graph)
  memory audit       run the learn skill
  memory reconcile   review contradicts clusters (requires use_graph)
  retro [N]          deprecated — use memory mine
  decisions          deprecated — use memory decisions
  history [n]        list past session transcript files (bare paths; n > 0, default 15)

examples:
  lmloop
  lmloop "why does setup.sh fail?"
  lmloop until --check 'pytest -q' make tests pass
  lmloop until
  lmloop graph company
  lmloop graph
  lmloop skills
  lmloop skills new deploy "roll out staging safely"
  lmloop skill investigate "vim plug install hangs"
  lmloop skill review
  lmloop memory mine 3
  lmloop memory kg
  lmloop eval --json
"""


_MINE_DETAIL_CLI = "  N is a positive session count (default 3)"


def _require_model(cfg: dict, console: Console) -> "str | None":
    return server.require_model(cfg, echo=console.info, on_error=console.error)


def _cli_display(cfg: dict, console: Console, model: str):
    return console.act_display(
        context_limit=server.get_context_limit(model, cfg),
        context_reserve=cfg_int(cfg, "context_reserve"),
        workspace_root=Path.cwd().resolve(),
    )


def _reject_memory(console: Console, request) -> int:
    console.error(request.error)
    if request.detail:
        console.info(request.detail)
    return 1


def cmd_memory_mine(cfg: dict, count: int, console: Console) -> int:
    sessions = memory.list_sessions(limit=count)
    if not sessions:
        console.info("no sessions recorded yet")
        return 0
    model = _require_model(cfg, console)
    if model is None:
        return 1
    try:
        return mine_sessions(cfg, model, sessions, console, make_confirm_gate(console))
    except KeyboardInterrupt:
        console.hint("\n[interrupted]")
        return 1


def cmd_retro(cfg: dict, count: int, console: Console) -> int:
    console.hint(MSG_DEPRECATE_RETRO)
    console.hint("[memory mine]")
    return cmd_memory_mine(cfg, count, console)


def cmd_skills(console: Console, names_only: bool = False, *, repl: bool = False) -> int:
    names = skills.list_skills()
    if names_only:
        for name in names:
            print(name)
        return 0
    if not names:
        console.info("(no skills yet)")
        return 0
    for name in names:
        blurb = skills.skill_blurb(name)
        line = f"  /{name}"
        if blurb:
            line += f"  — {blurb}"
        path = skills.skill_path(name)
        if path and path.parent == skills.USER_SKILLS_DIR:
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
    err = skills.validate_skill_name(name)
    if err:
        console.error(err)
        return 1
    existing = skills.skill_path(name)
    if existing is not None:
        console.warn(f"skill '{name}' already exists at {existing}")
        if not ask_yes_no("  overwrite? [y/N] "):
            console.info("cancelled")
            return 0

    model = _require_model(cfg, console)
    if model is None:
        return 1

    console.info(f"drafting skill '{name}'…")
    try:
        draft = agent.generate_skill_draft(cfg, model, name, brief)
    except server.ServerError as e:
        console.error(server.server_error_text(e))
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
    console.warn(f"Save to {skills.USER_SKILLS_DIR / (name + '.md')}?")
    if not ask_yes_no("  save skill? [y/N] "):
        console.info("cancelled — draft not saved")
        return 0

    try:
        path = skills.save_user_skill(name, draft)
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
            compadd - __MEMORY_VERBS__
          fi
          ;;
        retro|until|decisions|history|models)
          ;;
        graph)
          (( CURRENT == 2 )) && compadd - __GRAPH_PROPOSE__
          ;;
        eval)
          compadd - __EVAL_FLAGS__
          ;;
        flow)
          compadd - --json
          ;;
      esac
      ;;
  esac
}

compdef _lmloop lmloop
""".replace("__CONFIG_KEYS__", keys).replace("__SUBS__", subs).replace(
        "__MEMORY_VERBS__", " ".join(MEMORY_ARG_CHOICES)
    ).replace("__GRAPH_PROPOSE__", GRAPH_PROPOSE_VERB).replace(
        "__EVAL_FLAGS__", " ".join(EVAL_FLAGS)
    )
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
    request = parse_memory_words(
        words,
        mine_default=3,
        mine_usage="usage: lmloop memory mine [N]",
        mine_detail=_MINE_DETAIL_CLI,
    )
    if request.error:
        return _reject_memory(console, request)
    if request.hint:
        console.hint(request.hint)
    if request.verb == "mine":
        return cmd_memory_mine(cfg, request.mine_count or 3, console)
    if request.verb == "audit":
        console.hint("[memory audit · learn skill]")
        return cmd_skill_cli(cfg, ["learn", *request.rest], console)
    if request.verb == "reconcile":
        return _reconcile_cli(cfg, console)
    memory.render_memory_view(cfg, request, console)
    return 0


def _reconcile_cli(cfg: dict, console: Console) -> int:
    plan = knowledge_graph.prepare_reconcile(cfg)
    if plan.notice:
        console.info(plan.notice)
        return 0
    if plan.error:
        console.error(plan.error)
        return 1
    model = _require_model(cfg, console)
    if model is None:
        return 1
    result = loop_mod.isolated_act(
        cfg, model, plan.prompt,
        confirm_gate=make_confirm_gate(console),
        **_cli_display(cfg, console, model).for_isolated(),
        log_label="/memory reconcile",
    )
    if result is None:
        console.error(MSG_RECONCILE_INCOMPLETE)
        return 1
    return 0


def cmd_decisions(cfg: dict, words: list, console: Console) -> int:
    console.hint(MSG_DEPRECATE_DECISIONS)
    console.write_lines(memory.decision_list_lines(limit=30))
    return 0


def cmd_history(cfg: dict, words: list, console: Console) -> int:
    request = parse_count_words(
        words,
        default=HISTORY_DEFAULT_CLI,
        usage=count_usage("lmloop history"),
    )
    if request.error:
        console.error(request.error)
        return 1
    for path in memory.list_sessions(limit=request.count):
        console.info(path)
    return 0


def cmd_models(cfg: dict, words: list, console: Console) -> int:
    models = server.list_models(cfg["base_url"], cfg=cfg)
    console.info("\n".join(models) if models else f"(no server at {cfg['base_url']} or nothing loaded)")
    return 0


def cmd_until_cli(cfg: dict, words: list, console: Console) -> int:
    if not words:
        run = loop_mod.latest_open_until_run()
        if run is None:
            console.error("usage: lmloop until [--check <cmd>] [--keep <cmd>] <goal>")
            console.info("  no open until-run to resume")
            return 1
        return _cli_run_until(cfg, console, run)
    parsed = loop_mod.parse_until_args(words)
    if parsed.err:
        console.error(parsed.err)
        return 1
    model = _require_model(cfg, console)
    if model is None:
        return 1
    hint = loop_mod.superseded_until_hint()
    run = loop_mod.UntilRun.create(
        parsed.goal, checks=parsed.checks, keeps=parsed.keeps,
    )
    console.hint(f"[until · {parsed.goal}]")
    if hint:
        console.hint(hint)
    return _cli_run_until(cfg, console, run, model=model)


def _cli_run_until(cfg: dict, console: Console, run: loop_mod.UntilRun,
                   model: "str | None" = None) -> int:
    if model is None:
        model = _require_model(cfg, console)
        if model is None:
            return 1
    confirm_gate = make_confirm_gate(console)
    loop_mod.run_until(
        cfg, model, run=run,
        confirm_gate=confirm_gate,
        **_cli_display(cfg, console, model).for_isolated(),
        ask_gate=ask_until_gate,
        mine=make_session_miner(cfg, model, console, confirm_gate)
        if cfg_bool(cfg, "until_mine") else None,
    )
    loaded = loop_mod.UntilRun.load(run.path)
    if sys.stdin.isatty():
        return run_repl(cfg, console=console, until_run=loaded)
    if loaded.is_paused():
        console.hint("paused — run `lmloop until` with no goal to resume")
    return 0


def cmd_flow_cli(cfg: dict, words: list, console: Console) -> int:
    from . import workflow

    request = parse_flow_words(words, invocation="lmloop flow")
    if request.error:
        console.error(request.error)
        return 1
    stats = workflow.collect_flow_stats()
    console.info(workflow.render_flow(stats, cfg, request))
    return 0


def offer_proposed_graph(cfg: dict, model: str, name: str, console: Console) -> int:
    """Draft a graph, show the diff, and save only on an explicit ``y``."""
    try:
        draft = graph_mod.propose_graph_draft(
            cfg, model, name, echo_status=console.hint,
        )
    except graph_mod.GraphError as e:
        console.error(str(e))
        return 1
    if not draft:
        return 1
    console.info(graph_mod.diff_proposed_graph(name, draft))
    console.info("")
    console.info("--- proposed graph ---")
    console.info(draft)
    if not sys.stdin.isatty():
        console.hint("non-interactive — not saved")
        return 0
    if not ask_yes_no(f"Save to {graph_mod.USER_GRAPHS_DIR}/? [y/N] "):
        console.hint("not saved")
        return 0
    try:
        path = graph_mod.save_proposed_graph(name, draft)
    except graph_mod.GraphError as e:
        console.error(str(e))
        return 1
    console.info(f"saved {path}")
    return 0


def cmd_graph_cli(cfg: dict, words: list, console: Console) -> int:
    request = parse_graph_words(
        words,
        run_usage="usage: lmloop graph <name>",
        propose_usage="usage: lmloop graph propose <name>",
    )
    if request.error:
        console.error(request.error)
        return 1
    if request.action == "propose":
        model = _require_model(cfg, console)
        if model is None:
            return 1
        return offer_proposed_graph(cfg, model, request.name, console)
    if request.action == "resume":
        run = graph_mod.latest_open_graph_run()
        if run is None:
            console.error("usage: lmloop graph <name>")
            console.info("  no open graph-run to resume")
            known = ", ".join(graph_mod.list_graphs()) or "(none)"
            console.info(f"  graphs: {known}")
            return 1
        try:
            defn = graph_mod.load_graph(run.name)
        except graph_mod.GraphError as e:
            console.error(str(e))
            return 1
        return _cli_run_graph(cfg, console, run, defn)
    name = request.name
    try:
        defn = graph_mod.load_graph(name)
    except graph_mod.GraphError as e:
        console.error(str(e))
        return 1
    model = _require_model(cfg, console)
    if model is None:
        return 1
    hint = graph_mod.superseded_graph_hint()
    run = graph_mod.GraphRun.create(name)
    console.hint(f"[graph · {name}]")
    if hint:
        console.hint(hint)
    return _cli_run_graph(cfg, console, run, defn, model=model)


def _cli_run_graph(cfg: dict, console: Console, run: graph_mod.GraphRun,
                   defn: graph_mod.GraphDef, model: "str | None" = None) -> int:
    if model is None:
        model = _require_model(cfg, console)
        if model is None:
            return 1
    confirm_gate = make_confirm_gate(console)
    graph_mod.run_graph(
        cfg, model, run=run, defn=defn,
        confirm_gate=confirm_gate,
        **_cli_display(cfg, console, model).for_isolated(),
        ask_gate=ask_until_gate,
        mine=make_session_miner(cfg, model, console, confirm_gate)
        if cfg_bool(cfg, "graph_mine") else None,
    )
    loaded = graph_mod.GraphRun.load(run.path)
    if sys.stdin.isatty():
        return run_repl(cfg, console=console, graph_run=loaded)
    if loaded.is_paused():
        console.hint("paused — run `lmloop graph` with no name to resume")
    return 0


def cmd_retro_cli(cfg: dict, words: list, console: Console) -> int:
    request = parse_memory_words(
        ["mine", *words],
        mine_default=3,
        mine_usage="usage: lmloop retro [N]",
        mine_detail=_MINE_DETAIL_CLI,
    )
    if request.error:
        console.hint(MSG_DEPRECATE_RETRO)
        return _reject_memory(console, request)
    return cmd_retro(cfg, request.mine_count or 3, console)


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
        console.info(f"  skills: {', '.join(skills.list_skills()) or '(none)'}")
        return 1
    name = words[0]
    try:
        skills.load_skill(name, public_only=True)
    except FileNotFoundError as e:
        console.error(str(e))
        return 1
    task = " ".join(words[1:]) or None
    return run_repl(cfg, console=console, skill=name, first_task=task)


def cmd_eval_cli(cfg: dict, words: list, console: Console) -> int:
    from . import evals

    request = parse_eval_words(words, invocation="lmloop eval")
    if request.error:
        console.error(request.error)
        return 1
    from .workflow import collect_flow_stats
    stats = evals.load_stats()
    gaps = evals.find_gaps(stats)
    flow = collect_flow_stats()
    payload = evals.report_dict(stats, gaps)
    payload["flow"] = flow.to_dict()
    evals.write_last_report(payload)
    if request.mode == "json":
        console.info(json.dumps(payload, indent=2))
        return 0
    if request.mode == "design":
        console.info(evals.design_doc_skeleton(gaps))
        return 0
    console.info(evals.format_report(stats, gaps, flow=flow, cfg=cfg))
    return 0


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
        "graph": cmd_graph_cli,
        "flow": cmd_flow_cli,
        "decisions": cmd_decisions,
        "history": cmd_history,
        "models": cmd_models,
        "retro": cmd_retro_cli,
        "skills": cmd_skills_cli,
        "skill": cmd_skill_cli,
        "completion": cmd_completion_cli,
        "eval": cmd_eval_cli,
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
    console = Console(cfg_bool(cfg, "color"))

    words = ([args.cmd] if args.cmd else []) + list(args.args)
    sub = words[0] if words else ""
    handlers = cli_handlers()
    if sub in handlers:
        usage.record("cli.command", command=sub)
        return handlers[sub](cfg, words[1:], console)
    if sub in cli_subcommand_names():
        console.error(f"internal error: no handler for '{sub}'")
        return 1

    task = " ".join(words) if words else None
    usage.record("cli.repl", has_task=bool(task))
    return run_repl(cfg, console=console, skill=None, first_task=task)


if __name__ == "__main__":
    raise SystemExit(main())
