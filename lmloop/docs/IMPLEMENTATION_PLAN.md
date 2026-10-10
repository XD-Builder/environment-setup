# Implementation plan (all `docs/` specs)

**Status:** living — update when a phase ships or is cut.  
**Date:** 2026-10-10  
**Index:** [DESIGN_ROADMAP.md](DESIGN_ROADMAP.md) (cuts, config budget, deferred triggers)  
**Archive:** [archived/](archived/) (fully shipped design docs)

This file orders **every** document under `docs/`: what to build next, what depends on what,
and what to move to `archived/` when done.

---

## Doc inventory

| Document | Role | Implementation status | Location when done |
|----------|------|------------------------|-------------------|
| [DESIGN_ROADMAP.md](DESIGN_ROADMAP.md) | Master cuts + steps 0–9 | Living index | Always `docs/` |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Shipped behavior | Living | Always `docs/` |
| [GUIDE_DOCKER_AND_OPENROUTER.md](GUIDE_DOCKER_AND_OPENROUTER.md) | User guide | Update with Phase S / company | Always `docs/` |
| [FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md](FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md) | Research input | Not a build track | `docs/` (reference) |
| [DESIGN_LLM_CALLING.md](archived/DESIGN_LLM_CALLING.md) | Spec | **Shipped** | `archived/` |
| [DESIGN_FILE_READING.md](archived/DESIGN_FILE_READING.md) | Spec | **Shipped** | `archived/` |
| [DESIGN_GRAPH_ENGINEERING.md](archived/DESIGN_GRAPH_ENGINEERING.md) | Spec | **Shipped** | `archived/` |
| [DESIGN_LOOP_AND_GRAPH.md](archived/DESIGN_LOOP_AND_GRAPH.md) | Spec | **Shipped** | `archived/` |
| [DESIGN_SANDBOX_AND_VERIFICATION.md](DESIGN_SANDBOX_AND_VERIFICATION.md) | Spec | Partial — **Phase S open** | Archive after S1–S10 |
| [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) | Spec | Partial — D minimal, E deferred | Archive when D1–D8 + open A* done |
| [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md) | Spec | Partial — W6, C2–C3 open | Archive when W6 + C2–C3 done |
| [DESIGN_COMMAND_CONSOLIDATION.md](DESIGN_COMMAND_CONSOLIDATION.md) | Spec | Partial — Phase C open | Archive after Phase C |
| [DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md) | Spec | Partial — V0 shipped | Archive after V1–V5 (or cut V5) |
| [DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) | Spec | Proposed (S11–S13, V5) | Merge into sandbox/evals or archive when implemented |
| [DESIGN_MULTI_AGENT_COMPANY.md](DESIGN_MULTI_AGENT_COMPANY.md) | Spec | Proposed | Archive when company mode ships |
| [DESIGN_LONG_HORIZON_PLANNING.md](DESIGN_LONG_HORIZON_PLANNING.md) | Spec | Proposed | Archive when campaigns ship |
| [DESIGN_EVOLVING_SPIRIT.md](DESIGN_EVOLVING_SPIRIT.md) | Spec | Proposed | Archive when spirit layer ships |

**Archival rule:** When a design doc’s **user-facing** behavior is in README + ARCHITECTURE and
remaining ideas live only in Non-goals, set `Status: shipped`, move the file to
`docs/archived/`, and add a one-line pointer in this table (per [DEVELOPMENT.md](../DEVELOPMENT.md)).

Partial docs **stay in `docs/`** until their last task row ships; then archive the whole file.

---

## Recommended build order

Follow dependency edges first; within a wave, prefer **latency-neutral host-path** work before
Docker and multi-process orchestration (see roadmap §2).

```mermaid
flowchart TD
  classDef done fill:#f0fdf4,stroke:#22c55e
  classDef wave1 fill:#eef6ff,stroke:#3b82f6
  classDef wave2 fill:#fef9c3,stroke:#ca8a04
  classDef wave3 fill:#fce7f3,stroke:#db2777
  classDef wave4 fill:#f5f5f5,stroke:#666
  classDef defer fill:#fff,stroke:#999,stroke-dasharray:5 3

  shipped["Steps 0–5, most 6–7, graph propose<br/>(roadmap)"]:::done
  mem["Memory finish: A5, D3–D8"]:::wave1
  dag["DAG: W6 KG run nodes"]:::wave1
  cmd["Command consolidation Phase C"]:::wave1
  evals["Usage evals V1–V4"]:::wave1
  sandbox["Sandbox Phase S S1–S10"]:::wave2
  canvas["Canvas TUI C2–C3"]:::wave2
  cont["Continual harness S11–S13 + evals V5"]:::wave2
  company["Multi-agent company"]:::wave3
  spirit["Evolving spirit"]:::wave3
  horizon["Long-horizon campaigns"]:::wave3
  emb["Memory Layer E embeddings"]:::defer
  par["Parallel local agents"]:::defer

  shipped --> mem
  shipped --> dag
  shipped --> cmd
  shipped --> evals
  mem --> canvas
  evals --> cont
  sandbox --> cont
  sandbox --> company
  dag --> company
  mem --> spirit
  company --> horizon
  mem -.-> emb
  dag -.-> par
```

