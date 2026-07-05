# Skill: qa — verify changes end-to-end

Goal: prove the changed behavior works, find regressions, fix what you can.
Adapt to the project (CLI, library, web app, scripts) — do not assume a website.

## Step 1 — Scope
1. Identify what changed: `git diff --stat`, `git status`, or the user's description.
2. State the acceptance criteria in bullet form (what "working" means).

## Step 2 — Test tiers (pick Standard unless user says Quick or Exhaustive)
- **Quick:** smoke test critical paths only.
- **Standard:** critical + main edge cases + one regression check.
- **Exhaustive:** + error paths, concurrency-ish cases, cosmetic polish.

Run the project's real test/lint/build commands (`recall_memory` for known commands).

## Step 3 — Execute
1. Run tests and record pass/fail with command output cited.
2. For failures: use `/investigate` discipline — root cause before fix.
3. Apply minimal fixes; re-run the failing check until green.
4. Do not commit unless the user asked.

## Step 4 — Report
- Health: PASS / PASS WITH FIXES / FAIL
- Checks run (command + result)
- Bugs found and fixed (or filed as remaining work)
- Gaps: untested areas worth a follow-up

Save recurring test commands or pitfalls with `remember`.
