# Design: Capacity-aware DAG workflows, flow mining, and the terminal knowledge canvas

**Status:** proposed (nothing here is implemented)
**Date:** 2026-09-19
**Depends on:** `graph.py` (authored graphs), `server.py` (model server probing), `knowledge_graph.py`, `memory.py`
**Companion:** [DESIGN_SANDBOX_AND_VERIFICATION.md](DESIGN_SANDBOX_AND_VERIFICATION.md)

Four features, in dependency order:

1. **Capacity model** — how many agents this machine's model server can actually serve.
   A laptop running one local MoE gets **1**. A hosted endpoint gets a configurable cap.
2. **DAG joins** — a node may wait on several predecessors.
3. **Bounded parallelism** — fan-out only up to capacity, and only with real isolation.
4. **Terminal knowledge canvas** — a 2D pannable map of project memory in the terminal.
   No browser, no HTTP server, no port, no new dependency.

---

## Part 0 — Adversarial review before any of it

| # | Tempting shortcut | Consequence | Requirement |
|---|---|---|---|
| 1 | "DAG means run the ready nodes in parallel" | Two makers in one workspace: lost updates, interleaved `git` index state, `trash/` stamped per process, and `memory.py`'s documented **single-writer** JSONL assumption violated. Corruption is silent and surfaces days later in `recall_memory` | **R-ISO**: parallelism requires isolation — read-only nodes, or one `git worktree` per branch. Default is sequential |
| 2 | Fan out as wide as the DAG allows | A local server holding one `qwen3.6-A3B` instance has one KV budget. Two concurrent makers either queue (no gain, doubled latency variance, `timeout_s` risk) or force the server to split context, shrinking the window every agent's context bar was computed against | **R-CAP**: effective width = `min(server capacity, isolation capacity, ready nodes, per-graph cap)`; local default is 1 |
| 3 | Hardcode a concurrency number | Correct on the author's machine only. A hosted endpoint tolerates 8; a laptop tolerates 1 | **R-AUTO**: `auto` resolves from the configured `base_url` and the server's own report, with a hard cap and an explicit override |
| 4 | Assume a probe failure means "go wide" | An unreachable capability endpoint would silently license 8 concurrent agents against a laptop | **R-FLOOR**: every uncertainty resolves to 1 |
| 5 | Ignore 429/503 under parallel load | A hosted endpoint rate-limits, every branch retries, and the run burns its step budget on backoff | **R-DECAY**: multiplicative decrease on rate limiting, floor 1, never re-widens inside an invocation |
| 6 | Stream several agents to one TTY | Interleaved live markdown from three makers is unreadable and corrupts the live-region accounting in `display.py` | **R-TTY**: in parallel mode, children render one compact status line each; full output goes to their session logs |
| 7 | Auto-merge parallel branches | Silent `git merge` resolution inside an unattended loop is data loss with extra steps | **R-MERGE**: attempt a clean merge; any conflict is `blocked` → HITL gate, never auto-resolved |
| 8 | Let parallel children append to `learnings.jsonl` | Concurrent appends break the single-writer invariant that makes this memory system safe to `cat` | **R-SHARD**: children write per-branch shards; the parent merges at the join |
| 9 | Auto-run a mined workflow | The agent writes its own control flow and executes it; a mining bug becomes an autonomous action | **R-HUMAN**: proposals are a file plus `y/N`, exactly like `skills new` |
| 10 | Mine from 3 runs | Overfitting one bad night into policy | **R-N**: `propose_min_runs` (default 5) or the command prints stats and refuses to draft |
| 11 | Let the model invent acceptance commands | Hallucinated `make verify` that fails forever, or worse, passes vacuously | **R-EXEC**: commands in a draft are existence-probed and annotated `# unverified`; the human approves |
| 12 | Web UI for the knowledge graph | A stdlib HTTP server still means a bound port, a token scheme, DNS-rebinding defense, CSP, and a static asset pipeline — a large security and maintenance surface for a local dev tool, and a browser dependency for a project whose premise is a terminal | **R-TERM**: terminal only. The browser UI is rejected, not deferred-with-a-wink |
| 13 | Render every node every keypress | A thousand-node redraw makes panning feel broken over SSH | **R-DRAW**: server-side deterministic layout, cell bucketing, viewport culling, redraw on input only |
| 14 | Show session transcripts in the canvas | Transcripts are the likeliest place a pasted secret sits, and a canvas is the likeliest thing to be on screen during a screen share | **R-MIN**: learning/decision text and node metadata only; session nodes show path and timestamp |
| 15 | Add node/edge types freely | Old lmloop versions reading new JSONL must not crash | **R-ADD**: additive to existing frozensets; unknown types already fall through `_graph_node_live` as live |

