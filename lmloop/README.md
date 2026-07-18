# lmloop — a local agent loop for LM Studio

A small, fully-local research and coding agent. It runs any model you have in
LM Studio (or any OpenAI-compatible server), gives it real tools (shell, files,
code search, web fetch), and wraps it in persistent per-project memory so it
gets smarter about your projects over time. Everything — model, loop, memory —
stays on your machine.

Zero pip dependencies. Python 3.9+ stdlib only. State is human-readable
JSONL/markdown under `~/.lmloop/`.

## Setup

```bash
./setup.sh agent          # from the environment-setup repo root
# or directly:
bash lmloop/setup-lmloop.sh
```

Run tests:

```bash
cd lmloop && PYTHONPATH=. python3 -m unittest discover -s tests -v
```

This symlinks `lmloop` into `~/.local/bin`. LM Studio's `lms` CLI is optional
but recommended: with it installed, lmloop auto-starts the server
(`lms server start`) and auto-loads a model (`lms load`) when nothing is running.

## Usage

```bash
lmloop                                  # interactive REPL
lmloop "why does setup.sh fail on linux?"   # one-shot task
lmloop --skill investigate "vim plug install hangs"
lmloop --skill review                   # review the current branch diff
lmloop retro                            # mine the last 3 sessions into learnings
lmloop memory [query]                   # what has it learned about this project?
lmloop decisions                        # durable decisions on record
lmloop history                          # recent session transcripts
lmloop models                           # models loaded on the server
lmloop config show|get|set              # settings (~/.lmloop/config.json)
```

Inside the REPL:

| Command | Effect |
|---------|--------|
| `/skills` | list skill prompts with short descriptions |
| `/ceo [plan]` | strategy / plan review (gstack CEO mode, condensed) |
| `/review [scope]` | pre-landing code review with confidence gate |
| `/investigate [symptom]` | root-cause debugging (no fix before RCA) |
| `/learn [query]` | curate and audit project learnings |
| `/qa [scope]` | run tests and verify changes end-to-end |
| `/compact [focus]` | compress long thread into a handoff summary |
| `/undo` | drop last user turn from in-memory thread |
| `/skill <name> [task]` | run any prompt in `prompts/` |
| `/history [n]` | list recent session logs (global `#` matches `/restore`) |
| `/checkpoints [n]` | list saved checkpoints (global `#` matches `/restore`) |
| `/restore checkpoint\|session <query> [fresh]` | inject checkpoint or prior transcript; `fresh` starts a new chat first |
| `/decisions` | show active project decisions |
| `/context` | show learnings/decisions/checkpoint injected into the system prompt |
| `/continue [message]` | resume after max_rounds or an interruption |
| `/save [title]` | checkpoint the session for a future session to resume |
| `/retro` | extract learnings from this session immediately |
| `/memory [query]` | peek at project memory |
| `/model <name>` | switch model mid-session (no arg lists models) |
| `/stats` | token usage, turns, rounds, context breakdown |
| `/new` | fresh conversation (memory context re-injected) |
| `/help` | list commands |
| `/quit` | exit |

REPL UX:

- **Startup prompt** is a bare `›` — no message count until you send your first message.
- **Status strip** on the prompt line (when the terminal is wide enough) shows context fill, session tokens, and user turns, e.g. `[████░░░░ 8.2k/32k (26%) · 2.3k session · 1 turn]`. On narrow terminals it falls back to a line above `›`.
- **Post-turn footer** shows a context bar, token breakdown, rounds, and tools.
- **Tab completion** works for `/commands` and `/skill` names (macOS libedit supported).
- **Colors** are on when stdout is a TTY; disable with `NO_COLOR=1` or `lmloop config set color false`.
- **Context window** is auto-detected from LM Studio (`/api/v0/models`); override with `lmloop config set context_length 8192`. Reply reserve defaults to 2048 tokens (`context_reserve`).

## Architecture

```
┌────────────┐   /v1/chat/completions + tools   ┌───────────────┐
│  lmloop     │ ───────────────────────────────▶ │ LM Studio     │
│  CLI/REPL   │ ◀─────────────────────────────── │ (any model)   │
└─────┬──────┘        tool_calls                 └───────────────┘
      │ runs tools locally, feeds results back (multi-round loop)
      ▼
 shell · read/write file · list_dir · search (rg) · fetch_url
 remember · log_decision · recall_memory
      │
      ▼
 ~/.lmloop/projects/<slug>/          (slug = git remote or dir name)
 ├── learnings.jsonl     append-only; latest-wins dedup; confidence decay
 ├── decisions.jsonl     event-sourced; supersede retires old decisions
 ├── sessions/*.jsonl    full transcript history
 └── checkpoints/*.md    resumable session handoffs
```

Design choices, and why:

- **OpenAI-compatible REST, no SDK.** Works with system Python 3.9, no venv,
  no pip. The multi-round tool loop (model → tool_calls → local execution →
  results → model) is ~60 lines in `agent.py`. Swap `base_url` to point at
  Ollama, llama.cpp server, or anything else OpenAI-compatible.
- **File-only memory, computed views.** Append-only JSONL means no corruption
  and no migrations; dedup/decay happen at read time. You can `cat`, grep, or
  hand-edit every piece of agent memory.
- **Bounded context injection.** Session start injects only the top-N learnings
  (confidence-decayed) and active decisions; the model pulls more on demand via
  `recall_memory`. Local models have small contexts — the budget is respected.
- **Self-learning is curated, not automatic.** The model saves learnings through
  an explicit `remember` tool with a quality bar (see `prompts/retro.md`), and
  `lmloop retro` mines past transcripts. Noisy memory is worse than none.
- **Safety gates in code, not just prompt.** Destructive shell patterns
  (rm -rf, sudo, force-push, DROP TABLE…) require a y/n from you regardless of
  what the model wants. Web content is fenced as untrusted data.

## Lineage

The memory schema, context-recovery flow, skill-prompt style, careful-command
gate, and injection fencing are distilled from
[gstack](https://github.com/garrytan/gstack)'s patterns, reduced to what a
local, single-user research loop needs.

## Extending

- **New skill:** drop a markdown file in `lmloop/prompts/<name>.md` — numbered
  steps, English conditionals, explicit report format. It's immediately
  available as `--skill <name>`, `/skill <name>`, and `/name` when a shortcut exists.
- **New tool:** add an impl + JSON-schema spec in `tools.py::build_tools`.
- **Different server:** `lmloop config set base_url http://localhost:11434/v1`
  (Ollama example), `lmloop config set model llama3.1`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No context bar / `limit unknown` in `/stats` | LM Studio native API unreachable; set `lmloop config set context_length <n>` to match your loaded model |
| Tab completion silent | Restart `lmloop`; macOS uses libedit — Tab only completes `/` commands |
| Garbled prompt input | Plain `›` prompt is intentional for readline; status prints above the prompt on narrow terminals |
| `DENIED` on shell commands | Destructive or pipe/redirection commands require typing `y` at the confirm prompt |
| Tools can't read `/etc/...` | File tools are scoped to the current working directory by design |

## Safety

File tools (`read_file`, `write_file`, `list_dir`, `search_files`) are constrained
to the current working directory. Shell commands without pipes run via `exec` (no
shell); pipes and destructive patterns require explicit user confirmation. HTTPS
fetches verify TLS certificates.
