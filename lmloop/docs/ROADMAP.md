# Roadmap

**Status:** living — the one list of what is active, what is deferred, and why.
Update it in the same change that ships or cuts something.

Every design through the Claude Code parity work has shipped; those docs are in
[archived/](archived/README.md). New work starts as a design doc in `docs/`,
lands with README / ARCHITECTURE updates, and then moves to `archived/`
(rules in [DEVELOPMENT.md](../DEVELOPMENT.md) “Docs stay in lockstep”).

## Active docs

| Doc | Role |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Shipped behavior (internals) |
| [GUIDE_MCP.md](GUIDE_MCP.md) | User guide: MCP servers, sharing, policy |
| [GUIDE_DOCKER_AND_OPENROUTER.md](GUIDE_DOCKER_AND_OPENROUTER.md) | User guide: remote models, opt-in Docker sandbox, company mode |
| [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) | Partial: Layers A–D shipped; Layer E (embeddings rerank) deferred |
| [FINDINGS_HERMES_AGENT.md](FINDINGS_HERMES_AGENT.md) | Research input: event-driven learning (phases L0–L5). The next design candidate |
| [FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md](FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md) | Research input: harness patterns across open-source agents |
| [FINDINGS_CODE_INDEXING.md](FINDINGS_CODE_INDEXING.md) | Research input: workspace code indexing (symbol tools, optional LSP); code index not built |

## Next candidate

**Event-driven learning** from [FINDINGS_HERMES_AGENT.md](FINDINGS_HERMES_AGENT.md):
replace turn counters and calendar ticks (`until_mine`, `mine_on_exit`, the daily
campaign tick) with salience-triggered reviews at run and session boundaries,
measured by a repeat-mistake rate. Its L0 fix (mining on `eval_model`) is small
and independent. It needs a design doc before code.

## Deferred, with the evidence that would revive it

| Item | Revisit when | Record |
|---|---|---|
| Parallel local agents | `lmloop flow` shows, on a remote endpoint, frequent moments with ≥ 2 runnable frontier nodes and sequential waiting dominating wall-clock | [DAG design Part 5](archived/DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md) |
| Per-cycle eval probe | Runs finish on checker-only `pass` and later regress | [Sandbox design §2.9](archived/DESIGN_SANDBOX_AND_VERIFICATION.md) |
| Memory Layer E (embeddings rerank) | FTS5 recall misses paraphrases users search for | [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) |
| MCP OAuth sign-in | A server users need offers OAuth only | [Parity design](archived/DESIGN_CLAUDE_CODE_PARITY.md) |
| MCP legacy SSE transport | A needed server lacks streamable HTTP | same |
| Background MCP calls, `/tasks` | Server calls routinely exceed the 120 s timeout | same |
| `.cursor/rules` import | Users of that format ask for it (`AGENTS.md` / `CLAUDE.md` already load) | same |
| Unattended harness optimizer | Never by default; a human gates harness changes | [Continual harness design](archived/DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) |

## Standing constraints

- **Config budget.** A key exists only when users need different values and a
  wrong default costs them something; everything else is a named constant
  ([round-5 reasoning](archived/DESIGN_ROADMAP.md)).
- **Latency-neutral on a single-slot laptop.** A feature must not add a model
  call per cycle or defeat prompt-prefix reuse. MCP keeps two tool schemas by
  default and builds its catalog from cache for the same reason.
- **One agent at a time locally.** Parallelism is remote-only (company mode).