### Rejected outright

- **A browser UI on a local port** (R-TERM). Every hazard it carried — bind address,
  token handling, DNS rebinding, CSP, asset routing, redaction for a shared screen —
  disappears with it, and the TUI reaches the same data through the same accessor.
- **Any new runtime dependency.** `prompt_toolkit` and `rich` are already required by
  the REPL; the canvas uses them and nothing else.
- **Parallel execution without isolation** (R-ISO).
- **An LLM router choosing the next node.** Already rejected in
  [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md); `needs` is authored.

---

## Part 1 — Capacity: how many agents may run at once

### 1.1 Where it lives

`server.py` already owns "what the model server is" (`LmsClient`, model listing,
`get_context_limit`, VLM detection). Capacity is the same kind of fact, so it extends
that type rather than founding a new module:

```python
@dataclass(frozen=True)
class ServerCapacity:
    max_agents: int        # effective, already clamped
    profile: str           # "local" | "remote"
    reason: str            # one line for /stats and the run-start banner
```

`LmsClient.capacity(cfg) -> ServerCapacity`. `graph.py` consumes it; `loop.py` does not
need it (a single `until` run is one agent by construction).

### 1.2 Resolution rules (deterministic, fail-closed)

```
max_parallel_agents = "auto" | <int>

1. explicit int            -> clamp(int, 1, parallel_hard_cap)         # default cap 8
2. auto + local profile    -> loaded model instances reported by the
                              server, else 1                            # laptop default
3. auto + remote profile   -> remote_default_parallel (default 4), clamped
4. any probe error/timeout -> 1                                         # R-FLOOR
```

**Local profile** = `base_url` host is a loopback address, `*.local`, or an RFC1918
address. That covers LM Studio on `127.0.0.1:1234` (the shipped default), Ollama, and a
llama.cpp server on the LAN. **Remote profile** = anything else, e.g. OpenRouter.

For the local profile, "loaded model instances" comes from LM Studio's native
`/api/v0/models` — the same endpoint `get_context_limit()` already calls, so this costs
no new request shape. One loaded instance means **one agent**, which is the correct
answer for a laptop serving `qwen3.6-A3B`: the weights, the KV cache, and the context
window are one shared budget, and a second concurrent maker does not create a second
machine. If a user has deliberately loaded two instances, the probe reports two and the
scheduler may use two — the server's own report is the authority, not a guess.

For the remote profile the ceiling is a **cost and rate-limit** decision, not a hardware
one, so it is configuration: `remote_default_parallel` (4) under `parallel_hard_cap`
(8). A user who wants 8 sets `max_parallel_agents: 8`.

### 1.3 Rate-limit decay (R-DECAY)

During a parallel graph invocation, an HTTP 429 or 503 from the model server halves the
effective width for the remainder of that invocation, floor 1, and prints one status
line: `[graph · rate limited — capacity 4 → 2 for this run]`. It never re-widens
mid-run; predictability beats throughput in a loop nobody is watching. The next
invocation starts from the resolved capacity again.

### 1.4 Surfacing

`/stats` and the graph run-start banner state the resolved capacity and why:

```
capacity: 1 · local server reports 1 loaded model · sequential
capacity: 4 · remote endpoint, max_parallel_agents auto · parallel where the DAG allows
```

A user should never have to guess whether their DAG fanned out.

---

## Part 2 — DAG joins

### 2.1 What changes

Today `graph.py` walks one edge per outcome, so "review and lint both finished, now
package" is unexpressible. Add one keyword:

```
node plan    skill ceo
node api     until --check 'pytest -q tests/api' implement the API
node ui      until --check 'pytest -q tests/ui' implement the UI
node package skill review needs api ui
node mine    mine

edge plan -> api
edge api  -> ui
edge ui   -> package
edge package -> mine on pass
edge package -> plan on fail
```

