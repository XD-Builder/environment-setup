# lmloop — a local agent loop for LM Studio

A small, fully-local research and coding agent. It runs any model you have in
LM Studio (or any OpenAI-compatible server), gives it real tools (shell, files,
code search, web search, web fetch), and wraps it in persistent per-project memory so it
gets smarter about your projects over time. Everything — model, loop, memory —
stays on your machine.

The agent loop is Python 3.10+ **stdlib** for HTTP and tools. The interactive REPL
requires [`prompt_toolkit`](https://github.com/prompt-toolkit/python-prompt-toolkit)
and [`rich`](https://github.com/Textualize/rich). Web search uses
[`ddgs`](https://github.com/deedy5/ddgs) (no API key), then urllib fallbacks.
Setup installs these into `lmloop/.venv`. State is human-readable JSONL/markdown
under `~/.lmloop/`.

## Setup

```bash
./setup.sh agent          # from the environment-setup repo root
# or directly:
bash lmloop/setup-lmloop.sh
```

Setup creates `lmloop/.venv`, installs `requirements.txt` (prompt_toolkit, rich, ddgs), and
symlinks `lmloop` into `~/.local/bin`.

Run tests:

```bash
cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Contribution practices (module layout, exceptions, where to patch tests) are in [DEVELOPMENT.md](DEVELOPMENT.md).

LM Studio's `lms` CLI is optional but recommended: with it installed, lmloop
auto-starts the server (`lms server start`) and auto-loads a model (`lms load`)
when nothing is running.

Zsh tab completion is wired via `shell/.zshrc` (`load_completion lmloop lmloop completion zsh`).
After installing the binary, open a new shell (or re-source `~/.zshrc`).

## Usage

```bash
lmloop                                  # interactive REPL
lmloop "why does setup.sh fail on linux?"   # one-shot task
lmloop until --check 'pytest -q' make tests pass
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

These are easy to mix up. **Peek** (read-only) vs **write** vs **reload**:

| Command | What it does | Read / write |
|---------|--------------|--------------|
| `lmloop memory [query]` | Peek **learnings** — curated tips (patterns, pitfalls, prefs) in `learnings.jsonl`. Deduped by key; confidence decays over time. | Peek |
| `lmloop decisions` | Peek **decisions** — durable choices with rationale in `decisions.jsonl` (can be superseded). | Peek |
| `lmloop history` | List **session transcript** files under `sessions/*.jsonl` (raw logs, not summarized). | Peek |
| `lmloop memory mine [N]` | Mine the last N **session files** (default 3) and **write new learnings**. CLI never mines “this conversation.” `/retro` and `lmloop retro` are aliases that print `[memory mine]` then do the same work. | Write |

In the REPL:

- **Peek:** `/memory`, `/decisions`, `/history`, `/context` (what is injected into the system prompt). Checkpoints newer than 14 days are auto-injected.
- **Write:** `/memory mine` with no arg mines **this session**; `/memory mine n` mines the last n **prior** session files (excludes the live log). `/learn` curates via tools. `/save [title]` writes a checkpoint file (live thread unchanged).
- **Reload:** `/restore` reopens a session (shows last result, no new model turn) or a checkpoint handoff. `/compact` summarizes in a side thread; optional replace starts a new log.
- `/undo` drops the last user turn **in memory only** — the session JSONL is not trimmed.

`/history` + `/checkpoints` show global `#` indices that match `/restore`. CLI `lmloop history` prints bare paths (no indices).

Inside the REPL:

| Command | Effect |
|---------|--------|
| `/skills` | list skills, or: new `<name>` [brief] to author one |
| `/ceo [plan]` | strategy / plan review (auto from `skills/ceo.md`) |
| `/review [scope]` | pre-landing code review |
| `/investigate [symptom]` | root-cause debugging |
| `/learn [query]` | curate and audit project learnings |
| `/qa [scope]` | run tests and verify changes end-to-end |
| `/compact [focus]` | summarize thread in a side session; optional replace |
| `/undo` | drop the last user turn from the in-memory thread |
| `/skill <name> [task]` | run any skill prompt by name |
| `/history [n]` | list recent session logs (global `#` matches `/restore`) |
| `/checkpoints [n]` | list saved checkpoints (global `#` matches `/restore`) |
| `/restore [session\|checkpoint] <query> [fresh]` | reload a prior session (shows last result) or checkpoint; `fresh` copies a session into a new log |
| `/decisions` | show active project decisions |
| `/context` | show memory injected into the system prompt |
| `/continue [message]` | resume after max_rounds, an interruption, or a paused `/until` |
| `/until [--check cmd] <goal>` | isolated maker/checker loop until a check or evaluator passes; then type to continue from a handoff |
| `/save [title]` | checkpoint session for later restore |
| `/memory [query \| mine [n]]` | peek learnings, or mine this session (or last n prior files) |
| `/model <name>` | switch model, or list models with no argument |
| `/stats` | show token usage and session activity |
| `/new` | reset conversation (memory context re-injected) |
| `/transcript` | view rendered session in less (`q` to quit) |
| `/help` | show available commands |
| `/quit` | exit (`/exit` and `/q` also work) |

Every file in `skills/` (except `system.md`) is also available as `/name` automatically. Packaged skill names `compact` and `retro` can be overridden by a user skill with the same name. `/retro` is a hidden alias for `/memory mine` (the `retro.md` playbook is what mining loads).

REPL UX (prompt_toolkit + rich):

- **Assistant replies** are rendered as markdown. Live preview grows with the answer (tails only if it would overflow the terminal); the final print wraps at the terminal width, including list items, so line tails are never cropped.
- **`/transcript`** pages the full session (rendered) through `less -R`; press `q` to return to the REPL.
- **Prompt** shows `cwd · model ›` (decorative only) with a **bottom toolbar** for context fill / session tokens / turns.
- **Type `/`** (or Tab) for slash commands with blurbs, e.g. `/ceo — strategy / plan review…`.
- **Type `@`** for project file paths (git-aware when possible). On every turn submit (freeform, `/skill`, `/name`, `/continue`, …), existing `@path` refs append a “Referenced files” block for the model.
- **Ctrl-C** dismisses an open `/` or `@` completion menu first; with no menu, once shows “Ctrl-C again to exit”, twice exits. Ctrl-D exits immediately.
- **Post-turn footer** shows a context bar, token breakdown, rounds, and tools.
- **Colors** for banners/tools when stdout is a TTY; disable with `NO_COLOR=1` or `lmloop config set color false`.
- **Streaming** is on by default (SSE). Tokens appear live as markdown (`rich.Live`); a spinner shows until the first token. Disable with `lmloop config set stream false`.
- **Context window** is auto-detected from LM Studio (`/api/v0/models`); override with `lmloop config set context_length 8192`.
- **Completion while typing** for `/` and `@` (no Tab required). Tab / Ctrl-Space still work.
- Prompt history is in `~/.lmloop/history` (↑/↓). Incremental Ctrl-R search is off (it conflicts with as-you-type completion).

## Architecture

```
┌────────────┐   /v1/chat/completions + tools   ┌───────────────┐
│  lmloop     │ ───────────────────────────────▶ │ LM Studio     │
│  CLI/REPL   │ ◀─────────────────────────────── │ (any model)   │
└─────┬──────┘        tool_calls                 └───────────────┘
      │ runs tools locally, feeds results back (multi-round loop)
      ▼
 shell · read/write file · list_dir · search (rg) · web_search · fetch_url
 remember · log_decision · recall_memory
 (ToolDef registry + commands.py + status.py as single sources of truth)
      │
      ▼
 ~/.lmloop/projects/<slug>/          (slug = git remote or dir name)
 ├── learnings.jsonl     append-only; latest-wins dedup; confidence decay
 ├── decisions.jsonl     event-sourced; supersede retires old decisions
 ├── sessions/*.jsonl    full transcript history
 ├── checkpoints/*.md    resumable session handoffs
 └── until/<ts>.jsonl    goal-loop run log (maker / check / eval)
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
  `lmloop memory mine` mines past transcripts. Noisy memory is worse than none.
- **Safety gates in code, not just prompt.** Destructive shell patterns
  (rm -rf, sudo, force-push, DROP TABLE…) require a y/n from you regardless of
  what the model wants. Pipe/redirection confirms are off by default (set
  `confirm_shell_syntax true` to enable). Web content is fenced as untrusted data.

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

### Config keys

Stored in `~/.lmloop/config.json`. `lmloop config show` lists current values;
zsh completion for `config set` is generated from these keys.

| Key | Default | Meaning |
|-----|---------|---------|
| `base_url` | `http://127.0.0.1:1234/v1` | OpenAI-compatible API root |
| `model` | *(empty)* | Empty = first model the server reports |
| `max_rounds` | `60` | Tool-loop rounds per `act()` call |
| `max_continue_nudges` | `2` | Auto-resume when the model narrates a next step without tools |
| `temperature` | `0.7` | Chat sampling temperature |
| `timeout_s` | `600` | HTTP timeout for chat completions (seconds) |
| `stream` | `true` | SSE tokens as they arrive; `false` waits for the full reply |
| `context_learnings` | `8` | Top-N learnings injected at session start |
| `context_decisions` | `6` | Top-N active decisions injected at session start |
| `confirm_shell` | `true` | Master switch: `false` disables **all** shell confirms |
| `confirm_destructive` | `true` | y/N for rm -rf, sudo, DROP TABLE, force-push, … |
| `confirm_shell_syntax` | `false` | y/N for pipes/redirection; off to avoid fatigue |
| `shell_timeout_s` | `120` | `run_shell` timeout |
| `web_timeout_s` | `30` | `web_search` / `fetch_url` timeout |
| `max_tool_output` | `12000` | Truncate tool results (chars) |
| `auto_start_server` | `true` | Run `lms server start` / `lms load` when nothing is listening |
| `color` | `true` | ANSI chrome; also honors `NO_COLOR=1` |
| `context_length` | `0` | `0` = auto-detect from LM Studio; else token window override |
| `context_reserve` | `2048` | Tokens reserved for the model reply on the fill bar |
| `until_max_steps` | `12` | Maker cycles per `until` invocation before pause (`/continue` or `lmloop until` resumes) |
| `until_mine` | `true` | After an until-run passes, mine learnings from its transcripts |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No context bar / `limit unknown` in `/stats` | LM Studio native API unreachable; set `lmloop config set context_length <n>` to match your loaded model |
| `prompt_toolkit` import error | Re-run `bash lmloop/setup-lmloop.sh` (creates `lmloop/.venv`) |
| Zsh completion missing | Ensure `lmloop` is on `PATH`, then re-source `~/.zshrc` (or run `lmloop completion zsh`) |
| `lmloop --skill …` fails | `--skill` was replaced by the subcommand: `lmloop skill <name> [task]` |
| `DENIED` on shell commands | Destructive patterns require typing `y`. Pipes/redirection only if `confirm_shell_syntax` is true. `confirm_shell false` disables all confirms. |
| Tools can't read `/etc/...` | File tools are scoped to the workspace directory captured at session start |

## Safety

File tools (`read_file`, `write_file`, `list_dir`, `search_files`) are constrained
to the workspace directory captured at session start. Relative paths are resolved
against that directory (not `$HOME`); the ⚙ tool line shows the resolved path.
Shell commands without pipes
run via `exec` (no shell) with that directory as `cwd`. Destructive patterns
require explicit user confirmation; pipes/redirection do not unless you set
`lmloop config set confirm_shell_syntax true`. HTTPS fetches verify TLS certificates.
`remember` appends to `~/.lmloop/projects/<slug>/learnings.jsonl` (slug = git remote
or directory name).
