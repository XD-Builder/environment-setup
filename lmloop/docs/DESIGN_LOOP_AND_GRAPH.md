# Design: Knowledge Graph Memory for lmloop

**Status:** shipped (knowledge-graph memory, opt-in `use_graph`) — control-flow graphs are in [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md)
**Updated:** 2026-08-14
**Original date:** 2025-07-13
**References:** [Bian et al., "LLM-empowered knowledge graph construction: A survey" (arXiv 2510.20345v1)](https://arxiv.org/html/2510.20345v1)

This file is **only** a knowledge-graph memory design (nodes/edges over learnings, decisions, sessions). It is not a spec for control-flow.

## What shipped (August 2026)

Control-flow **goal loop** and **workflow graphs** are implemented. This file is the knowledge-graph memory layer, now opt-in via `use_graph`.

Shipped here (names resolved against `/graph` the control-flow stem):

| Piece | Where |
|-------|--------|
| Storage | `graph_nodes.jsonl`, `graph_edges.jsonl` under the project dir |
| Recall | `recall_memory` does keyword + 1-hop neighbors when `use_graph` |
| Writes | `graph_add_edge` tool (registered only when `use_graph`) |
| Slash | `/memory graph`, `/memory reconcile` — not `/graph *` |
| Auto edges | `in_session` / `references` from `remember` and `log_decision`; `uses_skill` from `/skill` |
| Decay | Learning/concept nodes with effective confidence ≤ 0 are hidden at query time, with their edges |

`use_graph` defaults **false**. No graph files are created until it is enabled.

| Piece | Where |
|-------|--------|
| `lmloop until` / `/until` | [`loop.py`](../lmloop/loop.py) — isolated maker `act()` until `--check` exit 0 or an isolated eval `STATUS:` line |
| Resume | `/continue` (REPL) or `lmloop until` with no goal (CLI) |
| Mine after pass | existing `retro.md` playbook via `memory mine` (`until_mine`, default true) |
| Command fold | `lmloop retro` / `/retro` are aliases that print `[memory mine]` |

Eval is a **fresh thread**. Missing `STATUS:` is `blocked`, never `pass`. Run logs: `~/.lmloop/projects/<slug>/until/<ts>.jsonl`.

## What to implement next (not this file)

Control-flow graphs are shipped in [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md). Remaining non-goals: embeddings, graph DB, `/graph pending` queues, `graph_archive.jsonl`, ASCII overview art.

This file must not be copied into README as if graph memory is on by default — it is opt-in (`use_graph`).

---

## Problem

lmloop's current memory is **flat**: learnings and decisions live in independent JSONL files with keyword search. The agent cannot reason about *relationships* between pieces of knowledge — why a learning was created, which decision led to which pitfall, which sessions explored a given topic.

This limits three things:
1. **Context quality.** `recall_memory("build fails")` returns matching learnings but cannot surface the decision that caused the build issue or the session where it was first encountered.
2. **Self-correction.** When a learning turns out wrong, there's no trail to update dependent learnings or retract derived decisions.
3. **Project understanding.** New sessions start with top-N learnings by confidence score, losing the narrative of *how* the project evolved.

The survey paper (Bian et al.) identifies this exact gap in Section 6.2: **dynamic knowledge memory for agentic systems** — moving from flat vector stores to structured, self-evolving knowledge graphs.

---

## Goal

Add a **lightweight knowledge graph layer** to lmloop's existing memory system. The graph connects entities the agent already produces (learnings, decisions, sessions, files, skills) and enables the agent to reason about their relationships during its loop.

Constraints:
- **Zero new dependencies.** Graph stored as JSONL, computed at read time (matching existing memory philosophy).
- **Backward compatible.** Existing `learnings.jsonl` and `decisions.jsonl` files work unchanged.
- **Local-model friendly.** Graph queries must be cheap — keyword + edge traversal, not full graph embeddings.
- **Opt-in.** Enabled by config flag; default false.

---

## Design

### 3.1 The Graph Schema

The graph has two table types: **nodes** and **edges**, both append-only JSONL.

```
~/.lmloop/projects/<slug>/
    ├── learnings.jsonl          # existing, unchanged
    ├── decisions.jsonl          # existing, unchanged
    ├── graph_nodes.jsonl        # new: typed node records
    └── graph_edges.jsonl        # new: typed edge records
```

**Node types** (auto-created from existing memory + explicit):

| Type | Source | Fields |
|------|--------|--------|
| `learning` | Auto-synced from `learnings.jsonl` | key, insight, type, confidence |
| `decision` | Auto-synced from `decisions.jsonl` | id, decision, rationale |
| `session` | Auto-created from `sessions/` | ts, preview_text |
| `file` | Auto-created from path tokens in learning/decision **text** | path |
| `skill` | Auto-created when `/skill` or a graph skill node runs | name |
| `concept` | Explicit: `graph_add_edge` may create concept endpoints | name |

**Edge types:**

| Type | From → To | Meaning |
|------|-----------|---------|
| `leads_to` | decision → learning | This decision produced this learning |
| `contradicts` | learning → learning | These learnings conflict |
| `in_session` | learning/decision → session | Created during this session |
| `references` | learning/decision → file | Mentions this file path |
| `uses_skill` | session → skill | This skill was invoked in the session |
| `related_to` | any → any | General semantic relationship |
| `supersedes` | decision → decision | Already in decisions.jsonl, mirrored in graph |

### 3.2 Graph Construction Pipeline

**Shipped** (only when `use_graph` is true):

**Layer 1: Ontology Engineering** — the node/edge type definitions above. Fixed schema, no LLM needed.

**Layer 2: Knowledge Extraction** — automatic edge creation during normal operation:
- When `remember()` is called, create a `learning` node + `in_session` edge to current session
- When `log_decision()` is called, create a `decision` node + `in_session` edge
- `references` edges when file paths appear in learning/decision **text** (not from file tools)
- `uses_skill` edges when `/skill` or a graph skill node actually runs

**Layer 3: Knowledge Fusion** — LLM-driven edge creation:
- `graph_add_edge` lets the agent propose edges between existing nodes (required `note`)
- `/memory mine` appends a graph phase (`_graph_mine.md`) after extracting learnings
- `/memory reconcile` reviews `contradicts` clusters

**Not shipped:** file-tool auto-nodes, a separate `graph_reason` / `graph_query` tool, `/graph *` slash commands (those stems are control-flow).

### 3.3 The Enhanced Agent Loop

When `use_graph` is on, `remember` / `log_decision` / skill use update `graph_nodes.jsonl` and `graph_edges.jsonl`. `recall_memory` stays the retrieval tool and attaches **1-hop neighbors**. There is no `graph_query` tool and no multi-hop BFS.

**`graph_add_edge(from_type, from_key, to_type, to_key, edge_type, note)`**
- Agent proposes a relationship between two existing nodes (concept endpoints may be created).
- Appends to `graph_edges.jsonl`.
- Requires a `note` field explaining the rationale (audit trail).

### 3.4 Context Injection with Graph Awareness

When graph is enabled, `context_block()` changes:

**Before:**
```
Active decisions:
- [abc123] Use pytest for testing

Prior learnings:
- (pitfall, 7/10) setup.sh fails on Linux because of line endings
```

**After:**
```
Active decisions:
- [abc123] Use pytest for testing
  → led to: pytest-config-in-src (learning)
  ← from: 20250710-143000 (session)

Prior learnings (with context):
- pytest-config-in-src (pitfall, 7/10) setup.sh fails on Linux because of line endings
  ← from: 20250710-143000 (session)
  → used by: deploy-pipeline (decision)
  ~ references: setup.sh, bin/deploy
```

The agent sees not just the fact, but its provenance and connections.

---

## Implementation Plan

Phases 1–4 below are **history**. What shipped is the August 2026 table at the top of this file.

**Shipped:** JSONL storage, backfill, `use_graph` default false, auto `in_session` / `references` / `supersedes` / `uses_skill` (gated on `use_graph`), `graph_add_edge`, mine graph phase, `context_block` 1-hop neighbors, query-time decay, `/memory graph`, `/memory reconcile`.

**Non-goals (do not treat as current):** `graph_query`, multi-hop BFS, file-tool auto-nodes, `/graph pending` / `/graph stats` / `/graph overview` / `/graph reconcile`, `graph_archive.jsonl`, embeddings.

---

## What This Enables

1. **Tracing.** "Why do we use pytest?" → graph shows the decision, the learning that led to it, the session where it was decided, and the files it touches.

2. **Contradiction detection.** When two learnings have `contradicts` edges, the agent surfaces both and asks the user to resolve — instead of silently keeping the latest.

3. **Session-aware recall.** "We hit a build issue before" → graph returns the session where it happened, not just the distilled learning. The agent can re-read the full context.

4. **Skill discovery.** "What skills have I used for debugging?" → graph shows `uses_skill` edges from investigation sessions.

5. **Project onboarding.** `/memory graph` prints counts and an adjacency list (not ASCII art).

---

## Alignment with the Survey

| Survey Section | lmloop Mapping |
|----------------|----------------|
| §3.1 Top-down ontology (LLM as assistant) | Agent proposes new node/edge types via `graph_add_edge` |
| §3.2 Bottom-up schema (KGs for LLMs) | Auto-extraction from sessions creates the schema from usage |
| §4.1 Schema-based extraction | Fixed node/edge types with structured fields |
| §4.2 Schema-free extraction | Agent can create `concept` nodes for emergent topics |
| §5.1 Schema-level fusion | Merging duplicate nodes during `/memory reconcile` |
| §5.2 Instance-level fusion | Resolving `contradicts` edges |
| §6.1 KG-based reasoning for LLMs | `recall_memory` with 1-hop neighbors (no `graph_query`) |
| §6.2 Dynamic knowledge memory | The entire graph layer is a dynamic, self-evolving memory |

The key insight from the survey (§1, §6.4): KGs in LLM applications go **beyond RAG**. They provide structured reasoning about *how knowledge was acquired and how it relates*. lmloop's graph layer turns the agent's memory from a flat list into a navigable knowledge structure.

---

## Open Questions

1. **Edge quality control.** Should edges require user confirmation? Auto-edges (`in_session`, `references`) are implicit; a `/graph pending` review queue is a non-goal.

2. **Graph size limits.** Decay filters learning/concept nodes at query time. An archive file (`graph_archive.jsonl`) is a non-goal.

3. **Embedding support.** Not shipped. Keyword + 1-hop is the local-project default. A `graph_embed` tool would be optional later.

4. **Visualization.** `/memory graph` is an adjacency list. Rich ASCII graph rendering is a nice-to-have, not shipped.

---

## Non-Goals

- Multi-project graph federation (lmloop is single-user, single-project)
- Graph database backends (SQLite, Neo4j) — JSONL matches our file-only philosophy
- Real-time graph streaming — all updates are append-only, computed at read time
- Replacing the existing memory system — graph is an additive layer
