# Design: LLM calling for lmloop

**Status:** shipped
**Date:** 2026-09-04
**Depends on:** `chat.py`, `agent.py` (`act`), `loop.py` (`until`), DEVELOPMENT.md import graph

This is the **completions calling** spec: which HTTP goes to the model, how one
`act()` turn gather/answers, and how outer loops budget those turns. Control-flow
graphs are [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md). Knowledge-graph
memory is [DESIGN_LOOP_AND_GRAPH.md](DESIGN_LOOP_AND_GRAPH.md). Internals overview:
[ARCHITECTURE.md](ARCHITECTURE.md).

Do not add an LLM router, LangGraph, or the OpenAI SDK.

---

## What shipped

| Piece | Where |
|-------|--------|
| Completions HTTP | [`chat.py`](../lmloop/chat.py) — `POST {base_url}/chat/completions` |
| Gather/answer loop | [`agent.py`](../lmloop/agent.py) `act()` |
| One-shot draft (tools off) | `agent.generate_skill_draft` → `_chat` |
| Tool mode | `act(..., no_tools=True)` omits `tools` + `tool_choice`; `/save` uses it |
| `tool_choice` | `"auto"` when tools are present; omitted on answer / `no_tools` |
| Eval budget | `eval_max_rounds` (default 8) on until/graph eval `act()` |
| Frozen clock | one `clock_now` per REPL session and per until/graph run |
| Parallel reads | `ToolDef.concurrent` + `tools.run_tool_calls()` |

LMS metadata (`GET /v1/models`, native `/api/v0/models`) is not completions.
Whisper CLI and `--check` are not model calls.

---

## One `act()` (inner loop)

```
gather (tools on, up to max_rounds or eval_max_rounds):
    msg, usage = _chat(..., tools + tool_choice=auto)
    if no tool_calls:
        return                          # natural final reply
    if this tool set was already run this turn:
        go to answer                    # exact name+args
    run tools (concurrent reads, serial writes); append results
answer (tools omitted, one call):
    msg, usage = _chat(..., tools omitted)
    return
```

Payload: `model`, `messages` (`status.api_messages` strips `_lmloop` keys),
`temperature`, `stream`, optional `stream_options.include_usage`, optional
`tools` + `tool_choice: "auto"`. No `max_tokens`.

---

## Outer turn types

Each isolated job is `loop.isolated_act` (fresh system prompt + session log).
The live REPL reuses `state.messages`.

| Surface | `act()` count | Tools |
|---------|---------------|--------|
| REPL freeform / `/skill` / `/continue` text | 1 live | full |
| until maker | 1 per cycle | full |
| until eval | 1 per cycle | readonly, `eval_max_rounds` |
| until `--check` | 0 | shell |
| graph skill node | 1 + eval (or `--check`) | full then readonly |
| graph until node | nested `run_until` | same as until (no mine inside) |
| `/memory mine`, until/graph mine | 1 isolated | full (`retro.md`) |
| `/compact` | 1 isolated | full (may `read_file`) |
| `/save` | 1 isolated | `no_tools` |
| `/memory reconcile` | 1 isolated | full |
| `skills new` | 1 `_chat`, not `act()` | omitted |

---

## Clock freeze

`system_prompt(..., clock_now=)` injects `steer.clock_block(now=clock_now)`.
Callers pass one datetime per REPL session (`SessionState.clock_now`) and per
until/graph run. `current_time` remains the refresh tool when a session is hours old.

---

## Non-goals

- Mid-`act()` auto-compact
- `tool_choice: "required"`
- `max_tokens` on every call (halt-drain in `stream.py` stays the thinking-loop stop)
- JSON-mode eval `STATUS:`
- Parallel graph workers / LLM router over nodes