`needs a b` means: **`package` may not start until `api` and `ui` each have a latest
status of `pass` in this run.** Edges drive traversal; `needs` is a guard.

### 2.2 Semantics (exact)

- `NodeDef` gains `needs: tuple[str, ...] = ()`.
- "Latest status" = the most recent `node` row for that name in this `GraphRun`. A node
  that later fails and re-runs invalidates the join; downstream `needs` re-blocks.
- Reaching a node with unmet `needs` runs the first unmet predecessor reachable from the
  start — deterministic and topological.
- An unmet predecessor that is unreachable is a **parse-time** error, not a runtime one.
- Join handoff: each predecessor's last summary, labeled and clipped to
  `join_handoff_chars` (default 1500), in `needs` order. No model call merges handoffs.
- `graph_max_steps` counts join-driven entries like any other entry.

### 2.3 Validation (fail closed, before any model call)

1. `needs` names an unknown node.
2. `needs` names the node itself.
3. `needs` forms a cycle **through `needs` alone** (retry cycles via `edge` stay legal —
   `qa -> build on fail` must keep working).
4. A `needs` predecessor is unreachable from the start.
5. `mine` nodes may not have `needs`.

Kahn topological sort over `needs` plus BFS over `edge`s; both are short stdlib routines
belonging next to the existing validation in `graph.py`.

---

## Part 3 — Bounded parallelism

Parallelism is off unless three things are simultaneously true: capacity ≥ 2, an
isolation mode is configured, and the DAG actually has independent ready nodes.

### 3.1 Isolation modes (`parallel_isolation`)

| Mode | Default | What may run in parallel | Isolation mechanism |
|---|---|---|---|
| `off` | ✅ | nothing | — |
| `readonly` | | Nodes marked `--readonly` (review, qa, research, audit) | `readonly=True` tool set: no `write_file`/`update_file`/`move_file`/`delete_file`/`remember`/`log_decision`. They cannot collide because they cannot write |
| `worktree` | | Any node | One `git worktree` per branch, one sandbox container per worktree under `--docker`, per-branch memory shards |

`readonly` is the mode most users should want: fan-out review and QA over a shared tree
is safe by construction and needs no git surgery. `worktree` is for genuine parallel
implementation and carries the merge problem below.

### 3.2 Scheduler

```
width = min(capacity.max_agents,
            isolation_capacity,          # 1 when parallel_isolation == off
            len(ready_nodes),
            graph_max_parallel)          # optional per-graph cap
```

- Ready = `needs` satisfied, inbound edge traversed, not already running.
- Children are threads (`ThreadPoolExecutor`, already the pattern in
  `tools.run_tool_calls`), each running `isolated_act` / `run_until` on its own thread
  and its own session log.
- **The parent owns the run log.** Children return results; only the parent appends
  `node` rows, each carrying a `par_group` field. `GraphRun` stays single-writer.
- A failing branch does not cancel its siblings. All branches in a group complete, the
  parent records them in declaration order, then edges are applied in that order. This
  keeps the log replayable and the behavior explainable at 3 AM.
