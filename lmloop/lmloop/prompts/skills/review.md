# Skill: review — pre-landing code review

Use when: a plan's tasks are complete, or the user asked for a review of a diff, before calling the work done.

Goal: find bugs that pass CI but break in production. Report only — do not
edit files during a review. Two passes, in order. Spec misses block. Minors are listed and not fixed in this pass.

## Pass 1 — Spec and plan
Read the design or plan when one was given, including its Review Focus. For each requirement, say met or missed and quote the line in the diff or the plan that shows it. A requirement with no task and no code is a miss. Severity for a miss is BLOCKER when a user would hit it, otherwise MAJOR. Do not grade a miss as MINOR because the spec was silent.

## Pass 2 — Quality

### Step 1 — Scope the diff
Run `git diff <base>...HEAD --stat` (detect the base branch: try
`git remote show origin` for HEAD branch, fall back to main/master). Then read
the full diff. If there is no branch diff, review uncommitted changes
(`git diff` + `git status`).

## Step 2 — Review passes (do each, in order)
1. Correctness: off-by-one, null/None paths, error handling that swallows
   failures, wrong async/await, resource leaks.
2. Edge cases: empty inputs, huge inputs, concurrent access, missing files,
   network failure. For each changed function ask "what input breaks this?"
3. Security: injection (shell, SQL, prompt), secrets in code, path traversal,
   unvalidated external input.
4. Blast radius: search for other callers of every changed function
   (`search_files`). Does the change break an assumption elsewhere?
5. Tests: does the diff change behavior without changing tests? Name the
   missing test cases concretely.

## Step 3 — Report
For each finding: severity (BLOCKER / MAJOR / MINOR / NIT), confidence (1–10),
file:line, quoted motivating line(s), what breaks, concrete fix. Order by
severity then confidence.

**Confidence gate:** Do not report findings below 7 unless you quote the exact
line that proves the issue. If you cannot quote it, drop the finding.

Minors and nits go in the report and stop there. Do not edit them in this pass.

End with a verdict: SHIP / SHIP WITH FIXES / DO NOT SHIP, plus one sentence
justification. SHIP WITH FIXES means a BLOCKER or MAJOR is still open.

If you find a repo-wide convention worth keeping, save it with `remember`
(type "pattern" or "architecture").
