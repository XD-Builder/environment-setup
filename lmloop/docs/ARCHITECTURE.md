# lmloop — Technical Architecture

A small, fully-local research and coding agent. Runs any model in LM Studio (or any OpenAI-compatible server), gives it real tools, and wraps it in persistent per-project memory. Everything stays on your machine.

**Python 3.9+ stdlib only** for the agent core. The REPL adds `prompt_toolkit` and `rich`.

---

## Component Map

```
lmloop/
├── bin/lmloop                  # entrypoint script
├── setup-lmloop.sh             # venv + pip install + symlink
├── requirements.txt            # prompt_toolkit, rich
├── docs/
│   ├── ARCHITECTURE.md         # this file
│   └── DESIGN_LOOP_AND_GRAPH.md
├── lmloop/
│   ├── __init__.py             # version string
│   ├── __main__.py             # raise SystemExit(main())
│   ├── cli.py                  # argparse subcommands → REPL or one-shot
│   ├── repl.py                 # interactive loop with slash commands
│   ├── prompt.py               # prompt_toolkit session, completers
│   ├── agent.py                # the loop: chat call + multi-round tool dispatch
│   ├── tools.py                # tool registry: specs + Python impls
│   ├── memory.py               # JSONL learnings, decisions, sessions, checkpoints
│   ├── config.py               # ~/.lmloop/config.json, project slug resolution
│   ├── files_index.py          # git-aware @path completion + ref expansion
│   ├── markdown_view.py        # render assistant markdown to terminal
│   ├── ui.py                   # Console: ANSI theming, status bars, stats
│   └── skills/                 # packaged skill prompts (markdown playbooks)
│       ├── system.md           # system prompt: how to work, memory, safety
│       ├── _author.md          # meta-prompt for skill drafting (not user-facing)
│       ├── retro.md            # mine sessions → learnings
│       ├── investigate.md      # root-cause debugging
│       ├── review.md           # pre-landing code review
│       ├── compact.md          # compress thread for context recovery
│       ├── qa.md               # run tests, verify changes
│       ├── learn.md            # curate and audit learnings
│       └── ceo.md              # strategy / plan review
```

---

## The Agent Loop

**File:** `agent.py`

The heart of lmloop. A single function, `act()`, implements the multi-round tool loop:

```
for round_idx in range(max_rounds):
    msg, usage = _chat(cfg, model, messages, tool_specs)  # → OpenAI-compatible /chat/completions
    append assistant message to messages[]
    echo content to user
    if no tool_calls:
        return                          # final reply
    for each tool_call:
        result = impls[name](**args)    # run Python impl locally
        append tool result to messages[]
```

Key properties:

- **No SDK dependency.** Plain `urllib` against `/v1/chat/completions`. Swap `base_url` to point at Ollama, llama.cpp, or anything OpenAI-compatible.
- **Tool errors are reported back** as text so the model can self-correct instead of crashing the session.
- **Usage tracking.** Prompt/completion/total tokens accumulated per-turn; fed to the UI for context fill bars.
- **Streaming is on by default** (`stream: true` in config). Completions use SSE on the wire. By default the UI stays silent during generation (TTY spinner) and renders via `echo` when the round completes (REPL: rich markdown). Opt into live plain tokens with `echo_delta=True`. Set `stream: false` to wait for a non-SSE full reply.

### Server management (`ensure_server`)

Before the loop starts, `ensure_server()` checks if LM Studio is reachable. If not and `auto_start_server` is enabled, it runs `lms server start` and `lms load` (if the `lms` CLI is on PATH). Falls back to first available model if the configured model isn't loaded.

### Context limit detection

`get_context_limit()` queries LM Studio's native API (`/api/v0/models` or `/api/v1/models`) to discover the loaded model's context window. Users can override with `context_length` in config.

---

## Tools

**File:** `tools.py`

Each tool has a JSON Schema spec (for the model) and a Python callable (for execution). The `build_tools()` function returns both the OpenAI tool spec list and an impl dispatch dict.

| Tool | Purpose | Safety |
|------|---------|--------|
| `run_shell` | Execute shell commands | Destructive patterns (rm -rf, sudo, DROP TABLE…) require user y/N confirmation. Pipe/redirection syntax also requires confirmation. |
| `read_file` | Read file with line numbers | Scoped to current working directory. Max 400 lines per call. |
| `write_file` | Overwrite file | Scoped to current working directory. Creates parent dirs. |
| `list_dir` | List directory entries | Scoped to CWD. Skips hidden files. |
| `search_files` | Regex search (ripgrep or grep fallback) | Scoped to CWD. Max 50 matches. |
| `fetch_url` | HTTP(S) fetch with HTML→text extraction | Content fenced as untrusted data with injection warning. TLS verified. Max 1MB download, 10KB returned. |
| `remember` | Save a learning to project memory | Write to learnings.jsonl |
| `log_decision` | Record a durable decision | Write to decisions.jsonl |
| `recall_memory` | Keyword-search learnings + decisions | Read-only view |

