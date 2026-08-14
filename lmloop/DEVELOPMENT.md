# Contributing to lmloop

How to change this package without turning `agent.py` back into a catch-all, and
without accumulating glue that a senior engineer would have to unwind later.

Architecture (what the pieces are) lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
This file is the **how**: the engineering bar, where code belongs, and what
“done” means.

Treat this document as a contract. If a change fights it, change the code — or
update this file in the same diff, with a reason.

---

## Engineering bar

Every change is a chance to make the tree easier to read. Default stance:

1. **Be critical.** Do not land a workaround you would reject in review. Name
   the smell (duplicate registry, extra callback signature, swallowed exception,
   regex over tokens) and remove it.
2. **Leave less debt than you found** in files you touch. Delete dead helpers,
   unused arguments, parallel name lists, and `_safe_*` wrappers. Do not add a
   TODO that is really “I did not want to fix this.”
3. **Coverage is part of the change.** New behavior, bug fixes, and refactors
   ship with tests in the same diff. If you cannot test it, the design is wrong
   or the seam is missing — add the seam, do not skip the test.
4. **Optimize for sharing, not for files.** A feature that three call sites
   need lives in one type or one registry. Copy-paste and “just this once”
   helpers are debt.
5. **Pleasure to read.** Prefer a small number of honest types with clear
   methods over a thicket of functions that pass the same bag of state around.

The package is small on purpose. Elegance here is **fewer concepts, stronger
ones** — not more layers.

---

## Setup and tests

Python 3.10+. Use the package venv, not Apple's system Python:

