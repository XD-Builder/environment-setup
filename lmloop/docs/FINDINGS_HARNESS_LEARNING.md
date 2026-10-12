# Findings: Harness Learning (top 3, regression-safe)

**Date:** 2026-10-12  
**Primary source:** Zhang et al., *Harness Learning Enables Generalizable Test-Time Adaptation* (arXiv [2609.35738](https://arxiv.org/abs/2609.35738), 2026)  
**Status:** implemented — see [DESIGN_HARNESS_LEARNING.md](DESIGN_HARNESS_LEARNING.md)  
**Scope:** Only changes that **do not alter default `act()` / `until` / graph behavior** and cannot regress live runs unless the user explicitly opts in. Omitted: test-time harness auto-edit, best-of-N selection inside the loop, multistep RL, or any change that injects revision context into standing prompts.  
**Audience:** Eval owners, harness editors (`improve`, `graph propose`), CI gate maintainers  
**Companions:** [FINDINGS_META_AGENTIC_META_REASONING.md](FINDINGS_META_AGENTIC_META_REASONING.md) · [DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) · [archived/DESIGN_ASSERTION_DRIVEN_EVALS.md](archived/DESIGN_ASSERTION_DRIVEN_EVALS.md) · [archived/DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](archived/DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md)

---

## Executive summary

The paper treats the **harness** as the adapted object (executable control flow + tool orchestration) with a **frozen solver**. Gains come from (1) **execution reports** that summarize failures on a dev set, (2) **disjoint** feedback vs scoring vs held-out eval questions, and (3) measuring **which harness** ran, not only which model. Three lmloop investments copy that structure **without touching the hot path**:

| # | Initiative | Why it is significant | Why it cannot regress quality |
|---|------------|------------------------|-------------------------------|
| **H1** | **Harness bundle fingerprint** on traces and run logs | Separates “model got worse” from “harness changed”; required for any serious harness A/B | Additive fields only; no prompt or loop change |
| **H2** | **Opt-in execution report** for harness editors | Paper proposer input = task + harness + report; beats ad-hoc chat for `/improve` and `graph propose` | Built offline; consumed only when user runs report command or skill step |
| **H3** | **Three-way golden split + adoption gate** | Prevents false wins from scoring on the same cases that produced feedback (paper §4.1) | New gate runs only on harness-touched PRs; Act/Abstain/Pair rule already validated on fixtures |

Everything else in the paper (learned proposer, GRPO, sequential revision inside the task loop) stays **out of scope** until H1–H3 are shipped and baselines are pinned.

---

## H1 — Harness bundle fingerprint

### Paper link

Adaptation is meta-learning over programs \(h_0 \rightarrow h_T\). Evaluation fixes the solver and varies \(h\). Without a stable id for \(h\), trajectories are not comparable across runs.

### lmloop definition

A **harness bundle** is the executable policy around the model, not the project repo under edit:

- Packaged + active **steer** stems and **system** skill content hash  
- **Tool registry** identity (`build_tools()` names + gate mode snapshot)  
- For control flows: **graph name / DSL hash** or `until` control mode (`until_control`, check plan fingerprint)  
- Config keys that change loop semantics (`until_max_steps`, `sandbox_network`, autonomous gate flags)

### Proposed behavior

- Compute `harness_bundle_id` (e.g. SHA-256 of a canonical JSON bundle descriptor).  
- Attach to: parent span in `traces.jsonl`, `until/*.jsonl` run header, `graph` run records, and optionally `usage.jsonl` rows for `until.*` / `graph.*` features.

### Regression safety

- **Write-only metadata.** No branch on `harness_bundle_id` inside `act()`, makers, or checks.  
- Default user experience unchanged; analysts and CI diff ids across commits.

### Done when

- Two consecutive runs with identical packaged harness produce identical `harness_bundle_id` on the same lmloop version.  
- Changing only `skills/system.md` or a packaged graph changes the id; changing workspace source under agent edit does not.  
- Documented field list lives in [ARCHITECTURE.md](ARCHITECTURE.md) (one subsection).

### Expected impact

Unlocks measuring harness evolution ROI (paper reports mean proposal quality **and** oracle best-of-N **per fixed solver**). Without H1, H2/H3 cannot attribute score deltas.

---

## H2 — Opt-in execution report (harness editor input)

### Paper link

Proposer context \(x_t\) = task description + current harness + **execution report** on \(Q^{\mathrm{fb}}\) (aggregate scores, failure counts, capped failed items with trace excerpts). Held-out eval text never appears in the report used for revision.

### lmloop analogue

Aggregate existing artifacts into a single file, e.g. `~/.lmloop/evals/harness_execution_report.json` (or beside `last_report.json`), produced by:

```text
lmloop eval --harness-report [--run PATH] [--json]
```

**Sources (read-only):** latest or specified `until`/`graph` JSONL, check/eval outcomes, `UntilArtifactStore` summaries, drain-scored spans where available.

**Suggested schema (minimal):**

| Field | Content |
|-------|---------|
| `task` | Goal text, project slug, mode (`until` / `graph` / one-shot) |
| `harness_bundle_id` | From H1 |
| `solver` | Model id string from server (informational; solver stays fixed during harness experiments) |
| `aggregate` | Pass/fail counts, check transitions, token/tool totals |
| `failures` | Up to **8** entries: case or check id, short error excerpt, optional trace span id (no full transcripts) |
| `question_sets` | Which case ids were used to build this report (`feedback` role only) |

### Consumption (opt-in only)

- Extend **`improve`** Step 1: if report exists, prefer it over raw usage gaps for harness-shaped gaps.  
- **`graph propose`**: optional `--report` flag; default remains flow stats only.  
- **Not** injected into `skills/system.md`, steer, or default maker prompts.

### Regression safety

- No report unless the user runs `--harness-report` or a skill explicitly loads the file.  
- Live agent paths unchanged.

### Done when

- Report generates from a finished until run with ≥1 failed check without calling the model.  
- Unit test: fixture log → golden report JSON (stable keys; clipped excerpts).  
- `improve.md` references the report as optional input.

### Expected impact

Matches the paper’s highest-leverage **input format** for revision quality. Human-gated harness PRs and future proposers share the same signal; avoids dumping full JSONL into prompts.

---

## H3 — Three-way golden split + harness adoption gate

### Paper link

\(Q^{\mathrm{fb}}\), \(Q^{\mathrm{score}}\), and \(Q^{\mathrm{eval}}\) are **disjoint** roles. Training rewards use scoring questions that were **not** in the feedback report for that revision. Held-out eval measures transfer.

### lmloop analogue (build on what shipped)

You already have:

- **Assertion golden** cases (`evals/golden/cases.json`) and **baseline** pass rates for `--gate pr`.  
- **Abstention validation** fixtures disjoint from mutation training pools ([DESIGN_CONTINUAL_HARNESS…](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) §4.1, HERA selection rule).

**Extend cases only with metadata**, not new default runtime behavior:

```json
"roles": ["feedback"]
```

```json
"roles": ["score"]
```

```json
"roles": ["eval"]
```

Rules:

- **Feedback** — may appear in H2 reports and gap mining; never the sole basis for merge.  
- **Score** — candidate harness proposals (human or scripted) must beat parent on this set.  
- **Eval** — merge gate; same cases as today’s PR/nightly layers plus abstention **validation** pairs.

**Adoption rule (unchanged semantics, explicit harness scope):** adopt harness change \(H_{k+1}\) only if on **eval** role cases and validation abstention fixtures, Act and Abstain do not decrease vs \(H_k\), and at least one strictly improves (HERA §4.1).

### New CI surface (opt-in path, not default commit gate)

- Add `lmloop eval --gate harness-adopt` that runs **eval-role** + abstention validation only.  
- Wire GitHub Actions to call it **only when paths touch harness surfaces** (`lmloop/lmloop/skills/`, `steer/`, `graphs/`, `loop.py`, `graph_runner.py`, `checks.py`, `contracts.py`, golden cases).  
- Pin baseline in `baseline.json` before enabling required check; until then, job is **informational**.

### Regression safety

- Default `lmloop eval --gate commit|pr|nightly` unchanged.  
- No split tags → cases behave as today (treat untagged as `eval` for gates).  
- Agent loop never reads `roles`.

### Done when

- Every golden case tagged; at least one **score** case not in any default H2 feedback template.  
- `--gate harness-adopt` documented; CI job optional/informational until baseline PR lands.  
- `improve` Step 4 reminds: run harness-adopt before opening harness PR.

### Expected impact

Stops the largest silent failure mode in harness evolution: **overfitting dev feedback** the editor just saw (paper §4.1; aligns with Wang et al. on harness evolution eval). Significant for any multi-candidate or iterative harness edit; zero cost on normal coding sessions.

---

## Explicit non-goals (significant but not regression-safe yet)

| Idea from paper | Why deferred |
|-----------------|--------------|
| Sequential harness revision inside `until` | Changes maker/check dynamics; needs H1–H3 + pinned baselines |
| Best-of-N harness proposals per round | Exploration policy can lower dev score before raising eval |
| Learned proposer / GRPO | Training infra; solver coupling risk |
| Injecting reports into every turn | Prompt bloat and behavior drift |

Revisit after H1–H3 metrics show **proposal reliability** (parse/run rate on graph DSL drafts) comparable to paper Fig. 6 — optional follow-up metric, not in the top 3.

---

## Suggested implementation order

1. **H1** (1–2 diffs: hash helper + span/run headers)  
2. **H2** (report builder + test + skill doc)  
3. **H3** (case tags + `--gate harness-adopt` + optional CI path)

Meta-reasoning ([FINDINGS_META_AGENTIC_META_REASONING.md](FINDINGS_META_AGENTIC_META_REASONING.md)) remains **within-run** control; this doc is **across-run harness identity and evaluation hygiene**. Compose both later; do not merge controllers until adoption gates exist.
