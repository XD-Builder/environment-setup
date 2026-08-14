# Design: Graph engineering for lmloop (next implementation)

**Status:** shipped (control-flow graphs). Knowledge-graph memory is shipped opt-in in [DESIGN_LOOP_AND_GRAPH.md](DESIGN_LOOP_AND_GRAPH.md)
**Date:** 2026-08-14
**Depends on:** `loop.py` (`until`), `memory mine`, DEVELOPMENT.md import graph

This is the **control-flow** spec: how several loops hand off. Knowledge-graph JSONL is [DESIGN_LOOP_AND_GRAPH.md](DESIGN_LOOP_AND_GRAPH.md). Do not add LangGraph.

An implementing agent should treat the **Ship** / **Do not ship** / **Tests** sections as the spec. Critique notes below are constraints, not optional flavor.

---

## Current stack (already shipped)

```
prompt / context → harness → loop (until) → graph (this proposal)
```

| Layer | Today |
|-------|--------|
| Loop | `until`: isolated maker `act()` until `--check` exit 0 or isolated eval `STATUS: pass\|fail\|blocked` |
| Graph | Skills are one-shot playbooks in one thread. No authored topology. |

Until is a one-node graph with an edge back to itself. A graph is worth it when **one loop is the wrong shape**: specialized roles that must hand off (the “run a company” case). Daily standup is still **OS cron + `lmloop skill …`**, not a graph.

### Known holes in until (shipped in phase 1)

These were correctness holes in the maker/checker split. **Shipped:**

1. **Eval is read-only.** Isolation is a fresh thread **and** a tool subset: eval omits `write_file` / `remember` / `log_decision`. It may still `run_shell` to verify.
2. **Check fail goes to maker.** `--check` nonzero exit skips eval. Eval runs only when there is no check command. Check `DENIED:` is `blocked` → gate (not eval).
3. **Overlapping runs.** Starting `/until` while another run is open already prints a hint (`superseded_until_hint`). `/continue` in the REPL still resumes `state.until_run` (the run this session started). CLI `lmloop until` with no goal still resumes **latest** open run. Do not auto-resume a week-old paused run from `/continue` after `/new`.
4. **Checker is prompt-only about STATUS.** Missing token → `blocked` is correct. Do not let the maker’s last line count as `STATUS:`.

Phase 1 stays in `loop.py` / `tools.py` / `agent.py` (`readonly=` on `build_tools` + `act`). No `graph.py` in that change.

---

## Phase 2: sparse authored graphs

Ship **one new stem**: `graph` / `/graph`. Resume stays `/continue` (and `lmloop graph` with no name resumes the latest open graph run). Do not add `graphs`, `graph continue`, or `graph status`.

### Why this shape

- A loop is the default. A graph is a **list of named loops plus authored edges**.
- Sparse edges only. **No LLM router over a fully connected graph** (CycGraph: that is often worse and more expensive than one ReAct agent).
- Each node runs **isolated** (reuse `loop.isolated_act` and/or `run_until`). No shared message thread across nodes.
- HITL is a node outcome (`blocked` → `ask_yes_no`), same as until, not a new framework primitive.

### Ship

- Module [`lmloop/lmloop/graph.py`](../lmloop/graph.py): `GraphDef`, `NodeDef`, `EdgeDef`, parser, `GraphRun`, `run_graph`.
- Packaged graphs under `lmloop/lmloop/graphs/*.md` plus user override `~/.lmloop/graphs/<name>.md` (same override rule as skills).
- One packaged graph: **`company`**. Not `daily`.
- `CommandMeta("graph", ..., cli=True)` — the only new stem.
- Run log: `~/.lmloop/projects/<slug>/graphs/<name>/<ts>.jsonl` (append-only; write the event **last**).
- After a **pass** on the terminal node, optional `memory mine` (reuse `until_mine` or a `graph_mine` key defaulting true).

### Do not ship

- LangGraph / ADK / AutoGen GraphFlow
- `graphs new`, `_graph_author.md`, or a skill-drafting flow for graphs
- Packaged `daily` (cron + `lmloop skill ceo` is enough)
- Knowledge-graph JSONL
- An LLM that can jump to any node
- In-process scheduler / daemon
- Parallel workers
- Growing `agent.py` with graph logic; growing `loop.py` with topology

### Import graph (do not invert)

- `graph.py` may import `loop` (`isolated_act`, `run_until`, `parse_eval_status`), `agent` (skills, `act` only through `isolated_act`), `memory`, `status`, `tools`
- `loop.py` must **not** import `graph`
- `agent.py` / `stream.py` / `display.py` must **not** import `graph`
- Handlers in `cli.py` / `repl.py`; names in `commands.py`

### Markdown format (line-based, no extra deps)

One graph = one markdown file. Parser ignores `#` headings and blank lines. No regex-driven language: `shlex.split` per line, first token is the kind.

```
# company — specialized roles with gates

node ceo    skill ceo
node build  until --check 'pytest -q' implement the agreed change
node qa     skill qa
node mine   mine

edge ceo -> build
edge build -> qa
edge qa -> mine   on pass
edge qa -> build  on fail
edge qa -> ceo    on blocked
```

**Node kinds** (closed set, named constant):