```bash
bash lmloop/setup-lmloop.sh
cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Setup reuses an existing venv when its Python is new enough; it only deletes the
tree if the interpreter is missing or older than 3.10.

**Dependencies.** Do not add a package unless the stdlib (or an existing extra:
`prompt_toolkit`, `rich`, `ddgs`) cannot do the job. The agent loop stays on
`urllib` — no OpenAI SDK, no HTTP client framework.

Use stdlib **when it is the reliable primitive** (`pathlib`, `shlex`,
`html.parser`, `json`, `argparse`, `dataclasses`, `unittest`, `urllib`). Do not
import stdlib modules as decoration, and do not reach for an obscure corner of
the library when a short explicit loop is clearer. Third-party code is a last
resort; clever stdlib is the second-to-last.

---

## Architecture for feature sharing

lmloop shares behavior through **registries, owning types, and a one-way import
graph** — not through a framework, DI container, or parallel copies of the same
list.

### Single sources of truth

| Concern | Owner | Rule |
|---|---|---|
| Tool schema + validation + impl | `ToolDef` rows in `tools.build_tools()` | `tool_names()` reads that registry. Never a second name list. |
| Slash / CLI stems, reserved skill names | `CommandMeta` rows in `commands.py` | Handlers stay in `cli.py` / `repl.py`. Names live here only. |
| Status / resume / nudge copy | `status.py` | Do not hard-code user-facing loop copy in `agent.py`. |
| Session mutable state | `SessionState` | Pass the object; do not thread the same fields as loose args. |
| TTY chrome | `ui.Console` / `Theme` | Display printers write; they do not own theming. |
| Token command view | `tools.ShellCommand` | Argv + flags, not a regex on the raw line. |

When you add a tool, command, or message, **extend the owning type**. If you
find yourself updating two lists, you have already added debt.

### Where code goes

| Change | Module |
|---|---|
| Multi-round tool loop, HTTP chat, skills, server bring-up | `agent.py` |
| SSE ingest, cumulative snapshots, overlap, repeat-halt | `stream.py` |
| Spinner, live printers, thinking lines, TTY writes | `display.py` |
| Tool schema + impl + validation | `tools.py` (`ToolDef` rows) |
| Slash/CLI names, reserved skill stems | `commands.py` |
| Status / resume / nudge copy | `status.py` |
| JSONL memory | `memory.py` |
| Goal loop (`until` maker/check/eval) | `loop.py` |
| Authored workflow graphs | `graph.py` |
| Always-on steering markdown + live clock | `steer.py` |
| argparse routing, non-REPL subcommands | `cli.py` |
| Paths, `DEFAULTS`, project slug | `config.py` |
| `@path` completion + ref expansion | `files_index.py` |
| REPL session + slash handlers | `repl.py` |
| prompt_toolkit session, completers, key bindings | `prompt.py` |
| ANSI chrome, status bar, `drain_tty_input` | `ui.py` |
| rich Console / markdown / pager | `markdown_view.py` |

`agent.py` imports display/stream helpers that `act()` calls. Tests patch
`lmloop.display` / `lmloop.stream` where those names are looked up — do not
re-export privates through `agent` for tests. New stream or display logic
belongs in those modules, not as more private helpers in `agent.py`.

**Import graph (do not invert):**

- `stream.py` must not import `agent` or `display`.
- `display.py` may import stream helpers (they share `_think_line_similar`) but
  not `agent`.
- `ui.py` must not import `agent`.
- `commands.py` and `status.py` stay leaf modules: names and copy, no loop.
- `loop.py` may import `agent.act`, `memory`, `status`, and `tools.run_shell` for the check command.
- `graph.py` may import `loop` (`isolated_act`, `run_until`, `parse_eval_status`, `run_check`), `agent` (skills, `act` only through `isolated_act`), `memory`, `status`.
- `steer.py` is a leaf: pathlib, datetime, `STATE_ROOT`. It must not import `agent`, `tools`, `loop`, or `graph`.
- `agent.py` and `tools.py` may import `steer`.
- `loop.py` must not import `graph`.
- `agent.py` / `stream.py` / `display.py` must not import `loop` or `graph`.

### Keep components small

- One registry: add tools as `ToolDef` rows in `build_tools()`. `tool_names()`
  reads that registry — do not keep a parallel name list.
- HTTP in `tools.py` returns `(info, err)` / `(body, err)` so backends do not
  each wrap urllib. Search backends are a list of `(name, fn)` in `web_search()`.
- Display close methods (`clear`, `finish`, `erase`, `reset`) must not raise.
  Catch `OSError` at the write site (`_tty_write`). `act()` then does not need
  `_safe_*` wrappers.
- `echo_tool` is `(name, preview, stats)`. `on_thinking(text)` stores prior-turn
  thinking for Ctrl+O — no unused `kind` argument.
- Thinking-repeat has two layers on purpose: `stream.py` stops ingesting a loop
  (`_looping_text`: char-window, identical lines, paraphrases, and multi-line
  plan blocks); `_ThinkingLive` skips painting near-duplicate lines that still
  arrived. Share `_think_line_similar`; do not copy the predicate. After halt,
  `_read_sse` drains a short window for a late tool/answer, then closes — there
  is no `max_tokens`, so a thinking loop can otherwise fill the context window.

A new collaborator is justified when it **owns state or a contract** that more
than one caller needs. A new module is justified when a boundary already exists
in the table above. Do not invent a layer to sit between two existing ones.

---

## OOP: pillars, applied here

Use objects where they earn their keep. This is not “make everything a class”
and not “never abstract.” It is the four pillars, used so features compose.

### Encapsulation

State and the operations that preserve it live together.

- `SessionState` owns the live thread, stats, and thinking history. Methods
  like `push_thinking()` enforce bounds; callers do not mutate the list ad hoc.
- `ToolDef` owns schema, required fields, and impl. `dispatch()` validates
  through that type.
- `Console` / `Theme` own color policy. Printers do not sprinkle ANSI.
- Display printers own buffer + TTY writes. Close methods never raise.

Private attributes (`_write`, `_started`) are for invariants, not secrecy. If
two modules need the same field, the type is in the wrong module — move it,
do not poke `_`.

### Abstraction

Expose a small, stable surface; hide the mechanism.

Good abstractions in this tree: `ToolDef.openai_spec()`, `ShellCommand` as a
token view, `dispatch()` turning impl failures into `ERROR: …` text, HTTP
helpers returning `(body, err)`.

Bad abstractions: a second callback signature “for compatibility”; a wrapper
that only catches `Exception`; an interface with one implementation; a
config object that is a disguised `dict` plus three unused fields.

Name the contract after **what callers need** (`finish()`, `feed()`,
`openai_spec()`), not after the current backend (`RichLiveTailV2`).

### Inheritance

Rare. Use it for a real **is-a** with shared parsing or protocol, not for
code reuse.

- **Yes:** `_HtmlToolParser(HTMLParser)` and its subclasses — the stdlib
  *is* the HTML API; subclasses specialize `handle_starttag` / data.
- **No:** a `BaseAgent` / `BaseTool` / `AbstractPrinter` hierarchy for one
  concrete loop. Share a method-name contract (`feed` / `finish` / `reset`)
  or a small helper function instead.
- **No:** inheriting to override one flag. Use composition (`ToolDef` holds
  `impl`; `SlashCommand` holds `handler`).

Prefer composition: `SessionState` has a `Console`; `CommandMeta` does not
subclass into CLI vs slash — it has `cli` / `slash` flags.

### Polymorphism

Same operation, different types, no `if kind ==` ladders at the call site.

- Live printers (`_StreamPrinter`, `_MarkdownLivePrinter`, `_ThinkingLive`)
  share `feed` / `finish` / `reset` (and `clear` / `erase` where they write).
  `act()` should not branch on printer class.
- Search backends are `(name, fn)` callables with the same `(query, …) ->
  (body, err)` shape. Add a backend by appending to the list.
- Slash handlers are `Callable[[SessionState, str], bool]`. The dispatcher
  does not know about `/memory` vs `/stats`.

If you need `isinstance` to pick behavior, the abstraction is leaking. Put
the behavior on the type.

### Design that stays readable

- **Dataclasses** for records and registries (`ToolDef`, `CommandMeta`,
  `SlashCommand`, `SessionState`). Frozen when the row is a constant
  (`CommandMeta`).
- **Plain functions** for stateless transforms (`format_tokens`,
  `context_block`, `expand_at_refs`, `load_steering`, `clock_block`). A class
  with one method and no state should be a function.
- **Factories** only at boundaries (`build_tools`, `make_console`,
  `make_confirm_gate`, `_open_live_display`). They assemble; they do not
  become god objects.
- **No new framework.** No plugin systems, metaclasses, or dynamic import
  of “every module in a folder” unless the existing skill/markdown discovery
  pattern already does that job.

---

## Python naming and conventions

Follow [PEP 8](https://peps.python.org/pep-0008/) and [PEP 257](https://peps.python.org/pep-0257/). This repo’s dialect:

| Kind | Convention | Examples |
|---|---|---|
| Modules | `snake_case`, noun or role | `markdown_view.py`, `files_index.py` |
| Packages | `lmloop` | never `LmLoop` |
| Classes | `CapWords` | `ToolDef`, `SessionState`, `ServerError` |
| Exceptions | `CapWords` ending in `Error` | `StreamError`, `ServerError` |
| Functions / methods | `snake_case`, verb or query | `build_tools()`, `list_skills()`, `is_destructive()` |
| Instance attrs | `snake_case` | `session_log`, `thinking_history` |
| Module constants | `UPPER_SNAKE` | `MAX_OUTPUT`, `COMMANDS`, `WEB_HEADERS` |
| Module-private | leading `_` | `_tty_write`, `_TOOL_DEFS`, `_HtmlToolParser` |
| Type aliases / records | `CapWords` dataclass | `CommandMeta` |
| Boolean helpers | `is_` / `has_` / `needs_` | `needs_shell`, `is_nudge_message` |
| CLI handlers | `cmd_<stem>` | `cmd_memory_mine`, `cmd_skills_new` |
| REPL handlers | `_cmd_<stem>` | `_cmd_restore` |
| Tests | `test_<behavior>` on `*Tests(unittest.TestCase)` | `test_list_skills_excludes_system` |

Further rules:

- **Python 3.10+.** Use `X | Y` unions (or quoted `"list[dict] | None"` when
  needed). Do not add `from __future__ import annotations` unless a cycle
  forces it.
- **Type hints on public functions and class methods.** Skip them on trivial
  one-line locals. Prefer concrete types over `Any`. `cfg: dict` is the
  established config bag — do not invent a Pydantic model for it.
- **Docstrings** on public types and non-obvious functions: one line stating
  the contract, not a restatement of the name. Module docstrings say what
  the file owns. Keep them in sync when the contract changes.
- **f-strings** for interpolation; `%` / `.format` only when an existing
  constant template requires it (`_TRUNCATE_HINT`).
- **No wildcard imports.** Explicit names.
- **Quotes:** match the file. Double-quoted strings dominate; keep a file
  internally consistent.
- **Line length:** wrap for readability around 88–100; do not fight a
  slightly long call if breaking it is worse.
- **Imports:** stdlib, then third-party, then `from . import …` relative
  package imports. No unused imports.

Names should say **what is true after the call** (`finish()` returns whether
anything was visible; `resolve_checkpoint` returns `None` on ambiguity). Avoid
`handle_data2`, `tmp`, `stuff`, `process`.

---

## Parsing: stdlib over brittle glue

Regex is a last resort. It is the usual way this codebase would rot.

| Job | Use | Not |
|---|---|---|
| Shell tokens, flags, pipes | `shlex.split` + `ShellCommand` | `re` on the command line |
| HTML / search pages | `html.parser.HTMLParser` subclasses | regex over tags |
| JSON (SSE lines, tool args, config) | `json.loads` / `JSONDecodeError` | scanning with `re` |
| Paths, workspace scope | `pathlib.Path` | string prefix checks that miss `..` |
| CLI | `argparse` | handmade `sys.argv` parsers |
| Structured records | `dataclasses` | parallel tuples and index magic |
| Tests | `unittest` + `unittest.mock` | a second test runner |

**Regex is allowed only** when the language is regular and there is no parser:

- charset validators (`SKILL_NAME_RE`)
- ANSI CSI strip (`strip_ansi`)
- slug / key sanitizing (`re.sub` of illegal filename chars)
- simple `@path` tokens in user text (`AT_REF_RE`)

Those patterns live as **named compiled constants** next to the function that
owns them, with a test for the boundary cases. Do not inline a new `re.search`
in a hot path to “quickly” parse HTML, shell, or JSON.

If a regex is getting flags, lookbehind piles, or is used to extract structure,
stop and use a parser.

---

## Exceptions: keep them at boundaries

Catch a specific type, or do not catch.

**Keep**

- `ImportError` for optional deps (`rich`, `prompt_toolkit`, `ddgs`)
- urllib `HTTPError` / `URLError` / `OSError` at HTTP edges
- `json.JSONDecodeError` on SSE lines and tool arguments
- `FileNotFoundError` for missing skills
- `KeyboardInterrupt` in the REPL
- `subprocess.TimeoutExpired`, `shlex` `ValueError`
- `dispatch()` turning impl failures into `ERROR: …` text (contract: the loop
  does not crash)
- `except Exception` in `_search_ddgs` only — that library’s errors are not a
  stable API

**Avoid**

- Bare `except Exception: pass` around HTML parsers, Live.stop, or SSE merge.
  `_merge_tool_call_delta` already skips bad chunks.
- Wrapping unexpected bugs as `ServerError`. Rollback incomplete messages, then
  re-raise the original.
- `TypeError` shims for two callback signatures. Pick one.
- Catching at the center of the loop to “be safe.” Safety belongs in
  `dispatch()`, HTTP helpers, and TTY write sites.

`act()` rolls back incomplete tool rounds on `KeyboardInterrupt` and on any
other exception, then re-raises. Do not convert programming errors into
user-facing `ServerError`.

---

## Tests and coverage

Tests live in `lmloop/tests/`, stdlib `unittest`, named `test_<area>.py`.
Run the full suite before you call a change done.

**Required with every behavior change**

- Happy path for the new contract.
- At least one failure / edge (missing file, bad JSON, ambiguous restore
  query, destructive shell, empty stream, interrupt rollback).
- A regression test that would have failed before the fix.

**How to write them**

- Patch the module where the name is looked up. `_open_live_display` reads
  `_rich_live_available` from `lmloop.display`, so patch
  `lmloop.display._rich_live_available`, not
  `lmloop.agent._rich_live_available`.
- Keep tests off `prompt_toolkit` unless they exercise the prompt. TTY helpers
  live in `ui.py` so collapse tests do not import the REPL prompt stack.
- Prefer fakes (`FakeResp`, in-memory printers) over network. Web tests mock
  urllib / `ddgs`.
- Assert contracts, not implementation trivia. If a refactor of a private
  helper breaks a test, the test was glued to the wrong name.
- New types get tests next to their existing area (`test_tools.py` for
  `ShellCommand` / `ToolDef`, `test_cli.py` for routing, `test_agent_stream.py`
  for the loop and SSE). Do not start a kitchen-sink file.

**Coverage is active, not aspirational.** If you touch `act()`, printers,
tools, memory, or slash routing, add or extend tests in that file. Untested
branches you notice while editing are in scope — cover or delete them.

Do not skip tests to land a feature. Do not comment out assertions. Do not
add `time.sleep` to wait for IO; fake the IO.

---

## Docs stay in lockstep

Docs are part of the diff, not a follow-up.

| Audience | File | Update when |
|---|---|---|
| Users | `README.md` | Flags, slash commands, tools, setup, troubleshooting |
| Internals | `docs/ARCHITECTURE.md` | Component map, loop, memory layout, design choices |
| Next-step proposals | `docs/DESIGN_*.md` | Shipped files keep `Status: shipped`. Leftover ideas stay in Non-goals / Open questions. |
| Contributors | `DEVELOPMENT.md` | Module ownership, contracts, conventions |
| Skills | `lmloop/skills/*.md` | Opt-in playbooks the model follows when that job is named |
| Steering | `lmloop/steer/*.md` | Always-on iron laws injected into the system prompt |

Rules:

- User-facing behavior goes in `README.md`. Internals go in
  `docs/ARCHITECTURE.md`. Keep the tools table as a table — do not insert
  prose between rows.
- The component map in `ARCHITECTURE.md` must match files that exist. Adding
  a module means adding a row in the same change.
- Slash / CLI tables in `README.md` must match `commands.py`. If you add a
  `CommandMeta`, update the README table.
- Module docstrings and this file’s “where code goes” table stay aligned.
- Do not mention a second way to do something that you just removed (`--skill`
  vs `lmloop skill`).
- Shipped design docs keep a `Status: shipped` header. Leftover ideas stay in
  Non-goals / Open questions. Do not describe unimplemented graph/memory
  features as current behavior in `README.md` or `ARCHITECTURE.md`.
- Skills are opt-in playbooks (`/skill`, `/name`). Steering is always
  injected and has no slash command. Do not turn a steer file into a skill
  or the reverse.

If you are unsure whether a sentence is still true, check the code. Stale docs
are bugs.

---

## Tech debt: find it, remove it

When you are in a file, you own the obvious rot in the hunk you are changing.

**Remove on sight**

- Parallel lists of the same names (tools, commands, skills).
- Unused parameters and “for compatibility” callback shims.
- `_safe_close` / `_safe_write` wrappers when the callee already swallows
  `OSError`.
- Duplicated predicates (`_think_line_similar` exists so display and stream
  share it).
- `except Exception` that hides programming errors.
- Regex that should be `shlex`, `HTMLParser`, `json`, or `Path`.
- Imports of `agent` from `stream` or `display`.
- Dead re-exports that nothing imports (except names tests already patch —
  move the test patch target rather than keep a zombie).

**Do not add**

- A compatibility facade so old and new APIs both work “for a while.” Pick
  the new contract, update call sites and tests, delete the old one.
- Config keys with no reader, or readers with no README line.
- Comments that narrate what the next line does. Comments explain *why* the
  non-obvious constraint exists (e.g. no `max_tokens` on thinking halt).

If a cleanup is too large for the current diff, extract it only if it blocks
correctness. Otherwise do the small cleanup in-place; do not leave a breadcrumb
for a fictional later PR.

---

## Diff hygiene

- Match the surrounding style: stdlib, small functions, JSONL on disk, types
  that earn their keep. No new abstraction layers for a single call site.
- Prefer a focused change (search fallbacks, *or* thinking UI, *or* stream
  overlap) over one diff that mixes all three.
- Public contracts change in one step: code, tests, README / ARCHITECTURE.
- Do not reformat unrelated files. Do not drive-by rename a module without
  updating imports, tests, and the architecture map.

A change is done when: the suite is green, the new behavior is tested, debt
in the touched code is gone or honestly reduced, and the docs still describe
the program.
