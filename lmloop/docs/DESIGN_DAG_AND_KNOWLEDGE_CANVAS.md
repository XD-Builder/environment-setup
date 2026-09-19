# Design: DAG workflows, workflow mining, and the knowledge canvas

**Status:** proposed (nothing here is implemented)
**Date:** 2026-09-19
**Depends on:** `graph.py` (authored graphs), `knowledge_graph.py` (opt-in JSONL graph), `memory.py`
**Companion:** [DESIGN_SANDBOX_AND_VERIFICATION.md](DESIGN_SANDBOX_AND_VERIFICATION.md)

Three features, deliberately in dependency order:

1. **DAG joins** — a node may wait on several predecessors. Sequential execution only.
2. **Workflow mining** — deterministic statistics over run logs, rule-based improvement
   proposals, optional LLM draft, human-approved save.
3. **Knowledge canvas** — a 2D pannable canvas of project memory, served by a
   loopback-only stdlib HTTP server, with a prompt_toolkit TUI over the same data layer.

---

## Part 0 — Adversarial review before any of it

| # | Tempting shortcut | Consequence | Requirement |
|---|---|---|---|
| 1 | "DAG means parallel nodes" | Two makers in one workspace: lost updates, interleaved `git` index state, `trash/` stamped per-process, and `memory.py`'s documented **single-writer** JSONL assumption violated. Corruption is silent and only visible days later in `recall_memory` | **R-SEQ**: DAG describes *dependencies*; execution is topological and sequential. Parallelism is a separate, later design requiring one worktree + one container per branch |
| 2 | Auto-run a mined workflow | The agent writes its own control flow and then executes it. Any mining bug becomes an autonomous action | **R-HUMAN**: proposals are files + a `y/N`, exactly like `skills new`. Nothing mined ever runs unreviewed |
| 3 | Mine from 3 runs | Overfitting to noise; a proposal that encodes one bad night as policy | **R-N**: minimum sample (`propose_min_runs`, default 5 terminal runs) or the command prints stats and refuses to draft |
| 4 | Let the LLM invent acceptance commands | Hallucinated `--check 'make verify'` where no such target exists; the gate silently errors to nonzero forever, or worse, to zero | **R-EXEC**: any command in a draft is dry-run validated (`command -v` / `--help` probe) and labeled `unverified` in the draft; the human approves |
| 5 | Web UI on Flask/FastAPI | Transitive tree (`starlette`, `anyio`, `click`, `h11`, `pydantic`…) into a project whose loop is stdlib `urllib`. Each is a supply-chain surface for a local dev tool | **R-STDLIB**: `http.server` only, zero new runtime dependencies |
| 6 | Bind `0.0.0.0` "so I can view it from my laptop" | Project memory — learnings, decisions, file paths, and whatever a transcript captured — served unauthenticated to the LAN, and to any coffee-shop network | **R-BIND**: loopback only; a non-loopback bind requires an explicit flag and prints a warning |
| 7 | "It's localhost, it's safe" | Any web page you visit can `fetch('http://127.0.0.1:8765/api/graph')`, and DNS rebinding defeats naive origin checks | **R-ORIGIN**: `Host` header allowlist, `Origin` rejection, no CORS headers, `X-Frame-Options: DENY`, token-to-cookie exchange |
| 8 | Token in the URL forever | Leaks into shell history, `ps` output, browser history, and any `Referer` | **R-TOKEN**: one-shot `?t=` exchanged for an `HttpOnly; SameSite=Strict` cookie, then 302 to a clean URL; `Referrer-Policy: no-referrer` |
| 9 | Pull a graph library from a CDN | Breaks the "everything stays on your machine" promise, and a CDN request tells a third party a canvas was opened | **R-OFFLINE**: assets are files in the package; CSP forbids remote origins |
| 10 | Render 5k nodes as DOM elements | Multi-second layout, unusable pan/zoom | **R-CAP**: server-side layout + node cap + viewport culling on a single `<canvas>` |
| 11 | Serve session transcripts in the canvas | Transcripts are the most likely place for a pasted secret to sit | **R-MIN**: v1 serves learning/decision text and node metadata; session nodes expose path + timestamp only |
| 12 | A long-lived UI daemon | lmloop is a process that exits; a forgotten server is an open port on a laptop for weeks | **R-IDLE**: idle shutdown, foreground process, Ctrl-C stops it |
| 13 | Write edges from the canvas in v1 | A UI mutation path into append-only memory, without the confirm-gate discipline the tools have | v1 is read-only. Writes are a later design with the same gate story as `graph_add_edge` |
| 14 | Add node/edge types freely | Old lmloop versions reading new JSONL must not crash | **R-ADD**: new types are additive to the existing frozensets; unknown types already fall through `_graph_node_live` as live |

