# Archived design docs

Fully **shipped** design specifications live here. They remain the historical record of
decisions and non-goals; **current behavior** is described in [ARCHITECTURE.md](../ARCHITECTURE.md)
and [README.md](../../README.md).

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

Active (partial or proposed) specs stay in [../](../). Build order:
[../IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) and [../DESIGN_ROADMAP.md](../DESIGN_ROADMAP.md).