| Kind | Runner |
|------|--------|
| `skill <name> [task…]` | isolated `act()` with `load_skill` + optional task; then isolated eval `STATUS:` (read-only tools) unless the node sets `--check` |
| `until [--check cmd] <goal>` | existing `run_until` (phase-1 semantics) |
| `mine` | `memory mine` over this graph run’s session logs; no model eval |

**Edge `on` tokens:** `pass` (default if omitted), `fail`, `blocked`. Missing edge for an outcome → HITL gate “continue from this node? [y/N]”; no → done.

**Validation (fail closed, no run):**

- Unique node names; every edge endpoint exists
- At least one node; exactly one start node = first `node` line
- No fully-connected default; unknown `on` token is an error
- Cycles are allowed (qa → build) but **must be authored**
- `until_max_steps`-style `graph_max_steps` (default 24 **node** entries this invocation) pauses; `/continue` resumes

### GraphRun

Same bar as `UntilRun`: dataclass + append-only JSONL. Roles: `meta`, `node`, `gate`, `mine`, `pause`, `done`. Skill eval is folded into the `node` status (`pass`/`fail`/`blocked`), not a separate event. Handoff = last assistant summary from the node, not the eval `STATUS:` line or the tool transcript. Crash retries the same node (event written last).

`/continue`: if `state.graph_run` is paused, resume it; else if `state.until_run` is paused, resume until; else today’s max_rounds resume. **One resume verb.** CLI: `lmloop graph` with no name resumes latest open graph run.

### Packaged `company`

Keep it small. Roles already exist as skills:

1. `ceo` — plan / prioritize (skill)
2. `build` — until the check passes
3. `qa` — skill qa
4. `mine` — memory mine

Do not invent a five-org-chart company. User overrides by dropping `~/.lmloop/graphs/company.md`.

### Commands

| Today | After |
|-------|--------|
| (none) | `lmloop graph [name]` run packaged/user graph (default `company` if one name is too much magic — **require the name**) |
| | `/graph <name>` in the REPL |
| `/continue` | also resumes a paused graph run |

`/help` lists `graph`, not `graphs` / `graph status`.

Config keys (README rows): `graph_max_steps` (24), `graph_mine` (true). Reuse confirm gates; do not add a second shell-confirm policy.

---

## Phase 3 (later, not this change)

- User-authored graphs beyond `company` once the parser and runner are boring
- Hook / heartbeat / cron loop types (OS launches `lmloop skill` or `lmloop graph`; lmloop stays a process that exits)
- Knowledge-graph JSONL from DESIGN_LOOP_AND_GRAPH.md
- Parallel workers
- `graphs new` authoring

---

## Implementation sequence

One focused change set per phase. Do not mix phase 1 and 2 unless phase 1 is tiny (read-only eval + check-fail→maker). Prefer **two PRs**: until-hardening, then graph.

### Phase 1 (until hardening)

1. `build_tools(..., kinds=...)` or `readonly=True`: omit `write_file`; keep `read_file`, `list_dir`, `search_files`, `run_shell`, `web_search`, `fetch_url`, `recall_memory`. Drop `remember` / `log_decision` on eval too (checker must not write memory).
2. `act(..., tool_names=...)` or `readonly=` passed from `isolated_act` / `run_until` for the **eval** role only. Maker keeps full tools.
3. `next_role`: `("check", "fail")` → `"maker"` (not `"eval"`). `("check", "blocked")` → `"gate"` unchanged.
4. Tests: eval `write_file` not in specs; check fail never calls `act` for eval; existing pass/pause/gate tests still green.
5. Docs lockstep: ARCHITECTURE Goal loop bullet; README until paragraph.

### Phase 2 (graph)

1. `graph.py` types + parser + validation tests (no network)
2. `GraphRun` JSONL; `run_graph` with fakes (`isolated_act` / `run_until` patched where `graph.py` looks them up)
3. `commands.py` + CLI/REPL: `graph`; extend `/continue`
4. Packaged `graphs/company.md`
5. Docs: README table, ARCHITECTURE component map + “Workflow graph” section, DEVELOPMENT module row. Leave this file labeled **proposal** until the code exists; then move behavior into ARCHITECTURE and keep this file as history or delete the “not implemented” banner.

---

## Tests (required in the same diff as the code)

**Phase 1**

- Eval tool list has no `write_file` / `remember` / `log_decision`
- `--check` exit 0 → pass, never eval; nonzero → next maker, never eval
- Check DENIED → blocked → gate
- No-check path still eval; missing `STATUS:` → blocked

**Phase 2**

- Parser: nodes, edges, `on pass|fail|blocked`, quoted `--check`
- Validation: unknown node, duplicate name, missing start
- Runner: ceo→build→qa→mine on pass; qa fail → build (cycle); blocked → gate no → done
- `graph_max_steps` pauses; `/continue` resumes the graph, not a new until-run
- Isolated threads per node (each `isolated_act` sees `system`+`user` only)
- `/help` lists `/graph`, not `/graphs`
- No network; patch `act` / `run_until` where `graph.py` looks them up

---

## Engineering bar

Follow DEVELOPMENT.md. Stdlib only. One type owns the graph (`GraphDef`), one owns a run (`GraphRun`). No parallel name lists. Slash/CLI names live in `commands.py` only. Status copy in `status.py`. Docs in the same diff.
