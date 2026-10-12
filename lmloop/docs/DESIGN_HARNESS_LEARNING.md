# Design: Harness learning (H1–H3)

**Status:** shipped (H1 fingerprint, H2 execution report, H3 adoption gate)  
**Date:** 2026-10-12  
**Source:** [FINDINGS_HARNESS_LEARNING.md](FINDINGS_HARNESS_LEARNING.md) · Zhang et al. [arXiv:2609.35738](https://arxiv.org/abs/2609.35738)  
**Depends on:** `harness_bundle.py`, `harness_report.py`, `contracts.py`, `evals.py`, `traces.py`, `run_log.py`

---

## Goals

1. **H1** — Stable `harness_bundle_id` on traces and workflow run meta rows.  
2. **H2** — Opt-in `lmloop eval --harness-report` for harness editors.  
3. **H3** — Golden case **roles** (`feedback` | `score` | `eval`) and `lmloop eval --gate harness-adopt`.

Non-goals: in-loop harness auto-edit, learned proposer, prompt injection of reports into `act()`.

---

## H1 — Bundle descriptor

Canonical JSON (sorted keys, UTF-8) hashed with SHA-256:

| Field | Source |
|-------|--------|
| `version` | `lmloop.__version__` |
| `system_skill` | SHA-256 of packaged `skills/system.md` |
| `steer` | Map stem → SHA-256 per file in packaged, user, and project steer dirs |
| `tool_names` | Sorted names from `build_tools(cfg)` |
| `config` | `autonomous_gates`, `until_control`, `until_max_steps`, `sandbox_network`, `eval_max_rounds` |
| `graph_name` | Optional graph run name |
| `check_fingerprint` | SHA-256 of sorted check command strings from until meta |

Workspace files under edit are **not** part of the bundle.

---

## H2 — Execution report

**Path:** `~/.lmloop/evals/harness_execution_report.json`

**CLI:** `lmloop eval --harness-report [--run PATH] [--json]`

Without `--run`, uses the latest until or graph JSONL in the current project. Parses meta + check/eval/gate failures; caps **8** failure rows. `question_sets.feedback` lists golden case ids with role `feedback` (for editors, not auto-injected).

---

## H3 — Roles and gate

- Cases without `roles` default to **`eval`** only.  
- **`--gate pr|commit|nightly`** — unchanged (all cases by layer).  
- **`--gate harness-adopt`** — cases with role `eval`, layers 1–3, plus validation abstention Act/Abstain vs `baseline.json` keys `harness-adopt`, `harness-adopt-act`, `harness-adopt-abstain` (2% max drop each, same as PR).

CI: PR workflow runs `harness-adopt` after the PR gate (informational parity with full gate at baseline 1.0).

---

## Verification

```bash
cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=. python -m lmloop eval --gate harness-adopt --json
PYTHONPATH=. python -m lmloop eval --harness-report --run tests/fixtures/harness_report/sample_until.jsonl --json
```

**Measured (2026-10-12, tree at ship):**

| Gate | Cases / n | Pass / Act / Abstain | vs baseline |
|------|-----------|----------------------|-------------|
| `pr` | 41 golden (layers 1–3) | 1.0 | 0 drop |
| `harness-adopt` | 31 golden `eval` + validation n=1 | 1.0 / 1.0 / 1.0 | 0 drop |
| Role split | 5 `feedback`, 5 `score`, 35 `eval` (4 eval cases are layer 4 only) | feedback ∩ score = ∅ | — |

H2 report on `tests/fixtures/harness_report/sample_until.jsonl` produces one check failure row and lists only feedback case ids in `question_sets.feedback` (score ids excluded).
