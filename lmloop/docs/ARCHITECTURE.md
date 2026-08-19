# lmloop — Technical Architecture

A small, fully-local research and coding agent. Runs any model in LM Studio (or any OpenAI-compatible server), gives it real tools, and wraps it in persistent per-project memory. Everything stays on your machine.

**Python 3.10+ stdlib** for the agent core. The REPL adds `prompt_toolkit` and `rich`. Web search uses `ddgs` (no API key), then urllib fallbacks (DuckDuckGo HTML/Lite, Instant Answer, Wikipedia). How to change the code: [DEVELOPMENT.md](../DEVELOPMENT.md).

---

## Component Map

```
lmloop/
├── bin/lmloop                  # entrypoint script
├── setup-lmloop.sh             # venv + pip install + symlink
├── requirements.txt            # prompt_toolkit, rich, ddgs
├── DEVELOPMENT.md              # contribution practices
├── docs/
│   ├── ARCHITECTURE.md         # this file
│   ├── DESIGN_LOOP_AND_GRAPH.md  # knowledge-graph memory (shipped opt-in)
│   └── DESIGN_GRAPH_ENGINEERING.md  # control-flow graphs (shipped)
├── lmloop/
│   ├── __init__.py             # version string
│   ├── __main__.py             # raise SystemExit(main())
│   ├── cli.py                  # argparse subcommands → REPL or one-shot
│   ├── repl.py                 # interactive loop with slash commands
│   ├── prompt.py               # prompt_toolkit session, completers
│   ├── agent.py                # the loop: chat call + multi-round tool dispatch
│   ├── stream.py               # SSE ingest, overlap, repeat-halt
│   ├── display.py              # spinner, live printers, thinking lines
│   ├── tools.py                # tool registry: specs + Python impls
│   ├── memory.py               # JSONL learnings, decisions, sessions, checkpoints
│   ├── config.py               # ~/.lmloop/config.json, project slug resolution
│   ├── files_index.py          # @path completion + ref expansion (~, abs, relative)
│   ├── markdown_view.py        # render assistant markdown to terminal
│   ├── ui.py                   # Console: ANSI theming, status bars, stats
│   ├── commands.py             # slash/CLI command names + reserved skill stems
│   ├── status.py               # status/resume copy
│   ├── loop.py                 # until goal loop: isolated maker + check/eval
│   ├── graph.py                # authored workflow graphs: parser + runner
│   ├── steer.py                # always-on steering markdown + live clock
│   ├── skills/                 # packaged skill prompts (markdown playbooks)
│   │   ├── system.md           # system prompt: how to work, memory, safety
│   │   ├── _author.md          # meta-prompt for skill drafting (not user-facing)
│   │   ├── _graph_mine.md      # mine phase: propose graph edges (private)
│   │   ├── _reconcile.md       # review contradicts edges (private)
│   │   ├── retro.md            # mine sessions → learnings
│   │   ├── investigate.md      # root-cause debugging
│   │   ├── review.md           # pre-landing code review
│   │   ├── compact.md          # compress thread for context recovery
│   │   ├── qa.md               # run tests, verify changes
│   │   ├── learn.md            # curate and audit learnings
│   │   └── ceo.md              # strategy / plan review
│   ├── steer/                  # packaged always-on rules (injected, not /name)
│   │   ├── time.md             # relative windows; Clock is source of truth
│   │   ├── memory.md           # current question wins over context recovery
│   │   ├── skills.md           # when to follow a playbook
│   │   └── development.md      # model-facing coding bar
│   └── graphs/                 # packaged workflow graphs
│       └── company.md          # ceo → build → qa → mine
├── tests/                      # stdlib unittest; named test_<area>.py
```

---

## The Agent Loop

**File:** `agent.py`

The heart of lmloop. A single function, `act()`, implements the multi-round tool loop:

```
gather (tools on, up to max_rounds):
    msg, usage = _chat(..., tool_specs)
    if no tool_calls:
        return                          # natural final reply
    if this tool set was already run this turn:
        go to answer                    # exact name+args; not fuzzy text
    run tools; append results
answer (tools off, one call):
    msg, usage = _chat(..., tools omitted)
    return
```

Key properties:

- **No SDK dependency.** Plain `urllib` against `/v1/chat/completions`. Swap `base_url` to point at Ollama, llama.cpp, or anything OpenAI-compatible.
- **Tool errors are reported back** as text so the model can self-correct instead of crashing the session.
- **Usage tracking.** Prompt/completion/total tokens accumulated per-turn; fed to the UI for context fill bars.
- **Streaming is on by default** (`stream: true` in config). Completions use SSE (`stream.py`); tokens render live via `rich.Live` markdown when available (else plain tokens) in `display.py`. The live view grows with the answer up to the terminal and never shrinks (reflow cannot leave leftover rows in scrollback); it only tails if it would overflow. `finish()` reprints the full answer folded at the terminal width (list items included — never cropped). Live refreshes on new tokens only (`auto_refresh` off) and closes when `tool_calls` start, so a long `write_file` argument stream cannot redraw the same preamble into scrollback. A spinner shows until the first token, and again while tool arguments stream. Set `stream: false` for a non-SSE full reply. Within one SSE body, `stream.py` halt-loops repeating thinking/content (`_halted`). A halt with no tools is not done: gather nudges and continues. Across rounds, `act()` is gather/answer: tools stay on for up to `max_rounds` gather steps; a repeated tool set (exact name+args already run this turn) or that budget forces one tools-off answer.

### Server management (`ensure_server`)

Before the loop starts, `ensure_server()` checks if LM Studio is reachable. If not and `auto_start_server` is enabled, it runs `lms server start` and `lms load` (if the `lms` CLI is on PATH). Falls back to first available model if the configured model isn't loaded.

### Context limit detection

`get_context_limit()` queries LM Studio's native API (`/api/v0/models` or `/api/v1/models`) to discover the loaded model's context window. Users can override with `context_length` in config.

---

## Goal loop

**File:** `loop.py`

`until` is a while-statement around isolated `act()` calls. The maker does not declare the goal done — an external check or a **fresh** eval thread does.

```
until goal:
    maker  — isolated act() with the goal + prior handoff
    if --check: run that shell command (exit 0 → pass; nonzero → next maker)
    else: isolated eval act() with read-only tools (+ run_shell); last line STATUS: pass|fail|blocked
    fail → next maker cycle; blocked → y/N gate; pass → optional memory mine → done
```

- Maker and checker are different session logs. Missing `STATUS:` is `blocked`, never `pass`.
- Eval cannot `write_file`, `remember`, `log_decision`, or `graph_add_edge`. It may `run_shell` to verify.
- `--check` fail (nonzero exit) goes straight back to maker — no eval turn. Check `DENIED:` is `blocked` → gate.
- `until_max_steps` (default 12) counts maker cycles **this invocation**; pause, then `/continue` or `lmloop until` with no goal resumes.
- REPL `/continue` resumes `state.until_run` (the run this session started). `/new` clears that pointer and does not auto-resume a disk until-run. CLI `lmloop until` with no goal still resumes the latest open run.
- After the run stops (pass, pause, or interrupt), the REPL appends a handoff so follow-up questions have context. `lmloop until` on a TTY then enters the prompt loop (piped stdin still exits).
- Run state is append-only JSONL under `projects/<slug>/until/<ts>.jsonl`. The event is written **after** the step, so a crash retries the same role.
- `agent.py` / `stream.py` / `display.py` must not import `loop`. Handlers stay in `cli.py` / `repl.py`.

This is control-flow, not a knowledge graph. Knowledge-graph memory is opt-in (`use_graph`) in `memory.py`: JSONL nodes/edges, `recall_memory` hops, `/memory graph` and `/memory reconcile`. See [DESIGN_LOOP_AND_GRAPH.md](DESIGN_LOOP_AND_GRAPH.md).

---

## Workflow graph

**File:** `graph.py`

A graph is a list of named loops with **authored** sparse edges. Until is the inner node. There is no LLM router over a fully connected graph.

```
node <name> skill <skill> [task…]     isolated act + eval STATUS: (read-only tools) unless --check
node <name> until [--check cmd] <goal>  existing run_until
node <name> mine                      memory mine over this graph run's session logs
edge <from> -> <to> [on pass|fail|blocked]
```