### Wave 0 — Already shipped (archive only)

Roadmap steps **0–5**, **9**, and most of **6–7** on the **host** path. Four standalone
specs are in [archived/](archived/). No feature work unless regressions appear.

---

### Wave 1 — Host path completion (no Docker)

Do these in parallel where convenient; suggested **serial** order for one contributor:

| Order | Track | Tasks | Doc | Why here |
|-------|--------|-------|-----|----------|
| 1.1 | Memory index | **A5** parse cache; **D3–D8** incremental sync, query, wiring, `memory reindex`, check-inference candidates | [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) | **Shipped** |
| 1.2 | Flow / KG | **W6** run/goal nodes in knowledge graph | [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md) | **Shipped** (`record_workflow_run`) |
| 1.3 | UX vocabulary | **Phase C** — `mine_on_exit`, submit-line mine routes, `/memory audit` | [DESIGN_COMMAND_CONSOLIDATION.md](DESIGN_COMMAND_CONSOLIDATION.md) | **Shipped** |
| 1.4 | Self-improvement loop | **V1–V4** — join until logs, align `flow`, human-gated agent patches, locked regressions | [DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md) | **Shipped** (V5 still open) |

Optional in Wave 1: **A1** perf harness (skipped-by-default tests) when touching memory hot paths.

**Defer in Wave 1:** **Layer E** embeddings rerank — revisit when FTS5 misses real paraphrases
(roadmap deferred table).

---

### Wave 2 — Isolation + visibility

| Order | Track | Tasks | Doc | Depends on |
|-------|--------|-------|-----|------------|
| 2.1 | Docker sandbox | **S1–S10** (`DockerBackend`, flags, preflight, shadow volumes, docs) | [DESIGN_SANDBOX_AND_VERIFICATION.md](DESIGN_SANDBOX_AND_VERIFICATION.md) | Host checks/snapshots shipped; unlocks 24/7 and company |
| 2.2 | Canvas TUI | **C2–C3** after **C1** (shipped) | [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md) | **D6** in memory doc; full **D** index helps layout |
| 2.3 | Governance + co-evolution | **S11–S13**, **V5** Act/Abstain/Pair fixtures | [DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) | Phase S + V1–V4 |

Update [GUIDE_DOCKER_AND_OPENROUTER.md](GUIDE_DOCKER_AND_OPENROUTER.md) as **S2+** land (flags,
network table, troubleshooting).

---

### Wave 3 — Multi-agent and identity (remote + Docker)

| Order | Track | Doc | Depends on |
|-------|--------|-----|------------|
| 3.1 | Multi-agent company | [DESIGN_MULTI_AGENT_COMPANY.md](DESIGN_MULTI_AGENT_COMPANY.md) | **S1–S6**, DAG **D1–D3**, OpenRouter allowlist in guide |
| 3.2 | Evolving spirit | [DESIGN_EVOLVING_SPIRIT.md](DESIGN_EVOLVING_SPIRIT.md) | Stable memory + optional `use_graph`; best after **D** index and mine/retro paths |
| 3.3 | Long-horizon campaigns | [DESIGN_LONG_HORIZON_PLANNING.md](DESIGN_LONG_HORIZON_PLANNING.md) | Company orchestrator, **`--docker-persist`**, plan/board stores |

Use [FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md](FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md) when
designing **V3/V5** and spirit reflection — research, not a gating dependency.

---

### Explicitly deferred (do not schedule ahead of evidence)

| Item | Doc | Revisit when |
|------|-----|--------------|
| Parallel local agents | [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md), roadmap | `lmloop flow` shows remote frontier wait dominates |
| Per-cycle eval probe | Sandbox | Runs pass on checker-only then regress |
| Memory Layer E | [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) | FTS5 recall misses user paraphrases |
| Command Phase D items | [DESIGN_COMMAND_CONSOLIDATION.md](DESIGN_COMMAND_CONSOLIDATION.md) | Marked “not merged” — only if product asks |

---

## Per-doc “done” checklist

Use before moving a file to `archived/`:

1. All task tables for that doc are **shipped** or moved to Non-goals with a revisit trigger.
2. `README.md`, `ARCHITECTURE.md`, and `commands.py` / tool tables match behavior.
3. `Status: shipped` in the doc header.
4. Component map in ARCHITECTURE lists the module under `docs/archived/` with “(shipped)”.

---

## Maintenance

- After each wave, refresh [DESIGN_ROADMAP.md](DESIGN_ROADMAP.md) step table and this file’s
  mermaid (green = done).
- [DESIGN_ROADMAP.md](DESIGN_ROADMAP.md) remains the **authority on cuts** (10 config keys,
  no default parallel agents). This plan is the **authority on order** across all doc files.
