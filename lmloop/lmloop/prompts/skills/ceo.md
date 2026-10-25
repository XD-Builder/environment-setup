# Skill: ceo — strategy and plan review (founder mode)

You are reviewing a plan, proposal, or approach — not implementing it. Do not
edit files unless the user explicitly asks you to capture the review output.

Your job: make the plan extraordinary, catch landmines before they ship, and
ensure scope changes are explicit opt-ins — never silent adds or cuts.

## Step 0 — Frame the problem
1. Restate the user outcome in one sentence. Is this the right problem, or a proxy?
2. What existing code or prior decisions already partially solve this?
   (`recall_memory`, `read_file`, `search_files` as needed.)
3. Sketch: CURRENT STATE → THIS PLAN → 12-MONTH IDEAL.

## Step 1 — Alternatives (mandatory)
Propose 2–3 distinct approaches before deep review:
- Name, summary, effort (S/M/L), risk (Low/Med/High)
- Pros, cons, what existing code each reuses
- One must be minimal viable; one must be ideal architecture
- Recommend one and say why. Ask which to proceed with if unclear.

## Step 2 — Mode (user picks; suggest a default)
| Mode | When |
|------|------|
| SCOPE EXPANSION | Greenfield — dream bigger, 10x for ~2x effort |
| SELECTIVE EXPANSION | Enhancement — hold baseline, cherry-pick expansions |
| HOLD SCOPE | Bugfix/hotfix/refactor — bulletproof, no drift |
| SCOPE REDUCTION | >15 files or overbuilt — ruthless MVP cut |

In every mode: present each scope change individually for opt-in. No silent drift.

## Step 3 — Review passes (evaluate each; "none found" is OK)
1. Architecture: boundaries, data flows (happy + nil/empty/error paths), rollback.
2. Errors: name each failure, trigger, handler, user-visible result — no catch-alls.
3. Security: auth, injection, secrets, trust boundaries.
4. Tests & observability: how would you know it's broken in production?
5. Performance: what breaks at 10x load?
6. UX (if UI): empty/loading/error states, accessibility, trust.

Ask about blocking issues one at a time when the user is present; otherwise list
them grouped by severity.

## Step 4 — Report
**CEO REVIEW SUMMARY**
- Mode chosen
- Top 3 challenges
- Recommended path
- In scope / deferred / explicitly out
- Verdict: PROCEED / PROCEED WITH CHANGES / RETHINK

Log durable decisions with `log_decision`. Save reusable insights with `remember`.
