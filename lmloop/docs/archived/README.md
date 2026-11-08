# Archived design docs

Shipped design specifications and the research they consumed. They are the
record of decisions and non-goals; **current behavior** is in
[ARCHITECTURE.md](../ARCHITECTURE.md) and [README.md](../../README.md), and the
live list of deferred work is [ROADMAP.md](../ROADMAP.md).

| File | Topic |
|------|--------|
| [DESIGN_LLM_CALLING.md](DESIGN_LLM_CALLING.md) | `act()`, gather/answer, streaming, eval budgets |
| [DESIGN_FILE_READING.md](DESIGN_FILE_READING.md) | `@path`, `read_file` headers, workspace scope |
| [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md) | Authored workflow graphs (`graph.py`) |
| [DESIGN_LOOP_AND_GRAPH.md](DESIGN_LOOP_AND_GRAPH.md) | Opt-in knowledge graph (`use_graph`) |
| [DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md) | `usage.jsonl` gaps, abstention pairs, human-gated improve loop |
| [DESIGN_ASSERTION_DRIVEN_EVALS.md](DESIGN_ASSERTION_DRIVEN_EVALS.md) | Four-layer contracts, golden cases, CI gates, local traces |
| [DESIGN_BUSINESS_AND_WORKFLOW_LAYERS.md](DESIGN_BUSINESS_AND_WORKFLOW_LAYERS.md) | Business vs workflow module split (`run_log`, DSL, runners) |
| [DESIGN_META_CONTROL_AND_BUDGET.md](DESIGN_META_CONTROL_AND_BUDGET.md) | `until_control` off/budget/meta, artifacts, `until analyze` |
| [DESIGN_MULTI_AGENT_COMPANY.md](DESIGN_MULTI_AGENT_COMPANY.md) | Docker company mode, manifest, worktrees, allowlist |
| [DESIGN_LONG_HORIZON_PLANNING.md](DESIGN_LONG_HORIZON_PLANNING.md) | Campaigns: plan, board, daily tick, supervisors |
| [DESIGN_EVOLVING_SPIRIT.md](DESIGN_EVOLVING_SPIRIT.md) | Spirit layer: seed, actions, thoughts, traits, `self.md` |
| [DESIGN_COMMAND_CONSOLIDATION.md](DESIGN_COMMAND_CONSOLIDATION.md) | Memory slash vocabulary; merge-tools and slash workflow control cut |
| [DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) | Abstention pairs, sandbox governance; unattended optimizer is a non-goal |
| [DESIGN_CONCURRENCY.md](DESIGN_CONCURRENCY.md) | Per-category tool pools inside one concurrent wave |
| [DESIGN_TRUNCATION_AND_CUTOFF.md](DESIGN_TRUNCATION_AND_CUTOFF.md) | Display limits; `/transcript` tool rows full by default |
| [DESIGN_SANDBOX_AND_VERIFICATION.md](DESIGN_SANDBOX_AND_VERIFICATION.md) | Snapshots, derived checks, baseline, opt-in `--docker`; per-cycle probe deferred |
| [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md) | Concurrency lock, DAG fan-out, `lmloop flow`, knowledge canvas; parallel agents deferred |
| [DESIGN_HARNESS_LEARNING.md](DESIGN_HARNESS_LEARNING.md) | Harness fingerprint, execution report, adoption gate |
| [DESIGN_SUPERPOWERS.md](DESIGN_SUPERPOWERS.md) | Skill index, plan ledger, one review, finish menu (and what was cut) |
| [DESIGN_STRUCTURAL_DECOMPOSITION.md](DESIGN_STRUCTURAL_DECOMPOSITION.md) | Category packages, five-module cap |
| [DESIGN_CLAUDE_CODE_PARITY.md](DESIGN_CLAUDE_CODE_PARITY.md) | MCP client, hooks, `task` subagent, project rules, session controls (OAuth and SSE cut) |
| [DESIGN_ROADMAP.md](DESIGN_ROADMAP.md) | Round-5 cuts, config budget, steps 0–9 |
| [FINDINGS_HARNESS_LEARNING.md](FINDINGS_HARNESS_LEARNING.md) | Research behind the harness-learning design |
| [FINDINGS_META_AGENTIC_META_REASONING.md](FINDINGS_META_AGENTIC_META_REASONING.md) | Research behind the meta-control design |