Tool output is truncated to 12,000 chars to protect the context window.

---

## Memory System

**File:** `memory.py`

All state is human-readable files under `~/.lmloop/projects/<slug>/`. No database, no migrations.

### State layout

```
~/.lmloop/
├── config.json                     # user settings (base_url, model, max_rounds, …)
├── history                         # REPL prompt history (prompt_toolkit)
├── skills/<name>.md                # user-authored skills (override packaged)
└── projects/<slug>/
    ├── learnings.jsonl             # append-only; dedup at read time
    ├── decisions.jsonl             # event-sourced (decide / supersede)
    ├── sessions/<ts>.jsonl         # per-session transcript history
    └── checkpoints/<ts>-<t>.md     # context-save handoffs
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
- Used by `/retro` to mine learnings and by `/restore` to resume prior conversations.
- `read_session()` renders the most recent lines up to `max_chars` (default 20,000).

### Checkpoints (`checkpoints/*.md`)

- Markdown files with YAML frontmatter (title, timestamp).
- Created via `/save [title]` which asks the model to summarize the session.
- Auto-injected into context if < 14 days old.

---

## Context Injection

**File:** `agent.py::system_prompt()`

At session start, the system prompt is assembled from:

1. `skills/system.md` — core instructions (how to work, memory discipline, safety, style)
2. `memory.context_block()` — bounded snapshot of active decisions, top learnings, and recent checkpoint (if < 14 days old)

This keeps the system prompt small for local models with limited context windows.

---

## REPL

**File:** `repl.py` + `prompt.py`

The interactive mode uses `prompt_toolkit` for:

- **As-you-type completion** for `/` slash commands (with blurbs) and `@` file paths (git-aware).
- **Decorative prompt** showing `cwd · model ›`.
- **Bottom toolbar** showing context fill bar, session tokens, and turn count.
- **Double Ctrl-C to exit** (first dismisses completion menu, second exits).

Slash commands (`/help`, `/stats`, `/retro`, `/save`, `/restore`, …) are built at runtime so newly created skills appear immediately.

Non-interactive mode (piped input or `lmloop "task"`) falls back to plain `input()` without completions.

---

## CLI

**File:** `cli.py`

`argparse`-based subcommands:

| Command | Description |
|---------|-------------|
| `lmloop` | Interactive REPL |
| `lmloop "task"` | One-shot task, exits after reply |
| `lmloop skill <name> [task]` | Start with a skill prompt |
| `lmloop skills` | List skill prompts |
| `lmloop skills new <name> [brief]` | AI-draft a skill, review, then save |
| `lmloop memory [query]` | Peek curated learnings |
| `lmloop decisions` | Peek durable decisions |
| `lmloop history` | List session transcripts |
| `lmloop retro [N]` | Mine last N sessions into learnings |
| `lmloop models` | List models on server |
| `lmloop config get\|set\|show` | Manage settings |
| `lmloop completion zsh` | Print zsh completion script |

---

## Design Decisions (and why)

1. **OpenAI-compatible REST, no SDK.** The multi-round loop is stdlib `urllib`. Swappable backend.
2. **File-only memory, computed views.** Append-only JSONL means no corruption, no migrations. You can `cat`, `grep`, or hand-edit every piece of agent memory.
3. **Bounded context injection.** Local models have small contexts — the budget is respected. Only top-N learnings and active decisions injected at start; model pulls more on demand.
4. **Self-learning is curated, not automatic.** The `remember` tool has a quality bar (enforced by `skills/retro.md`). Noisy memory is worse than none.
5. **Safety gates in code, not just prompt.** Destructive shell patterns require user confirmation regardless of what the model wants. Web content is fenced as untrusted data.
6. **Skills as markdown playbooks.** Numbered steps, English conditionals, explicit report format. User skills override packaged ones with the same name.

---

## Lineage

The memory schema, context-recovery flow, skill-prompt style, careful-command gate, and injection fencing are distilled from [gstack](https://github.com/garrytan/gstack)'s patterns, reduced to what a local, single-user research loop needs.