- Packaged graphs live in `lmloop/graphs/*.md`; `~/.lmloop/graphs/<name>.md` overrides the same name.
- One packaged graph: `company` (ceo → build → qa → mine).
- Each node is an isolated thread. Handoff is the last assistant summary, not the eval `STATUS:` line or the tool transcript.
- Missing edge for fail/blocked → HITL gate; pass with no outgoing edge is terminal success.
- `graph_max_steps` (default 24) counts **skill/until node entries this invocation** (mine and HITL gate do not count). Pause, then `/continue` or `lmloop graph` with no name resumes.
- REPL `/continue` prefers a paused `state.graph_run` over `state.until_run`. `/new` clears both pointers.
- `lmloop graph <name>` starts a run (name required). `lmloop graph` with no name resumes the latest open graph run.
- Run logs: `projects/<slug>/graphs/<name>/<ts>.jsonl`. Event written last, so a crash retries the same node.
- `loop.py` must not import `graph`. `agent.py` / `stream.py` / `display.py` must not import `graph`. Handlers stay in `cli.py` / `repl.py`.

---

## Tools

**File:** `tools.py`

Each tool has a JSON Schema spec (for the model) and a Python callable (for execution). The `build_tools()` function returns both the OpenAI tool spec list and an impl dispatch dict.

| Tool | Purpose | Safety |
|------|---------|--------|
| `run_shell` | Execute shell commands | Destructive patterns (rm -rf, sudo, DROP TABLE…) require user y/N confirmation. Pipe/redirection syntax is off by default (`confirm_shell_syntax`); set that key to enable. |
| `read_file` | Read file with line numbers | Scoped to the workspace directory captured at session start, plus paths the user attached with `@` on this turn. Relative `path` resolves there; `~` and absolute paths work. The ⚙ line and result header show the resolved path. Max 400 lines per call. |
| `write_file` | Overwrite file | Scoped to the workspace directory captured at session start. Creates parent dirs. Result cites the resolved path. `@` attachments do not grant write access outside the workspace. |
| `list_dir` | List directory entries | Scoped to the session workspace, plus directories the user attached with `@` this turn. Includes dotfiles. |
| `search_files` | Regex search (ripgrep or grep fallback) | Scoped to the session workspace. Max 50 matches. |
| `web_search` | `ddgs` metasearch (no key), then DuckDuckGo HTML/Lite, Instant Answer, Wikipedia | Optional `ddgs` dep. Content fenced as untrusted. All backends failing → ERROR (do not paraphrase-retry). |
| `fetch_url` | HTTP(S) fetch with HTML→text + on-page links | Content fenced as untrusted. Returns final URL + HTTP status. TLS verified. Max 1MB download, ~10KB returned. |
| `remember` | Save a learning to project memory | Writes `~/.lmloop/projects/<slug>/learnings.jsonl`; tool result cites that path |
| `log_decision` | Record a durable decision | Writes `~/.lmloop/projects/<slug>/decisions.jsonl`; tool result cites that path |
| `recall_memory` | Keyword-search learnings + decisions | Read-only view. When `use_graph` is on, includes 1-hop graph neighbors. |
| `current_time` | UTC/local now plus 7/28/90-day lookback dates | No network. OS clock. For relative windows when Clock is stale. |
| `graph_add_edge` | Record a relationship between existing memory-graph nodes | Only registered when `use_graph` is true. Requires a `note`. |

Tools are registered once as `ToolDef` rows in `tools.build_tools()` (schema, validation, and impl). Slash/CLI command names and reserved skill stems come from `commands.py`. Status/resume copy lives in `status.py`.

Tool output is truncated to 12,000 chars to protect the context window.

---

## Memory System

**File:** `memory.py`

All state is human-readable files under `~/.lmloop/projects/<slug>/`. No database, no migrations. JSONL files assume a **single writer** (one REPL or CLI process per project); concurrent appends are out of scope.

### State layout

