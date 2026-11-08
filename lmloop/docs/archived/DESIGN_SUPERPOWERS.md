# Superpowers methodology in lmloop

**Status:** shipped — skill index, plan ledger, one review, finish menu  
**Date:** 2026-11-06  
**Source:** [obra/superpowers](https://github.com/obra/superpowers) (MIT). The methodology is rewritten in lmloop’s voice. Upstream markdown is not vendored.

Superpowers is a skill library plus a session-start hook. A frontier model follows a long playbook because the hook injects it before the first reply. lmloop skills were opt-in slash playbooks, the standing prompt stays short, and one local model slot cannot go faster by fanning out ([DESIGN_ROADMAP.md](DESIGN_ROADMAP.md)). This port puts the methodology in the runner and keeps the playbooks short enough to load on demand.

User instructions still win. “Do not commit unless asked” in `prompts/steer/development.md` overrides any per-task commit step.

## What shipped

| Piece | Where it lives |
|---|---|
| Skill index (`Use when:` lines) plus `read_skill` | `prompts/skills/`, `capabilities/tools.py` |
| `design`, `write-plan`, `tdd`, `verify` | packaged skills, about a page each |
| `investigate` stops after three failed fixes; `techniques` is a sibling part | `prompts/skills/investigate.md`, `investigate/techniques.md` |
| `review` checks the spec, then the diff | `prompts/skills/review.md` |
| Plan parser, disk ledger, red-green gate, resume | `run/plan_runner.py` |
| One fresh review, one fix wave | `isolated_act` from `run/until_runner.py` |
| Worktree offer, baseline, merge / PR / keep | `company/worktree.py`, callbacks on `PlanHost` |

`design` writes `docs/designs/YYYY-MM-DD-<slug>.md` and stops until that file is approved. “Just do it” skips `design` only. `write-plan` writes `docs/plans/YYYY-MM-DD-<slug>.md` (Goal, Architecture, Spec, Global Constraints, Review Focus, Files, Interfaces, checkbox steps, `Run:` / `Expected:`). That file is not the campaign `plan` skill, which still emits JSON for `lmloop campaign`. The slash `/plan` is the runner; the campaign playbook is `lmloop skill plan`.

A task with a RED step stays open until the trace shows that command exiting non-zero and, later, exiting 0. A “done” claim with no fresh exit 0 stays open. Rulings are a `Ruling: <command> - <reason>` line the controller records. Silent deviation does not advance the task. The runner does not pause between tasks. It stops for a destructive command the existing gate already confirms, a merge or push, or a plan so contradictory the parser cannot name the next task.

The ledger is `~/.lmloop/plans/<slug>/<plan-id>/progress.json`. The worktree yes/no answer is `~/.lmloop/plans/<slug>/worktree.json`. That is a project file, not an 11th config key.

Golden trajectory cases cover an edit that precedes `read_skill tdd`, and a completion claim with no `run_shell` span. Wording drills that need a live model stay out of CI.

## Left out

These stay out so the next pass does not import them back in:

- **Visual companion** (`brainstorming/scripts/server.cjs`) and its telemetry. Text sketches are the design artifact.
- **Parallel dispatch** and per-task subagents. One model slot. Sequential `isolated_act` is the whole subagent port. Claude Code parity M3.2 can wrap that function later; this work does not add a `task` tool.
- **Model-tier routing.** One loaded model.
- **`diagnosing-superpowers`.** A later diagnosis can read `traces.jsonl`. It is a separate change.
- **Plugin hooks for other harnesses**, and the upstream evals repo. lmloop’s assertion pyramid covers the gates that can be checked without a model.
- **Commit-by-default.** A plan includes a commit step only when the user asked to commit.
- **Installing Superpowers** as a Cursor or Claude plugin in this repo.

`flow/graphs/company.md` is the multi-role path and is unchanged.
