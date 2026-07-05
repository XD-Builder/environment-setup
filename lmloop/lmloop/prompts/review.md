# Skill: review — pre-landing code review

Goal: find bugs that pass CI but break in production. Report only — do not
edit files during a review.

## Step 1 — Scope the diff
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

End with a verdict: SHIP / SHIP WITH FIXES / DO NOT SHIP, plus one sentence
justification.

If you find a repo-wide convention worth keeping, save it with `remember`
(type "pattern" or "architecture").
