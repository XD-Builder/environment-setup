# Contributing to lmloop

How to change this package without turning `agent.py` back into a catch-all.
Architecture (what the pieces are) lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). This file is the how.

## Setup and tests

Python 3.10+. Use the package venv, not Apple's system Python:

```bash
bash lmloop/setup-lmloop.sh
cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Setup reuses an existing venv when its Python is new enough; it only deletes the tree if the interpreter is missing or older than 3.10.

Do not add dependencies unless the stdlib (or an existing extra: `prompt_toolkit`, `rich`, `ddgs`) cannot do the job. The agent loop stays on `urllib` — no OpenAI SDK.

## Where code goes

| Change | Module |
|---|---|
| Multi-round tool loop, HTTP chat, skills, server bring-up | `agent.py` |
| SSE ingest, cumulative snapshots, overlap, repeat-halt | `stream.py` |
| Spinner, live printers, thinking lines, TTY writes | `display.py` |
| Tool schema + impl + validation | `tools.py` (`ToolDef` rows) |
| Slash/CLI names, reserved skill stems | `commands.py` |
| Status / resume / nudge copy | `status.py` |
| JSONL memory | `memory.py` |
| REPL session + slash handlers | `repl.py` |
| prompt_toolkit session, completers, key bindings | `prompt.py` |
| ANSI chrome, status bar, `drain_tty_input` | `ui.py` |
| rich Console / markdown / pager | `markdown_view.py` |

`agent.py` may import display/stream helpers and re-export names tests already use. New stream or display logic belongs in those modules, not as more private helpers in `agent.py`.

`stream.py` must not import `agent` or `display`. `display.py` may import stream helpers (they share `_think_line_similar`) but not `agent`.

## Keep components small

- One registry: add tools as `ToolDef` rows in `build_tools()`. `tool_names()` reads that registry — do not keep a parallel name list.
- HTTP in `tools.py` returns `(info, err)` / `(body, err)` so backends do not each wrap urllib. Search backends are a list of `(name, fn)` in `web_search()`.
- Display close methods (`clear`, `finish`, `erase`, `reset`) must not raise. Catch `OSError` at the write site (`_tty_write`). `act()` then does not need `_safe_*` wrappers.
- `echo_tool` is `(name, preview, stats)`. `on_thinking(text)` stores prior-turn thinking for Ctrl+O — no unused `kind` argument.
- Thinking-repeat has two layers on purpose: `stream.py` stops ingesting a loop; `_ThinkingLive` skips painting near-duplicate lines that still arrived. Share `_think_line_similar`; do not copy the predicate.

## Exceptions: keep them at boundaries

Catch a specific type, or do not catch.

**Keep**

- `ImportError` for optional deps (`rich`, `prompt_toolkit`, `ddgs`)
- urllib `HTTPError` / `URLError` / `OSError` at HTTP edges
- `json.JSONDecodeError` on SSE lines and tool arguments
- `FileNotFoundError` for missing skills
- `KeyboardInterrupt` in the REPL
- `subprocess.TimeoutExpired`, `shlex` `ValueError`
- `dispatch()` turning impl failures into `ERROR: …` text (contract: the loop does not crash)
- `except Exception` in `_search_ddgs` only — that library’s errors are not a stable API

**Avoid**

- Bare `except Exception: pass` around HTML parsers, Live.stop, or SSE merge. `_merge_tool_call_delta` already skips bad chunks.
- Wrapping unexpected bugs as `ServerError`. Rollback incomplete messages, then re-raise the original.
- `TypeError` shims for two callback signatures. Pick one.

`act()` rolls back incomplete tool rounds on `KeyboardInterrupt` and on any other exception, then re-raises. Do not convert programming errors into user-facing `ServerError`.

## Tests

- Patch the module where the name is looked up. `_open_live_display` reads `_rich_live_available` from `lmloop.display`, so patch `lmloop.display._rich_live_available`, not `lmloop.agent._rich_live_available`.
- Keep tests off `prompt_toolkit` unless they exercise the prompt. TTY helpers live in `ui.py` so collapse tests do not import the REPL prompt stack.
- Prefer fakes (`FakeResp`, in-memory printers) over network. Web tests mock urllib / `ddgs`.

## Docs and diffs

- User-facing behavior: `README.md`. Internals: `docs/ARCHITECTURE.md`. Keep the tools table as a table — do not insert prose between rows.
- Match the surrounding style: stdlib, small functions, JSONL on disk, no new abstraction layers.
- Prefer a focused change (search fallbacks, or thinking UI, or stream overlap) over one diff that mixes all three.