---

## Part 1 — DAG joins in authored graphs

### 1.1 What changes

Today `graph.py` walks one edge per outcome: `next_step` finds `edge_for(node, status)`
and runs the target. That is a state machine, not a DAG — a node cannot wait for two
predecessors, so "review and lint both finished, now package" is unexpressible.

Add exactly one keyword:

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
status of `pass` in this run.** Edges still drive traversal; `needs` is a guard.

### 1.2 Semantics (exact)

- `NodeDef` gains `needs: tuple[str, ...] = ()`.
- "Latest status" = the most recent `node` row for that name in this `GraphRun`. A node
  that later fails and is re-run invalidates the join; a downstream `needs` re-blocks.
- When traversal reaches a node whose `needs` are unmet, `next_step` returns the
  **first unmet predecessor that is reachable from the start** and runs that instead —
  deterministic, topological, sequential (R-SEQ).
- If an unmet predecessor is unreachable, the graph is invalid and is rejected at parse
  time, not at runtime.
- Join handoff: the concatenation of each predecessor's last summary, labeled and
  clipped to `join_handoff_chars` (default 1500 each), in `needs` order. Deterministic;
  no model call to merge.
- `graph_max_steps` counts join-driven entries like any other node entry.

### 1.3 Validation (fail closed, before any model call)

Parse-time errors, all with the node name in the message:

1. `needs` names an unknown node.
2. `needs` names the node itself.
3. `needs` forms a cycle **through `needs` alone** (retry cycles through `edge` stay
   legal — `qa -> build on fail` must keep working).
4. A `needs` predecessor is unreachable from the start node.
5. `mine` nodes may not have `needs` (mine is terminal bookkeeping).

Implementation note: reachability and the `needs`-cycle check are a Kahn topological
sort over the `needs` relation plus a BFS over `edge`s. Both are ~20 lines of stdlib and
belong in `graph.py` next to the existing validation, not in a new module.

### 1.4 Explicit non-goals

- **Parallel execution.** R-SEQ. Revisit only with `git worktree` + per-branch sandbox
  containers, which is a larger design than this file.
- **An LLM router choosing the next node.** Already rejected in
  [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md); `needs` is authored.
- **Conditional expressions on edges** (`on pass and coverage>80`). If a condition is
  worth having, it is an acceptance command with an exit code.

---

## Part 2 — Workflow identification and proposal

### 2.1 Deterministic first: `lmloop flow`

Everything useful here is computable without a model. A new leaf module
`workflow.py` reads `until/*.jsonl` and `graphs/*/*.jsonl` and produces a `FlowStats`
dataclass:

| Metric | Source | Why it matters |
|---|---|---|
| Runs by outcome (`pass` / `gate no` / abandoned-paused) | terminal row | Is the loop finishing at all? |
| Maker cycles per run: median, p90, max | `maker` rows | p90 near `until_max_steps` means the budget, not the goal, is ending runs |
| Check command → pass rate, mean cycles to first pass | `check` rows | Flaky or impossible gates |
| Eval `blocked` rate | `eval` rows | A checker that cannot see enough evidence |
| Node transition frequencies per graph | `node` rows | The actual topology, versus the authored one |
| Gate denials by command shape | `approve` rows + `GatePolicy` denials | Which irreversible actions the loop keeps needing |
| Skills used per run | `uses_skill` edges | Which playbooks earn their keep |
| Wall-clock per node | row timestamps | Where the night went |

`lmloop flow` prints this. `lmloop flow --json` emits it for scripting. This is the
"automated workflow identification" deliverable and it has **zero** model calls, so it
cannot hallucinate.

### 2.2 Rule-based enhancement proposals

