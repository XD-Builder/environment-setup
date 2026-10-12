# Skill: improve — close a usage gap

Human-gated. Read the local eval report, pick one action gap, and propose the
smallest code change. Do not commit.

## Step 1 — Report
1. Prefer `~/.lmloop/evals/harness_execution_report.json` when present
   (`lmloop eval --harness-report [--run PATH]` after an until/graph run).
2. Otherwise use `~/.lmloop/evals/last_report.json` (`lmloop eval --json`).
3. List gaps with `severity: action` only (usage report) or summarize harness
   failures from the execution report (check/eval/gate rows).

## Step 2 — One gap
Pick the first action gap. Quote its `id`, `evidence`, and `design_hook`.
Open or extend that design doc using the sections from `design_doc_skeleton`
(Observed gaps, Proposed changes, Non-goals).

## Step 3 — Smallest diff
Implement one fix and a test in the same change. Prefer extending
`tests/test_evals.py` or the area the gap names.

## Step 4 — Stop
Show the diff summary and the test command. Do not commit. Do not start a
second gap in the same turn. Before opening a harness PR, run
`lmloop eval --gate harness-adopt`.

## Harness edits
Add a standing line only when it states curiosity, learning, growth, or the
safety floor, and a tool, skill, or learning does not already enforce it.
Otherwise save a learning or a spirit trait. If two lines say the same thing,
delete one.
