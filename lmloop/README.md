# lmloop — a local agent loop for LM Studio

A small, fully-local research and coding agent. It runs any model you have in
LM Studio (or any OpenAI-compatible server), gives it real tools (shell, files,
code search, web fetch), and wraps it in persistent per-project memory so it
gets smarter about your projects over time. Everything — model, loop, memory —
stays on your machine.

The agent loop is Python 3.9+ **stdlib only**. The interactive REPL requires
[`prompt_toolkit`](https://github.com/prompt-toolkit/python-prompt-toolkit)
and [`rich`](https://github.com/Textualize/rich) (markdown rendering), installed
into `lmloop/.venv` by setup. State is human-readable JSONL/markdown under
`~/.lmloop/`.

## Setup

```bash
./setup.sh agent          # from the environment-setup repo root
# or directly:
bash lmloop/setup-lmloop.sh
```

Setup creates `lmloop/.venv`, installs `requirements.txt` (prompt_toolkit), and
symlinks `lmloop` into `~/.local/bin`.

Run tests:

```bash
cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

LM Studio's `lms` CLI is optional but recommended: with it installed, lmloop
auto-starts the server (`lms server start`) and auto-loads a model (`lms load`)
when nothing is running.

Zsh tab completion is wired via `shell/.zshrc` (`load_completion lmloop lmloop completion zsh`).
After installing the binary, open a new shell (or re-source `~/.zshrc`).

## Usage

```bash
lmloop                                  # interactive REPL
lmloop "why does setup.sh fail on linux?"   # one-shot task
lmloop skills                           # list skill prompts
lmloop skills new deploy "roll out staging safely"   # AI draft → review → save
lmloop skill investigate "vim plug install hangs"
lmloop skill review                     # review the current branch diff
lmloop models                           # models loaded on the server
lmloop config show|get|set              # settings (~/.lmloop/config.json)
lmloop completion zsh                   # print zsh completion script
```

### Creating skills

```bash
lmloop skills new <name> [brief…]
# or in the REPL:
/skills new <name> [brief]
```

lmloop asks the model to draft a skill playbook, prints the markdown for review,
then saves to `~/.lmloop/skills/<name>.md` only if you confirm with `y`.
User skills override packaged ones with the same name and show as `(user)` in
`lmloop skills`. They are available immediately as `lmloop skill <name>` and `/name`.


### Project memory commands

These four are easy to mix up:

| Command | What it does | Read / write |
|---------|--------------|--------------|
| `lmloop memory [query]` | Peek **learnings** — curated tips (patterns, pitfalls, prefs) in `learnings.jsonl`. Deduped by key; confidence decays over time. | Read |
| `lmloop decisions` | Peek **decisions** — durable choices with rationale in `decisions.jsonl` (can be superseded). | Read |
| `lmloop history` | List **session transcript** files under `sessions/*.jsonl` (raw logs, not summarized). | Read |
| `lmloop retro [N]` | Run the `retro` skill over the last N sessions and **write new learnings** into project memory. | Write |

In the REPL, `/memory` and `/decisions` are the same peeks; `/retro` mines *this* session; `/history` + `/restore` resume a prior transcript.

Inside the REPL:

| Command | Effect |
|---------|--------|
| `/skills` | list skill prompts; `/skills new <name> [brief]` authors a new one |
| `/ceo [plan]` | strategy / plan review (auto from `skills/ceo.md`) |
| `/review [scope]` | pre-landing code review |
| `/investigate [symptom]` | root-cause debugging |
| `/learn [query]` | curate and audit project learnings |
| `/qa [scope]` | run tests and verify changes end-to-end |
| `/compact [focus]` | compress long thread into a handoff summary |
| `/undo` | drop last user turn from in-memory thread |
| `/skill <name> [task]` | run any prompt in `skills/` |
| `/history [n]` | list recent session logs (global `#` matches `/restore`) |
| `/checkpoints [n]` | list saved checkpoints (global `#` matches `/restore`) |
| `/restore checkpoint\|session <query> [fresh]` | inject checkpoint or prior transcript; `fresh` starts a new chat first |
| `/decisions` | show active project decisions |
| `/context` | show learnings/decisions/checkpoint injected into the system prompt |
| `/continue [message]` | resume after max_rounds or an interruption |
| `/save [title]` | checkpoint the session for a future session to resume |
| `/retro` | extract learnings from this session immediately |
| `/memory [query]` | peek at project learnings |
| `/model <name>` | switch model mid-session (no arg lists models) |
| `/stats` | token usage, turns, rounds, context breakdown |
| `/new` | fresh conversation (memory context re-injected) |
| `/transcript` | open this session as rendered markdown in `less` (`q` returns to REPL) |
| `/help` | list commands |
| `/quit` | exit |

Every file in `skills/` (except `system.md`) is also available as `/name` automatically.

REPL UX (prompt_toolkit + rich):

- **Assistant replies** are rendered as markdown in the terminal (headings, lists, code fences).
- **`/transcript`** pages the full session (rendered) through `less -R`; press `q` to return to the REPL.
- **Prompt** shows `cwd · model ›` (decorative only) with a **bottom toolbar** for context fill / session tokens / turns.
- **Type `/`** (or Tab) for slash commands with blurbs, e.g. `/ceo — strategy / plan review…`.
- **Type `@`** for project file paths (git-aware when possible). On every turn submit (freeform, `/skill`, `/name`, `/continue`, …), existing `@path` refs append a “Referenced files” block for the model.
- **Ctrl-C** dismisses an open `/` or `@` completion menu first; with no menu, once shows “Ctrl-C again to exit”, twice exits. Ctrl-D exits immediately.
- **Post-turn footer** shows a context bar, token breakdown, rounds, and tools.
- **Colors** for banners/tools when stdout is a TTY; disable with `NO_COLOR=1` or `lmloop config set color false`.
- **Streaming** is on by default (SSE). The REPL shows a spinner while generating, then renders the reply as rich markdown. Disable with `lmloop config set stream false`.
- **Context window** is auto-detected from LM Studio (`/api/v0/models`); override with `lmloop config set context_length 8192`.
- **Completion while typing** for `/` and `@` (no Tab required). Tab / Ctrl-Space still work.
- Prompt history is in `~/.lmloop/history` (↑/↓; Ctrl-R incremental search).

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

- **OpenAI-compatible REST, no SDK for the agent.** The multi-round tool loop
  is stdlib urllib. The REPL uses prompt_toolkit for reliable completion.
  Swap `base_url` to point at Ollama, llama.cpp server, or anything else
  OpenAI-compatible.
- **File-only memory, computed views.** Append-only JSONL means no corruption
  and no migrations; dedup/decay happen at read time. You can `cat`, grep, or
  hand-edit every piece of agent memory.
- **Bounded context injection.** Session start injects only the top-N learnings
  (confidence-decayed) and active decisions; the model pulls more on demand via
  `recall_memory`. Local models have small contexts — the budget is respected.
- **Self-learning is curated, not automatic.** The model saves learnings through
  an explicit `remember` tool with a quality bar (see `skills/retro.md`), and
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

- **New skill:** `lmloop skills new <name> [brief]` (AI draft + confirm), or drop
  a markdown file in `~/.lmloop/skills/<name>.md` or `lmloop/skills/<name>.md` —
  numbered steps, English conditionals, explicit report format. Immediately
  available as `lmloop skill <name>`, `/skill <name>`, and `/name`.
- **New tool:** add an impl + JSON-schema spec in `tools.py::build_tools`.
- **Different server:** `lmloop config set base_url http://localhost:11434/v1`
  (Ollama example), `lmloop config set model llama3.1`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No context bar / `limit unknown` in `/stats` | LM Studio native API unreachable; set `lmloop config set context_length <n>` to match your loaded model |
| `prompt_toolkit` import error | Re-run `bash lmloop/setup-lmloop.sh` (creates `lmloop/.venv`) |
| Zsh completion missing | Ensure `lmloop` is on `PATH`, then re-source `~/.zshrc` (or run `lmloop completion zsh`) |
| `lmloop --skill …` fails | `--skill` was replaced by the subcommand: `lmloop skill <name> [task]` |
| `DENIED` on shell commands | Destructive or pipe/redirection commands require typing `y` at the confirm prompt |
| Tools can't read `/etc/...` | File tools are scoped to the current working directory by design |

## Safety

File tools (`read_file`, `write_file`, `list_dir`, `search_files`) are constrained
to the current working directory. Shell commands without pipes run via `exec` (no
shell); pipes and destructive patterns require explicit user confirmation. HTTPS
fetches verify TLS certificates.