```
~/.lmloop/
├── config.json                     # user settings (base_url, model, max_rounds, …)
├── history                         # REPL prompt history (prompt_toolkit)
├── skills/<name>.md                # user-authored skills (override packaged)
├── steer/<name>.md                 # user always-on steering (concatenated)
├── graphs/<name>.md                # user-authored graphs (override packaged)
└── projects/<slug>/
    ├── learnings.jsonl             # append-only; dedup at read time
    ├── decisions.jsonl             # event-sourced (decide / supersede)
    ├── sessions/<ts>.jsonl         # per-session transcript history
    ├── checkpoints/<ts>-<t>.md     # context-save handoffs
    ├── until/<ts>.jsonl            # goal-loop run log (maker / check / eval)
    ├── graphs/<name>/<ts>.jsonl    # workflow-graph run log
    ├── graph_nodes.jsonl           # knowledge-graph nodes (when use_graph)
    └── graph_edges.jsonl           # knowledge-graph edges (when use_graph)
```

**Project slug** is resolved from git remote (`owner-repo`) or directory name.

### Learnings (`learnings.jsonl`)

- **Append-only.** Each row: timestamp, type, key, insight, confidence, source.
- **Dedup by key, latest-wins.** At read time, only the last row per key is returned.
- **Confidence decay.** Non-user-stated entries lose 1 confidence point per 30 days. Entries with effective confidence ≤ 0 are filtered out. User-stated entries never decay.
- **Bounded injection.** Session start injects only the top-N learnings (configurable, default 8). The model pulls more on demand via `recall_memory`.

### Decisions (`decisions.jsonl`)

- **Event-sourced.** Each row is either `decide` or `supersede`.
- **Supersede retires.** A `supersede` event references the ID of the decision it replaces. `get_decisions()` computes the active set by filtering out retired IDs.
- **Bounded injection.** Top-N active decisions injected at session start (default 6).

### Sessions (`sessions/*.jsonl`)

- Full transcript of role/content pairs. Created per REPL session.
- Used by `/memory mine` to mine learnings and by `/restore` to reload a prior conversation.
- `read_session()` renders log files (including truncated tool rows) up to `max_chars`.
- `format_messages_transcript()` renders the live thread (honors `/undo`) for `/compact` and this-session `/memory mine`. Tool rows are included truncated (live payloads can be huge); system messages are omitted.
- `session_messages()` rebuilds user/assistant turns for `/restore`. Tool/system rows are transcript-only (truncated, not API-shaped) and are omitted. Restore loads those turns into the in-memory thread and prints the last assistant reply — it does not start a new model turn.
- `/compact`, `/memory mine`, and `/save` run `act()` on a side thread so the live conversation is unchanged until the user confirms a compact replace (which starts a new session log).

### Checkpoints (`checkpoints/*.md`)

- Markdown files with YAML frontmatter (title, timestamp).
- Created via `/save [title]` which asks the model to summarize the session.
- Auto-injected into context if < 14 days old.

### Knowledge graph (`graph_nodes.jsonl`, `graph_edges.jsonl`)

Opt-in (`use_graph`, default false). Owned by a `KnowledgeGraph` dataclass in `memory.py`. When off, no graph files are created, auto-edges do not write (even if files already exist), and `recall_memory` is unchanged.

- Nodes are typed (`learning`, `decision`, `session`, `file`, `skill`, `concept`). Latest row per `(type, key)` wins. Learning/concept nodes reuse confidence decay; others stay live.
- Edges are typed (`leads_to`, `contradicts`, `in_session`, `references`, `uses_skill`, `related_to`, `supersedes`). Endpoints that decayed away are dropped at read time.
- First use backfills nodes from existing learnings, decisions, and sessions. `remember` / `log_decision` then add `in_session` and `references` (paths mentioned in the text). `/skill` records `uses_skill`.
- `recall_memory` does keyword match plus 1-hop neighbors. `graph_add_edge` (tool, `use_graph` only) requires a `note`. `/memory graph` prints counts and an adjacency list. `/memory reconcile` reviews `contradicts` clusters. `/memory mine` appends a graph-edge phase.

---

## Context Injection

**File:** `agent.py::system_prompt()`

At session start (and each isolated `/until` or graph `act()`), the system prompt is assembled from:

