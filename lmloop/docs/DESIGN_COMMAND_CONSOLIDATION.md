# Design: Command consolidation (memory and neighbors)

**Status:** partial — Phases A–C shipped (`mine_on_exit`, submit-line mine routes, `/memory audit`). Phase D not merged.
**Date:** 2026-10-07
**Companions:** [ARCHITECTURE.md](ARCHITECTURE.md) · [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) · [DESIGN_LLM_CALLING.md](archived/DESIGN_LLM_CALLING.md)

## Problem

lmloop exposes the same memory lifecycle through many surfaces: slash subverbs
(` /memory mine`, `/decisions`, `/learn`), CLI aliases (`retro`, `decisions`),
agent tools (`remember`, `log_decision`, `recall_memory`), and automatic hooks
(`until_mine`, `graph_mine`). Users must learn peek vs write vs reload and two
different meanings of “graph” (workflow `/graph` vs knowledge `/memory graph`).

The product goal: **a small REPL/CLI vocabulary** while **orchestration and
playbooks** (`retro.md`, `learn.md`, `steer/memory.md`, config flags) carry
behavior that can evolve without new slash commands.

## Principles

1. **Single registry** — names stay in `commands.py`; handlers in `cli.py` /
   `repl.py`; tools in `build_tools()`. Consolidation removes aliases and tiers
   help, it does not fork registries.
2. **No LLM router for slash** — natural-language triggers (Phase C) use keyword
   / `shlex` patterns on submit, not a model call to pick a command.
3. **Tools stay, users don’t memorize them** — `remember` / `log_decision` are
   for the agent during normal chat; steer tells the model when to use them.
4. **Workflow graph ≠ knowledge graph** — user-facing KG peek is `/memory kg`;
   `graph` remains a deprecated alias with a hint.

## Target mental model

| Intent | User surface | Orchestration |
|--------|--------------|---------------|
| What does the project know? | `/memory`, `/context` | Injection + `recall_memory` |
| Remember / decide in chat | Plain language | Tools + `steer/memory.md` |
| Harvest sessions | `/memory mine` (optional) | `until_mine`, `graph_mine`; future hooks |
| Curate / contradictions | `/learn`, `/memory reconcile` | Skills + `use_graph` |

## Phases

### Phase A — Help and docs (shipped)

- **`/help`** lists **core** slash commands only; **`/help all`** lists advanced
  commands too (still omits `hidden` aliases like `/retro`).
- **`/` completion** offers core commands and core `/memory` subverbs only.
- **README** and this doc describe peek vs write; `/context` vs `/memory dump`.
- **Deprecation hints** (no removal yet): `retro`, `lmloop decisions`, `/decisions`,
  `/memory graph` → prefer `memory mine`, `memory decisions`, `memory kg`.

### Phase B — Collapse aliases (shipped)

- **`/memory` and `/memory list`** show a **peek dashboard**: HUD line, top
  learnings, top decisions, hints for `/context` and `/memory dump`.
- **`/decisions`** delegates to `/memory decisions` (same output, deprecation hint).
- **`/memory kg`** is the canonical knowledge-graph stats subverb; **`graph`**
  still works with a deprecation hint.
- **`/retro`** stays **hidden** (handler only); CLI `lmloop retro` retained with hint.
- **Advanced skill shortcuts** (`/learn`, `/retro` if exposed as skill): omitted
  from default `/` completion (`ADVANCED_SKILL_SLASH` in `commands.py`).

### Phase C — Orchestration (shipped)

- Config: `mine_on_exit`, stronger HUD nudges after long sessions.
- Submit-line keyword routes: “mine last 3 sessions” → `_cmd_memory_mine` (tests in
  `test_cli.py` / REPL).
- **`/memory audit`** → `learn.md` skill; `/learn` hidden alias.

### Phase D — Not merged

- `remember` vs `log_decision` (different stores).
- `/until` / `/graph` workflow control (only post-pass mine stays automatic).
- CLI `lmloop memory mine N` for scripting.

## Command tiers (REPL)

| Tier | Examples |
|------|----------|
| Core | `/memory`, `/context`, `/stats`, `/until`, `/graph`, `/continue`, `/skill`, packaged workflow skills |
| Advanced | `/decisions`, `/restore`, `/checkpoints`, `/compact`, `/undo`, `/memory reconcile`, skill `/learn` |
| Hidden | `/retro` (alias for `/memory mine`) |

## Success metrics

- Default `/help` line count stable or decreasing as features move to orchestration.
- New memory features land as skill/steer/config changes before new `CommandMeta` rows.
- README “Project memory” table fits **peek | write | reload** without duplicate stems.
