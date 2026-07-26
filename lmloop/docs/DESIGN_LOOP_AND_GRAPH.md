# Design: Loop and Graph Engineering for lmloop

**Status:** proposal
**Date:** 2025-07-13
**References:** [Bian et al., "LLM-empowered knowledge graph construction: A survey" (arXiv 2510.20345v1)](https://arxiv.org/html/2510.20345v1)

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
- **Opt-in.** Enabled by config flag; disabled by default until v0.2.

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
| `file` | Auto-created when agent reads/writes a file | path, last_touched |
| `skill` | Auto-created from `skills/` | name, blurb |
| `concept` | Explicit: agent creates via `remember` with `graph` type | name, description |

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

Following the survey's three-layered KG pipeline (Section 2):

**Layer 1: Ontology Engineering** — the node/edge type definitions above. Fixed schema, no LLM needed. This is the "schema-based" paradigm from Section 4.1.

**Layer 2: Knowledge Extraction** — automatic edge creation during normal operation:
- When `remember()` is called, create a `learning` node + `in_session` edge to current session
- When `log_decision()` is called, create a `decision` node + `in_session` edge
- When a tool reads/writes a file, create/update a `file` node + `references` edge from current learning/decision
- When `/retro` runs, create `uses_skill` edge from session → retro skill

**Layer 3: Knowledge Fusion** — LLM-driven edge creation:
- A new `graph_reason` tool lets the agent propose edges between existing nodes
- `/retro` gains a graph phase: after extracting learnings, the agent proposes `leads_to`, `contradicts`, and `related_to` edges
- Periodic `/graph reconcile` resolves contradictions by asking the agent to review conflicting learnings

### 3.3 The Enhanced Agent Loop

Current loop:
```
user → [chat → tool_calls → run_tools] → reply
```

Enhanced loop with graph awareness:
```
user → [chat → tool_calls → run_tools] → reply
                        ↓
              graph_edges.jsonl (auto-updated)
                        ↓
              graph_nodes.jsonl (auto-updated)
```

The agent gains two new tools:

**`graph_query(node_type, keywords, max_hops)`**
- Returns nodes matching keywords, plus their connected neighbors up to `max_hops` away.
- Replaces `recall_memory` as the primary context retrieval tool when graph is enabled.
- Example: `graph_query("learning", "build fails", 2)` returns learnings about build failures, the decisions that led to them, and the sessions where they were created.

**`graph_add_edge(from_type, from_key, to_type, to_key, edge_type, note)`**
- Agent proposes a relationship between two existing nodes.
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

### Phase 1: Graph Storage (low risk, high value)

- Add `graph_nodes.jsonl` and `graph_edges.jsonl` to project directory
- Auto-create nodes from existing learnings, decisions, sessions on first run
- Add `graph_query()` tool: keyword match on nodes + BFS up to max_hops
- Config flag: `use_graph = false` (default)

**Risk:** minimal. Two new append-only files. Existing tools unchanged.

### Phase 2: Auto Edge Creation

- Hook `remember()`, `log_decision()`, file tools to create edges
- `in_session` edges for every learning/decision
- `references` edges when file paths appear in learning/decision text
- `uses_skill` edges when skills are invoked

**Risk:** low. Edges are append-only, computed at read time.

### Phase 3: LLM-Driven Graph Engineering

- Add `graph_add_edge()` tool for the agent to propose relationships
- Enhance `/retro` skill with a graph phase
- Add `/graph reconcile` skill: review contradictions, merge duplicates
- Enhance `context_block()` to include 1-hop neighbors

**Risk:** medium. Agent quality depends on model capability. Poor edges could clutter the graph.

### Phase 4: Graph Maintenance

- Confidence decay applies to nodes (reuse existing mechanism)
- Edges to decayed nodes are filtered at query time
- `/graph stats` command: show node/edge counts, orphan nodes, contradiction clusters
- Periodic compaction: remove edges to nodes with confidence ≤ 0

---

## What This Enables

1. **Tracing.** "Why do we use pytest?" → graph shows the decision, the learning that led to it, the session where it was decided, and the files it touches.

2. **Contradiction detection.** When two learnings have `contradicts` edges, the agent surfaces both and asks the user to resolve — instead of silently keeping the latest.

3. **Session-aware recall.** "We hit a build issue before" → graph returns the session where it happened, not just the distilled learning. The agent can re-read the full context.

4. **Skill discovery.** "What skills have I used for debugging?" → graph shows `uses_skill` edges from investigation sessions.

5. **Project onboarding.** New contributors can run `/graph overview` to see the project's knowledge structure: key decisions, major pitfalls, and how they connect.

---

## Alignment with the Survey

| Survey Section | lmloop Mapping |
|----------------|----------------|
| §3.1 Top-down ontology (LLM as assistant) | Agent proposes new node/edge types via `graph_add_edge` |
| §3.2 Bottom-up schema (KGs for LLMs) | Auto-extraction from sessions creates the schema from usage |
| §4.1 Schema-based extraction | Fixed node/edge types with structured fields |
| §4.2 Schema-free extraction | Agent can create `concept` nodes for emergent topics |
| §5.1 Schema-level fusion | Merging duplicate nodes during `/graph reconcile` |
| §5.2 Instance-level fusion | Resolving `contradicts` edges |
| §6.1 KG-based reasoning for LLMs | `graph_query` with multi-hop traversal |
| §6.2 Dynamic knowledge memory | The entire graph layer is a dynamic, self-evolving memory |

The key insight from the survey (§1, §6.4): KGs in LLM applications go **beyond RAG**. They provide structured reasoning about *how knowledge was acquired and how it relates*. lmloop's graph layer turns the agent's memory from a flat list into a navigable knowledge structure.

---

## Open Questions

1. **Edge quality control.** Should edges require user confirmation? Proposal: auto-edges (`in_session`, `references`) are implicit; LLM-proposed edges (`contradicts`, `leads_to`) show a summary in `/graph pending` for user review.

2. **Graph size limits.** What happens after 100 sessions? Proposal: same confidence decay as learnings. Nodes with effective confidence ≤ 0 are archived (moved to `graph_archive.jsonl`), and their edges are filtered at query time.

3. **Embedding support.** Should we add vector similarity for `related_to` edges? Proposal: not in v0.2. Local models make embedding expensive. Keyword + BFS is sufficient for the single-user, local-project scope. Can add later as an optional `graph_embed` tool.

4. **Visualization.** Should `/graph overview` render an ASCII graph? Proposal: a simple adjacency list for now. Rich ASCII graph rendering is a nice-to-have v0.3 feature.

---

## Non-Goals

- Multi-project graph federation (lmloop is single-user, single-project)
- Graph database backends (SQLite, Neo4j) — JSONL matches our file-only philosophy
- Real-time graph streaming — all updates are append-only, computed at read time
- Replacing the existing memory system — graph is an additive layer