1. `skills/system.md` — core instructions (how to work, memory discipline, safety, style)
2. Live tool name list
3. `steer.clock_block()` — current UTC timestamp and calendar date (OS clock, rebuilt every call)
4. `steer.steering_block()` — concatenated `*.md` from packaged `lmloop/steer/`, `~/.lmloop/steer/`, then `<workspace>/.lmloop/steer/` (later dirs can contradict earlier; same-name files are additive, unlike skills)
5. `memory.context_block()` — bounded snapshot of active decisions, top learnings, and recent checkpoint (if < 14 days old)

This keeps the system prompt small for local models with limited context windows. The clock is injected so relative windows ("last 4 weeks") do not resolve to a training-cutoff year.

---

## REPL

**File:** `repl.py` + `prompt.py`

The interactive mode uses `prompt_toolkit` for:

- **As-you-type completion** for `/` slash commands (with blurbs) and `@` file paths (git-aware project index, plus filesystem completion for `~/`, `/`, `./`, `../`). Filesystem matches show the resolved path in the menu.
- **Decorative prompt** showing `cwd · model ›`.
- **Bottom toolbar** showing context fill bar, session tokens, and turn count.
- **Double Ctrl-C to exit** (first dismisses completion menu, second exits).
- **`@path` refs** on submit (`~/`, absolute, `./`, `../`, quoted spaces, or project-relative) append a “Referenced files” block with the **resolved** path. Missing tokens warn; existing ones are readable this turn even outside the workspace.

Slash commands (`/help`, `/stats`, `/memory`, `/until`, `/save`, `/restore`, …) are built at runtime so newly created skills appear immediately.

Non-interactive mode (piped input or `lmloop "task"`) falls back to plain `input()` without completions.

---

## CLI

**File:** `cli.py`

`argparse`-based subcommands:

| Command | Description |
|---------|-------------|
| `lmloop` | Interactive REPL |
| `lmloop "task"` | Task, then REPL prompt when stdin is a TTY (exits when piped) |
| `lmloop until [--check cmd] <goal>` | Goal loop, then REPL prompt when stdin is a TTY |
| `lmloop until` | Resume latest open until-run |
| `lmloop graph <name>` | Authored workflow graph, then REPL prompt when stdin is a TTY |
| `lmloop graph` | Resume latest open graph-run |
| `lmloop skill <name> [task]` | Skill, then REPL prompt when stdin is a TTY |
| `lmloop skills` | List skill prompts |
| `lmloop skills new <name> [brief]` | AI-draft a skill, review, then save |
| `lmloop retro [N]` | Alias for `memory mine` |
| `lmloop memory [query]` | Peek curated learnings |
| `lmloop memory mine [N]` | Mine last N sessions into learnings |
| `lmloop memory graph` | Knowledge-graph stats (requires `use_graph`) |
| `lmloop memory reconcile` | Review `contradicts` clusters (requires `use_graph`) |
| `lmloop decisions` | Peek durable decisions |
| `lmloop history` | List session transcripts |
| `lmloop models` | List models on server |
| `lmloop config get\|set\|show` | Manage settings |
| `lmloop completion zsh` | Print zsh completion script |

---

## Design Decisions (and why)

1. **OpenAI-compatible REST, no SDK.** The multi-round loop is stdlib `urllib`. Swappable backend.
2. **File-only memory, computed views.** Append-only JSONL means no corruption, no migrations. You can `cat`, `grep`, or hand-edit every piece of agent memory.
3. **Bounded context injection.** Local models have small contexts — the budget is respected. Only top-N learnings and active decisions injected at start; model pulls more on demand.
4. **Self-learning is curated, not automatic.** The `remember` tool has a quality bar (enforced by `skills/retro.md`). Noisy memory is worse than none.
5. **Safety gates in code, not just prompt.** Destructive shell patterns require user confirmation regardless of what the model wants. Pipe/redirection confirms are opt-in (`confirm_shell_syntax`). Web content is fenced as untrusted data.
6. **Skills as markdown playbooks.** Numbered steps, English conditionals, explicit report format. User skills override packaged ones with the same name.

---

## Lineage

The memory schema, context-recovery flow, skill-prompt style, careful-command gate, and injection fencing are distilled from [gstack](https://github.com/garrytan/gstack)'s patterns, reduced to what a local, single-user research loop needs.