Also deterministic, also in `workflow.py`, as a list of `(predicate, suggestion)` rows —
code, not prompt:

| Rule | Fires when | Suggestion |
|---|---|---|
| `budget-bound` | p90 maker cycles ≥ 0.9 × `until_max_steps` and outcome usually paused | Raise `until_max_steps` or split the goal into two `until` nodes |
| `weak-gate` | `--check` passes on cycle 1 in ≥ 80% of runs | Enable `require_negative_baseline`; the check likely passes before any work |
| `flaky-gate` | Same check alternates pass/fail with no intervening maker edit | Quarantine the check; add `--accept` for the stable part |
| `blocked-loop` | Eval `blocked` ≥ 30% | Add an authored `on blocked` edge; the missing edge is costing a HITL gate each time |
| `repeat-denial` | The same denied command shape in ≥ 3 runs | Pre-approve it via config, or move the work into an acceptance command |
| `dead-node` | A node never entered across ≥ 5 runs | Remove it from the graph |
| `hot-cycle` | One `on fail` edge accounts for ≥ 50% of transitions | That node needs a `--accept` list, not more retries |

Output is a bulleted report with the evidence line for each ("`build -> qa on fail`:
14/26 transitions"). No file is written.

### 2.3 `lmloop graph propose [name]`

Only after the deterministic layer exists, and only when
`len(terminal_runs) >= propose_min_runs` (default 5), may a draft be generated:

1. `workflow.py` renders a **compact, factual** summary (the tables above, no prose).
2. One `_chat` call, **no tools**, with a frozen prompt in `lmloop/skills/_graph_author.md`
   (private, `_`-prefixed, like `_author.md`) asking for graph markdown only.
3. The draft is parsed by `graph.parse_graph` **before display**. Parse failure → show
   the error and the raw draft, save nothing.
4. Every `--check` / `--accept` command in the draft is probed for existence
   (`command -v <argv0>` through the configured exec backend) and annotated
   `# unverified: <cmd>` where the probe fails (R-EXEC).
5. A unified diff against the existing graph of that name (if any) is printed.
6. Saved to `~/.lmloop/graphs/<name>.md` only on `y`. Packaged graphs are never
   overwritten — the user copy shadows them, which is the existing override rule.

`propose` becomes a reserved graph name (same pattern as `commands.py`'s reserved skill
stems) so `lmloop graph propose` can never collide with a graph called `propose`.

### 2.4 Growing the knowledge network during runs

Today `knowledge_graph.py` records `in_session`, `references`, and `uses_skill`. Runs
themselves are invisible to it. Add, deterministically (no model), when `use_graph` is
on (R-ADD, additive to the frozensets):

| New node type | Key | Written by |
|---|---|---|
| `run` | `until:<ts>` / `graph:<name>:<ts>` | `loop.run_until` / `graph.run_graph` on terminal status |
| `goal` | slug of the goal text | same |

| New edge type | Meaning |
|---|---|
| `ran_node` | `run` → `skill` / `concept` node entered during the run |
| `verified_by` | `run` → `concept` node for each check/acceptance command |
| `produced` | `run` → `learning` mined from that run |

This is what makes the canvas a *network that grows as the agent works* rather than a
static dump of learnings: after a week of `until` runs, the canvas shows which goals
touched which files, which checks guarded them, and which learnings came out.

Cost control: one node per run and one edge per distinct node/command, written once at
terminal status — not per cycle. A 24/7 month of hourly runs adds ~700 nodes, which is
inside the canvas cap.

---

## Part 3 — Knowledge canvas

### 3.1 Data layer (shared by both UIs)

One accessor on `KnowledgeGraph`, so the browser and the TUI cannot drift:

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
    x: float           # layout, computed server-side
    y: float

@dataclass(frozen=True)
class CanvasView:
    nodes: tuple[CanvasNode, ...]
    edges: tuple[tuple[str, str, str], ...]   # (from_id, to_id, edge_type)
    truncated: bool
    total_nodes: int
```

`KnowledgeGraph.canvas_view(query="", types=(), limit=canvas_max_nodes) -> CanvasView`.
Filtering and layout happen **server-side** so the client stays a renderer and the TUI
gets the same coordinates.

**Layout** is deterministic and dependency-free: nodes are banded by type on the y-axis
(decisions, learnings, concepts, files, skills, sessions, runs), ordered within a band by
degree then key, with a seeded jitter derived from `sha1(id)` so positions are stable
across reloads. No force simulation, no SciPy, no D3 — and stable positions matter more
than pretty ones when a human is building a mental map over weeks.

### 3.2 Browser UI: `lmloop canvas`

**Server:** `http.server.ThreadingHTTPServer`, one new module `canvas.py`
(may import `knowledge_graph`, `memory`, `config`; must not import `agent`, `loop`, or
`graph`).

**Startup sequence:**

1. Refuse to start when `use_graph` is false (print the existing `MSG_GRAPH_OFF`).
2. Bind `127.0.0.1` on `canvas_port` (default `0` = ephemeral, printed). A non-loopback
   `canvas_bind` requires `--unsafe-bind` and prints a warning naming the exposure (R-BIND).
3. Generate a 32-byte `secrets.token_urlsafe` token.
4. Print `http://127.0.0.1:<port>/?t=<token>` and open the browser unless `--no-browser`.
5. Serve in the foreground. Ctrl-C stops it. Idle beyond `canvas_idle_timeout_m`
   (default 30) shuts down and says so (R-IDLE).

**Every response carries:**

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data'; connect-src 'self'; base-uri 'none'; form-action 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cache-Control: no-store
```

**Request guard, in order, before routing:**

1. `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` — this is the DNS
   rebinding defense, and the reason an origin check alone is insufficient (R-ORIGIN).
2. `Origin`, if present, must match the same; otherwise 403. No CORS headers are ever
   emitted.
3. Auth: valid `lmloop_canvas` cookie, or a `?t=` that matches — in which case set
   `Set-Cookie: lmloop_canvas=<token>; HttpOnly; SameSite=Strict; Path=/` and 302 to
   `/`, so the token leaves the URL bar immediately (R-TOKEN).
4. Only `GET` and `HEAD` are routed. Anything else is 405 (v1 is read-only).

**Routes:**

| Route | Returns |
|---|---|
| `GET /` | `index.html` from the package |
| `GET /static/canvas.js`, `/static/canvas.css` | Package files, `nosniff`, no directory traversal (path is matched against a literal allowlist, not joined from user input) |
| `GET /api/graph?q=&types=&limit=` | `CanvasView` as JSON, with an `ETag` of the nodes/edges file mtimes+sizes |
| `GET /api/node/<type>/<key>` | One node's detail plus its 1-hop neighbors |
| `GET /api/stats` | Counts, contradiction clusters, truncation flag |

**Client:** one `<canvas>`, ~300 lines of dependency-free JS. Pan (drag), zoom (wheel,
clamped), click-to-select with a side panel, `/` to focus search, arrow keys to walk
neighbors, and viewport culling so only visible nodes are drawn (R-CAP). Polls
`/api/graph` every 5s with `If-None-Match`; a 304 costs nothing, so a canvas left open
beside a running agent updates as the network grows, without SSE or websockets.

**Redaction (`canvas_redact`, default true):** served `detail` strings are passed
through a small set of named compiled patterns for obvious secret shapes (`AKIA…`,
`ghp_…`, `sk-…`, `-----BEGIN … PRIVATE KEY-----`, `Bearer <jwt>`), replaced with
`[redacted]`. This is not a secret scanner and must not be described as one — it is a
shoulder-surfing and screenshot guard for a UI whose whole purpose is displaying text
the agent wrote (R-MIN).

### 3.3 Terminal UI: `lmloop canvas --tui`

Same `CanvasView`, `prompt_toolkit` full-screen (already a dependency, no new deps):

- Left: scrollable, selectable node list grouped by type, with confidence and age.
- Right: selected node's detail, provenance (`ts`, `source`, session path), and 1-hop
  neighbors as a selectable list — so keyboard traversal of the network works exactly
  like the browser's arrow-key walk.
- Bottom: `/` search, `t` type filter, `g` jump to contradiction clusters, `q` quit.
- No sockets, no port, no token — nothing to expose. This is the right default for a
  remote/SSH session, and the browser UI is the right default for a local desktop.

### 3.4 Non-goals for the canvas

Editing memory, deleting nodes, running the agent from the UI, multi-project views,
authentication beyond the loopback token, TLS (loopback only), and websockets.

---

## Part 4 — Config and commands

| Key | Default | Meaning |
|---|---|---|
| `join_handoff_chars` | `1500` | Per-predecessor clip in a join handoff |
| `propose_min_runs` | `5` | Terminal runs required before `graph propose` will draft |
| `canvas_port` | `0` | `0` = ephemeral; set a fixed port if you want a stable bookmark |
| `canvas_bind` | `127.0.0.1` | Non-loopback requires `--unsafe-bind` |
| `canvas_idle_timeout_m` | `30` | Auto-shutdown |
| `canvas_max_nodes` | `2000` | Server-side cap; `truncated` is surfaced in the UI |
| `canvas_open_browser` | `true` | `--no-browser` overrides |
| `canvas_redact` | `true` | Mask obvious secret shapes in served text |

New stems (two, both with README rows and `CommandMeta` entries):

| Command | Effect |
|---|---|
| `lmloop flow [--json]` | Deterministic workflow stats + rule-based suggestions |
| `lmloop graph propose [name]` | Draft a graph from mined runs; review; save on `y` |
| `lmloop canvas [--tui] [--port N] [--no-browser]` | Knowledge canvas |
| `/flow`, `/canvas` | REPL equivalents |

---

## Part 5 — Task breakdown

### Phase D — DAG joins

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| D1 | `needs` parsing on `node` lines; `NodeDef.needs` | `graph.py` | `needs a b` parses; `needs` on `mine` rejected; quoted rest unaffected | Existing `company.md` parses unchanged |
| D2 | Validation: unknown/self/cycle-through-`needs`/unreachable predecessor | `graph.py` | One test per rule, each fail-closed with the node name | No invalid graph reaches a model call |
| D3 | `next_step` join guard + first-unmet-predecessor selection | `graph.py` | Diamond graph runs `a,b` before `c`; re-failed predecessor re-blocks; `graph_max_steps` counts join entries | Topological order asserted on a diamond and on a re-entry |
| D4 | Deterministic join handoff with labels and per-predecessor clip | `graph.py` | Two predecessors → both labeled summaries present, each clipped | No model call merges handoffs |
| D5 | Packaged example graph using a join (small; does not replace `company`) | `graphs/*.md` | Parses; runner test with fakes | `lmloop graph` lists it |
| D6 | Docs: ARCHITECTURE workflow-graph section, README graph rows, this file → shipped | docs | — | `needs` documented next to `edge` |

### Phase W — workflow mining

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| W1 | `workflow.py` leaf: `FlowStats` from until/graph JSONL | `workflow.py` | Fixture logs → exact medians, rates, transition counts; empty history → empty stats, no crash | Pure function over rows, no I/O beyond `memory.read_jsonl` |
| W2 | Rule engine: seven rules as `(predicate, suggestion)` rows with evidence strings | `workflow.py` | One test per rule firing and not firing | Rules are data, extendable without touching the printer |
| W3 | `lmloop flow` / `/flow` + `--json` | `commands.py`, `cli.py`, `repl.py`, `ui.py` | Routing; JSON shape is stable | Works with `use_graph` off |
| W4 | `graph propose`: sample gate, `_graph_author.md`, parse-before-display, command probing, diff, `y/N` save | `graph.py`, `skills/_graph_author.md`, `cli.py`, `repl.py` | Below `propose_min_runs` → refuses and prints stats; unparseable draft → nothing written; `n` → nothing written; packaged graph never overwritten | No path writes a graph without `y` |
| W5 | Reserved graph name `propose` | `graph.py`, `commands.py` | A graph file named `propose.md` is rejected with a clear message | Stem collision impossible |
| W6 | Run/goal nodes + `ran_node` / `verified_by` / `produced` edges at terminal status | `knowledge_graph.py`, `loop.py`, `graph.py` | Written once per run, not per cycle; `use_graph` off → nothing written; unknown types tolerated by old readers | A finished run appears in `/memory graph` |
| W7 | Docs: README memory table rows, ARCHITECTURE memory section | docs | — | New node/edge types documented |

### Phase C — canvas

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| C1 | `CanvasNode` / `CanvasView` + `canvas_view()` with filter, cap, deterministic layout | `knowledge_graph.py` | Same input → identical coordinates across runs; cap sets `truncated`; decayed nodes excluded | Layout is a pure function of the data |
| C2 | `canvas.py` server: bind, token→cookie, `Host`/`Origin` guard, security headers, GET/HEAD only, idle shutdown | `canvas.py` | Wrong `Host` → 403; missing token → 403; `POST` → 405; token in URL → 302 + cookie; idle → shutdown; non-loopback bind without flag → refuses | Every guard has a test; no live browser needed |
| C3 | JSON routes + ETag/304 | `canvas.py` | `/api/graph` shape; unchanged mtimes → 304; `/api/node` 1-hop neighbors | Polling costs a 304 |
| C4 | Static assets: `index.html`, `canvas.js`, `canvas.css`, literal-allowlist routing | `canvas/` | Traversal attempt (`/static/../../config.json`) → 404; no remote URL appears in any asset | `rg -n 'https?://' lmloop/canvas/` is empty |
| C5 | Client: pan/zoom/select/search/neighbor-walk with viewport culling | `canvas/canvas.js` | Manual check documented; unit-test the pure layout/culling helper if extracted | 2000 nodes pan smoothly |
| C6 | `canvas_redact` patterns as named compiled constants | `canvas.py` | Each pattern masks; ordinary text untouched | Documented as a screenshot guard, not a scanner |
| C7 | `--tui` full-screen prompt_toolkit over the same `CanvasView` | `canvas.py` or `canvas_tui.py` | Key bindings; no import of the browser server path | `--tui` opens no socket |
| C8 | `lmloop canvas` stem, `/canvas`, README + ARCHITECTURE + DEVELOPMENT rows | `commands.py`, `cli.py`, `repl.py`, docs | Routing; `/help` lists it once | Docs state loopback-only and read-only |

### Sequencing

- **W1–W3 first.** Deterministic stats are useful immediately, have no model and no UI,
  and they are the evidence base everything else needs.
- **C1 before C2.** The data layer is shared; building the server first guarantees the
  TUI diverges.
- **D1–D4 are independent** of both and can land in parallel with either.
- **W4 (`graph propose`) last** — it is the only part with a model in the loop, and it
  should be built on statistics that have already been read by a human a few times.
- **W6 depends on nothing**, but it is most valuable once the canvas exists to show it.

---

## Part 6 — Risk register

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| Canvas exposes memory to the network | P0 | R-BIND + R-ORIGIN + R-TOKEN + read-only | A local process running as the same user can read `~/.lmloop` anyway |
| DNS rebinding into `/api/graph` | P0 | `Host` allowlist, checked before routing | Requires the guard to run on *every* path including static |
| Mined workflow runs itself | P0 | R-HUMAN: file + `y/N`, never auto-run | A human can still approve a bad graph |
| Parallel DAG corrupts memory or the tree | P0 | R-SEQ: sequential only, parallelism deferred | Feature request stays open |
| Hallucinated commands in a draft | P1 | R-EXEC probe + `# unverified` annotation + diff | The human must read the diff |
| Proposal overfits | P1 | R-N minimum sample + evidence strings on every suggestion | Statistics from an unrepresentative week |
| Canvas performance | P1 | R-CAP server cap, culling, 304 polling | Very dense graphs still cluster visually |
| Secret in a learning is displayed | P1 | R-MIN (no transcripts) + `canvas_redact` | Redaction is pattern-based and partial by construction |
| New node/edge types break old readers | P2 | R-ADD additive frozensets, `.get` readers | Old `/memory graph` shows unfamiliar types |
| Forgotten canvas server | P2 | R-IDLE timeout + foreground process | A 30-minute window on loopback |

---

## Part 7 — What this explicitly does not become

- Not a workflow engine. `needs` is a guard; there are no expressions, retries policies,
  or timers beyond what `until` already has.
- Not a dashboard. The canvas has no run controls, no log tailing, no metrics.
- Not multi-user. One project, one process, loopback, read-only.
- Not a graph database. Layout and traversal stay linear scans over JSONL that a human
  can still `cat`, which is the property that made this memory system worth keeping.
