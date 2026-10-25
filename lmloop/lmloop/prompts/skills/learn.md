# Skill: learn — manage project memory

Review, search, prune, and curate what this project has learned across sessions.
Use tools — do not guess what's in memory.

## Step 1 — Inventory
1. Call `recall_memory` with no query (or the user's query) to load candidates.
2. If the user gave a topic, filter to matching keys/insights.
3. Show a table: key · type · confidence · one-line insight.

## Step 2 — Quality pass
For each learning, ask:
- Still true? (If stale, note it — suggest lower confidence or removal.)
- Actionable? (Would it change behavior next session?)
- Duplicate? (Same key or overlapping insight — merge into one key.)

Flag entries that are vague ("be careful with X") or one-off trivia.

## Step 3 — User-directed actions
If the user asked to prune/export/fix:
- **Prune:** list keys to drop; only `remember` overwrites — tell user which keys
  to ignore or overwrite with empty insight via explicit correction.
- **Export:** print the curated list in markdown bullet form.
- **Add:** if the user states a preference or fact, `remember` with
  type "preference", confidence 9–10, source "user-stated".

## Step 4 — Report
- Count reviewed / kept / flagged stale
- Top 5 highest-value learnings for this project right now
- Gaps: what should we learn but haven't captured yet?