- Crash mid-group re-runs the whole group on resume (rows are written after completion,
  matching today's "event written last" rule). Worktrees from a crashed group are
  detected by name and reused or cleaned on resume.

### 3.3 Display under parallelism (R-TTY)

Live markdown streaming is single-agent by nature. In a parallel group, children run
with `echo_delta=False`: one line per node, updated in place —
`[api · maker 3/12 · run_shell pytest -q]` — and the full transcript goes to the session
log. The join node's output streams normally, because by then only one agent is running.

### 3.4 Worktree mode specifics

- Branch: `lmloop/par/<run-ts>/<node>` created from HEAD via
  `git worktree add <dir> -b <branch>`; the worktree lives under
  `~/.lmloop/projects/<slug>/worktrees/<run-ts>/<node>`, **never** inside the project.
- Each branch's sandbox (when `--docker`) mounts its own worktree, so the container
  identity hash from the companion doc naturally differs per branch.
- Memory shards (R-SHARD): children write `learnings.<node>.jsonl` /
  `decisions.<node>.jsonl` in the project dir; the parent appends them into the canonical
  files at the join, in declaration order, preserving append-only semantics.
- Merge (R-MERGE): at the join, the parent attempts `git merge --no-ff` of each branch in
  declaration order. A conflict aborts the merge (`git merge --abort`), records
  `blocked`, and raises the existing HITL gate with the conflicting paths listed.
  lmloop never resolves a conflict.
- Cleanup: worktrees and branches are removed after a successful join; kept on `blocked`
  so the human can inspect them; pruned by age with the same 14-day horizon as `trash/`.

### 3.5 Honest defaults for the shipped configuration

With the shipped `base_url` (local LM Studio) and default config, capacity resolves to
**1**, `parallel_isolation` is **off**, and every graph runs exactly as it does today.
Nothing about the DAG work changes single-agent behavior — `needs` is a guard that a
sequential walker honors identically.

---

## Part 4 — Workflow identification and proposal

### 4.1 Deterministic first: `lmloop flow`

A new leaf `workflow.py` reads `until/*.jsonl` and `graphs/*/*.jsonl` into `FlowStats`:

| Metric | Source | Why it matters |
|---|---|---|
| Runs by outcome (pass / gate-no / abandoned-paused) | terminal row | Is the loop finishing at all? |
| Maker cycles per run: median, p90, max | `maker` rows | p90 near `until_max_steps` means the budget ended the run, not the goal |
| Check command → pass rate, cycles to first pass | `check` rows | Flaky or impossible gates |
| Eval `blocked` rate | `eval` rows | A checker that cannot see enough evidence |
| Node transition frequencies | `node` rows | The real topology versus the authored one |
| Gate denials by command shape | `approve` rows | Which irreversible actions the loop keeps needing |
| Wall-clock per node, and per `par_group` | timestamps | Whether parallelism actually paid |
| Capacity used per run | run-start banner row | Did the DAG ever fan out? |

`lmloop flow` prints it; `lmloop flow --json` emits it. Zero model calls, so it cannot
hallucinate.

### 4.2 Rule-based enhancement proposals

Also deterministic, also `workflow.py`, as `(predicate, suggestion)` rows — code, not
prompt:

| Rule | Fires when | Suggestion |
|---|---|---|
| `budget-bound` | p90 cycles ≥ 0.9 × `until_max_steps`, usually paused | Raise the budget or split the goal |
| `weak-gate` | `--check` passes on cycle 1 in ≥ 80% of runs | Enable `require_negative_baseline` |
| `flaky-gate` | Same check alternates pass/fail with no intervening edit | Quarantine it; add `--accept` for the stable part |
| `blocked-loop` | Eval `blocked` ≥ 30% | Author an `on blocked` edge |
| `repeat-denial` | Same denied command shape in ≥ 3 runs | Pre-approve it, or move the work into an acceptance command |
| `dead-node` | Never entered across ≥ 5 runs | Remove it |
| `hot-cycle` | One `on fail` edge ≥ 50% of transitions | That node needs `--accept`, not more retries |
| `serial-fanout` | Independent nodes always run sequentially while capacity ≥ 2 | Consider `needs` + `parallel_isolation: readonly` |

Every suggestion prints its evidence line (`build -> qa on fail: 14/26 transitions`).
Nothing is written to disk.

### 4.3 `lmloop graph propose [name]`

Only when `terminal_runs >= propose_min_runs` (default 5):

1. `workflow.py` renders a compact factual summary.
2. One `_chat` call, **no tools**, frozen prompt in `lmloop/skills/_graph_author.md`
   (private, `_`-prefixed like `_author.md`).
3. The draft is parsed by `graph.parse_graph` **before display**; parse failure shows the
   error and saves nothing.
4. Each `--check` / `--accept` command is existence-probed (`command -v <argv0>` through
   the active exec backend) and annotated `# unverified: <cmd>` on failure (R-EXEC).
5. A unified diff against the existing graph of that name is printed.
6. Saved to `~/.lmloop/graphs/<name>.md` only on `y`. Packaged graphs are never
   overwritten; the user copy shadows them, per the existing override rule.

`propose` becomes a reserved graph name, mirroring `commands.py`'s reserved skill stems.

### 4.4 Growing the knowledge network as the agent works

Deterministic, no model, only when `use_graph` is on (R-ADD):

| New node type | Key | Written by |
|---|---|---|
| `run` | `until:<ts>` / `graph:<name>:<ts>` | `run_until` / `run_graph` at terminal status |
| `goal` | slug of the goal text | same |

| New edge type | Meaning |
|---|---|
| `ran_node` | `run` → `skill` / `concept` node entered during the run |
| `verified_by` | `run` → `concept` node for each check/acceptance command |
| `produced` | `run` → `learning` mined from that run |

One node per run, one edge per distinct target, written once at terminal status — not per
cycle. Hourly runs for a month add roughly 700 nodes, inside the canvas cap. This is what
turns the canvas into a network that grows with the work rather than a static dump.

---

## Part 5 — Terminal knowledge canvas

No server, no port, no browser, no new dependency (R-TERM).

### 5.1 Data layer

```python
@dataclass(frozen=True)
class CanvasNode:
    id: str            # "<type>:<key>"
    type: str
    label: str
    detail: str        # learning insight / decision text / path — never a transcript
    confidence: int    # effective, post-decay
    ts: str
    degree: int
    x: float           # deterministic layout
    y: float

@dataclass(frozen=True)
class CanvasView:
    nodes: tuple[CanvasNode, ...]
    edges: tuple[tuple[str, str, str], ...]   # (from_id, to_id, edge_type)
    truncated: bool
    total_nodes: int
```

`KnowledgeGraph.canvas_view(query="", types=(), limit=canvas_max_nodes) -> CanvasView`.

**Layout** is deterministic and dependency-free: nodes band by type on the y-axis
(decisions, learnings, concepts, files, skills, sessions, runs), order within a band by
degree then key, with seeded jitter from `sha1(id)`. No force simulation. Stable
positions across sessions matter more than pretty ones when a human is building a mental
map over weeks.

### 5.2 `lmloop memory canvas` / `/memory canvas`

No new command stem — this extends the existing `memory` stem, whose `arg_choices`
already carry `list` / `decisions` / `dump` / `graph` / `mine` / `reconcile`.

A `prompt_toolkit` full-screen application, three panes:

```
┌ canvas ─────────────────────────────────┬ detail ───────────────┐
│  ·  ·  ▣ ·   ·                          │ learning:venv-python  │
│     ·  ·  ·  ▣  ·   ·                   │ confidence 8 · 3d ago │
│  ·  ▣₃ ·                                │                       │
│                                         │ setup-lmloop.sh reuses│
│  [decisions] [learnings] [files] …      │ an existing venv …    │
├─────────────────────────────────────────┤ neighbors:            │
│ / search  t types  g clusters  q quit   │  → session:20260918   │
└─────────────────────────────────────────┴───────────────────────┘
```

- **2D canvas pane:** layout coordinates projected to character cells. Arrows / `hjkl`
  pan, `+` / `-` zoom, `Tab` cycles visible nodes, `Enter` selects. Cells holding several
  nodes show a count glyph (`▣₃`); selecting one lists them in the detail pane, so
  nothing is hidden by collision (R-DRAW).
- **Detail pane:** label, type, effective confidence, age, source, provenance path, and
  1-hop neighbors as a selectable list — keyboard traversal of the network.
- **Footer:** `/` search (server-side filter over the same accessor), `t` type filter,
  `g` jump to `contradiction_clusters()`, `q` quit.
- Viewport culling means only on-screen cells are composed; redraw happens on input, not
  on a timer.
- Read-only. Editing memory from the canvas is a later design with the same gate story as
  `graph_add_edge`.
- Degradation: `use_graph` off → print the existing `MSG_GRAPH_OFF`. `prompt_toolkit`
  unavailable → fall back to today's `/memory graph` text summary rather than failing.
- Live growth: `r` re-reads the JSONL files. Nodes appearing during a run show up on
  refresh; there is no watcher thread, no polling, and nothing to leave running.

### 5.3 Non-goals

Editing, deleting, running the agent from the canvas, multi-project views, a browser UI,
any network listener, and mouse support beyond what `prompt_toolkit` gives for free.

---

## Part 6 — Config and commands

| Key | Default | Meaning |
|---|---|---|
| `max_parallel_agents` | `auto` | `auto` \| int; auto = server-reported instances (local) or `remote_default_parallel` (remote) |
| `remote_default_parallel` | `4` | Auto width for a non-local `base_url` |
| `parallel_hard_cap` | `8` | Ceiling for any resolution or override |
| `parallel_isolation` | `off` | `off` \| `readonly` \| `worktree` |
| `graph_max_parallel` | `0` | Per-graph override; `0` = no extra cap |
| `join_handoff_chars` | `1500` | Per-predecessor clip in a join handoff |
| `propose_min_runs` | `5` | Terminal runs required before `graph propose` drafts |
| `canvas_max_nodes` | `2000` | Cap; `truncated` is surfaced in the footer |

Commands (one new stem, `flow`; everything else extends existing stems):

| Command | Effect |
|---|---|
| `lmloop flow [--json]` | Deterministic workflow stats + rule-based suggestions |
| `lmloop graph propose [name]` | Draft a graph from mined runs; review; save on `y` |
| `lmloop memory canvas` / `/memory canvas` | Terminal knowledge canvas |
| `/flow` | REPL equivalent of `lmloop flow` |

---

## Part 7 — Task breakdown

### Phase K — capacity

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| K1 | `ServerCapacity` + `LmsClient.capacity()` with the four resolution rules | `server.py` | Loopback/`.local`/RFC1918 → local; probe error → 1; explicit int clamped to the hard cap; remote → `remote_default_parallel` | Every uncertain path returns 1 |
| K2 | Local instance count read from the existing native models response | `server.py` | One instance → 1; two → 2; malformed payload → 1 | No new request shape |
| K3 | Capacity line in `/stats` and the graph run-start banner | `ui.py`, `status.py`, `graph.py` | Text states the number and the reason | A user never has to guess the width |
| K4 | R-DECAY: 429/503 halves width for the invocation, floor 1, one status line | `graph.py`, `chat.py` (error classification only) | Decay applied once per event; never re-widens | Rate limiting degrades predictably |

### Phase D — DAG joins

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| D1 | `needs` parsing; `NodeDef.needs` | `graph.py` | Parses; rejected on `mine`; `company.md` unchanged | Existing graphs parse identically |
| D2 | Validation: unknown / self / `needs`-cycle / unreachable | `graph.py` | One test per rule, fail-closed with the node name | No invalid graph reaches a model call |
| D3 | Join guard in `next_step` + first-unmet-predecessor selection | `graph.py` | Diamond runs `a,b` before `c`; re-failed predecessor re-blocks | Topological order asserted |
| D4 | Deterministic labeled, clipped join handoff | `graph.py` | Both predecessors present and clipped | No model merges handoffs |
| D5 | Docs: ARCHITECTURE workflow-graph section, README graph rows | docs | — | `needs` documented beside `edge` |

### Phase P — bounded parallelism

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| P1 | Scheduler width computation and ready-set derivation | `graph.py` | width = min of all four inputs; `off` → 1; capacity 1 → sequential regardless of DAG | Default config never fans out |
| P2 | `readonly` mode: `--readonly` node flag, thread pool, parent-only run-log writes, `par_group` | `graph.py` | Two readonly nodes run concurrently with fake acts; a write tool is absent from their specs; rows in declaration order | Read-only fan-out works with no git involvement |
| P3 | R-TTY compact child rendering | `graph.py`, `display.py`, `status.py` | Children use `echo_delta=False`; one line per node; join streams normally | No interleaved live markdown |
| P4 | `worktree` mode: create/reuse/cleanup, per-branch sandbox identity, memory shards | `graph.py`, `snapshot.py` | Worktree outside the project; shard merge order deterministic; crash-resume reuses by name | Parallel writes never share a tree |
| P5 | R-MERGE: ordered merge, conflict → abort → `blocked` gate listing paths | `graph.py`, `status.py` | Clean merge passes; conflict blocks and leaves the worktree for inspection | lmloop never resolves a conflict |
| P6 | Docs: parallelism section with the "default is sequential" statement first | docs | — | Defaults are unambiguous |

### Phase W — workflow mining

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| W1 | `workflow.py` leaf: `FlowStats` from run logs | `workflow.py` | Fixture logs → exact medians, rates, transitions; empty history → empty stats | Pure function over rows |
| W2 | Eight rules as data with evidence strings | `workflow.py` | Each rule fires and does not fire | Rules extendable without touching the printer |
| W3 | `lmloop flow` / `/flow` / `--json` | `commands.py`, `cli.py`, `repl.py`, `ui.py` | Routing; stable JSON shape | Works with `use_graph` off |
| W4 | `graph propose` with sample gate, parse-before-display, command probing, diff, `y/N` | `graph.py`, `skills/_graph_author.md`, `cli.py`, `repl.py` | Below the minimum → refuses with stats; unparseable → nothing written; `n` → nothing written; packaged never overwritten | No path writes a graph without `y` |
| W5 | Reserved graph name `propose` | `graph.py`, `commands.py` | `propose.md` rejected clearly | Stem collision impossible |
| W6 | `run` / `goal` nodes, `ran_node` / `verified_by` / `produced` edges at terminal status | `knowledge_graph.py`, `loop.py`, `graph.py` | Written once per run; `use_graph` off → nothing; old readers tolerate | A finished run appears in `/memory graph` |

### Phase C — terminal canvas

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| C1 | `CanvasNode` / `CanvasView` / `canvas_view()` with filter, cap, deterministic layout | `knowledge_graph.py` | Identical coordinates across runs; cap sets `truncated`; decayed nodes excluded | Layout is a pure function of the data |
| C2 | Projection + cell bucketing + viewport culling as pure helpers | `canvas_tui.py` | Collision bucket yields a count and lists members; culling excludes off-screen cells | Testable without a terminal |
| C3 | Full-screen app: panes, pan/zoom, select, neighbor walk, search, type filter, clusters, refresh | `canvas_tui.py` | Key bindings dispatch to the helpers; no import of `agent`/`loop`/`graph` | Keyboard traversal works end to end |
| C4 | `memory canvas` arg + `/memory canvas` + graceful degradation | `commands.py`, `cli.py`, `repl.py` | `use_graph` off → `MSG_GRAPH_OFF`; no `prompt_toolkit` → text summary | No new stem appears in `/help` |
| C5 | Docs: README memory table row, ARCHITECTURE memory section, DEVELOPMENT module row | docs | — | Docs state terminal-only and read-only |

### Sequencing

- **K1–K3 first.** Capacity is small, has no UI, and everything about parallelism is
  meaningless without it. On the shipped local default it resolves to 1 and proves the
  no-change claim.
- **D1–D4 next**, independent of capacity; joins are useful sequentially.
- **P1–P3 after both**, and `readonly` before `worktree` — most of the value with none of
  the git risk.
- **W1–W3 anytime**; they only read logs.
- **C1 before C2/C3**, so the TUI consumes the shared accessor from the first commit.
- **W4 last** — the only part with a model in the loop, best built on statistics a human
  has already read a few times.

---

## Part 8 — Risk register

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| Parallel makers corrupt the tree or memory | P0 | R-ISO (`off` default, `readonly` safe by construction, `worktree` otherwise) + R-SHARD | `worktree` mode still needs a human for conflicts |
| Over-subscribing a laptop model server | P0 | R-CAP + R-AUTO + R-FLOOR: local resolves to 1 | A user forcing `max_parallel_agents: 8` on a laptop gets what they asked for |
| Silent auto-merge loses work | P0 | R-MERGE: abort and block | Run stalls for a human, by design |
| Mined workflow runs itself | P0 | R-HUMAN | A human can still approve a bad graph |
| Rate limiting burns the step budget | P1 | R-DECAY | Throughput drops for the rest of the invocation |
| Unreadable interleaved output | P1 | R-TTY compact child lines | Debugging a parallel group means reading session logs |
| Hallucinated commands in a draft | P1 | R-EXEC probe + `# unverified` + diff | The human must read the diff |
| Proposal overfits | P1 | R-N minimum sample + evidence strings | Statistics from an unrepresentative week |
| Secret displayed in the canvas | P1 | R-MIN: no transcripts, metadata only for sessions | Learning text is shown as written |
| Canvas sluggish over SSH | P2 | R-DRAW culling, cap, redraw on input | Very dense graphs cluster visually |
| New node/edge types confuse old readers | P2 | R-ADD additive frozensets, `.get` readers | Old `/memory graph` shows unfamiliar types |

---

## Part 9 — What this explicitly does not become

- Not a workflow engine. `needs` is a guard; no expressions, retry policies, or timers
  beyond what `until` already has.
- Not a distributed system. Parallelism is threads in one process on one machine, bounded
  by what the model server says it can serve.
- Not a dashboard or a web app. The canvas is a terminal view with no listener.
- Not a graph database. Layout and traversal stay linear scans over JSONL a human can
  still `cat` — the property that made this memory system worth keeping.
