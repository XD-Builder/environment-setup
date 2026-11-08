# Structural decomposition

**Status:** shipped  
**Date:** 2026-11-05  
**Plan:** executed and removed; the resulting layout is the ARCHITECTURE component map and the DEVELOPMENT “Where code goes” table.

lmloop’s loop, CLI, and company package have grown past the ownership rules in [DEVELOPMENT.md](../../DEVELOPMENT.md). This change moves code onto modules that already own the concern, and fixes a few correctness bugs found while checking that move. It does not add a framework, a dependency, or a sixth module in a full category.

## Intent

Keep `agent.act` and `entry.cli.main` as the facades callers and tests already patch. Put display wiring, continue-intent policy, zsh completion, and sandbox CLI orchestration in the modules that own those jobs. Make JSONL appends and tool waves safe when a second writer or a second `build_tools` runs during the first.

## What stays

Control messages are already typed. `core/status.py` owns `nudge_message`, `answer_message`, and `is_control_message` through the `_lmloop` key. A second enum would be another registry.

`ActDisplay` in `ui/console.py` already carries echo and context fields. `act()` keeps its keyword facade. A new `ActTurn` in `agent.py` holds only the knobs `ActDisplay` does not: confirm gate, stats, session log, echo delta, thinking callback, readonly, no tools, max rounds, extra readable paths, and tool allow-list.

Handlers stay in `cli.py` and `repl.py`. `CommandMeta` stays names and metadata. `cli_handlers()` must match `cli_subcommand_names()`; it does not gain callables on the command table.

`capabilities/`, `run/`, `memory/`, and `harness/` are at five modules. `tools.py`, `until_runner.py`, `repl.py`, and `memory/__init__.py` are not split.

## Chosen layout

| Concern | Home |
|---|---|
| Gather/answer loop, skill draft, `ActTurn` | `agent.py` |
| One-round printer, spinner, thinking | `model/display.py` (`RoundDisplay`) |
| Intent prefixes, grounding, nudge/halt | `core/continue_policy.py` |
| Zsh completion script | `entry/completion.py` |
| Sandbox subcommand and `--docker` startup | `entry/sandbox_cli.py` |
| Task packet and result envelope | `company/manifest.py` |
| One JSONL line | `memory.append_jsonl` with `fcntl.flock` |
| Tool defs and output limit for a wave | the `ToolImpls` object returned by `build_tools` |

`entry/` and `core/` are full after this (five counted modules each). `model/` stays at four because `RoundDisplay` folds into `display.py`.

## Approaches dropped

A new `agent/` package and handler packages under `entry/` match a scratch diagram and break the five-file root rule and the “handlers stay in cli/repl” rule.

Comment-only section banners do not change ownership. They are not part of this work.

`cli.py` cannot drop under about 600 lines without moving handlers. That target is dropped. Sandbox and completion leave `cli.py`; the handlers stay.

## Non-goals

MCP and Claude Code parity. Embeddings. Parallel agents. A control-message enum. An `ActContext` beside `ActDisplay`. Splitting `tools.py`, `until_runner.py`, `repl.py`, `memory/__init__.py`, or `verify/checks.py`.

## Constraints

Python 3.14 stdlib only. Patch display names on `lmloop.model.display` once `RoundDisplay` lives there. Keep `agent._chat` and `agent._chat_stream` so existing patches of those names still hit the lookup site. No user-facing README change: completion text, sandbox verbs, and `act()` keywords stay the same.
