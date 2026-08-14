# Skill: retro — extract durable learnings from recent sessions

You will be given one or more recent session transcripts. Mine them for
knowledge worth keeping so future sessions start smarter. Be selective — a
noisy memory is worse than none.

Iron law: you are mining, not resuming. Do not continue the work in the
transcript. Do not call shell, file, search, or web tools. Use only memory
tools (`recall_memory`, `remember`, `log_decision`). Tool lines in the log
may be truncated — treat them as evidence of what happened, not a task list.

## Step 1 — Read
Read the transcript(s). Note: tasks attempted, what worked, what failed, what
was retried, decisions made, user corrections. Call `recall_memory` first so
you do not save a duplicate.

## Step 2 — Extract (quality bar: would this change behavior next session?)
Candidates worth saving:
- A pitfall that cost more than one attempt (type "pitfall").
- A command, flag, or workflow that worked and will recur (type "tool" or
  "operational").
- A project convention or architecture fact discovered the hard way
  (type "architecture" or "pattern").
- An explicit user preference or correction (type "preference",
  confidence 9-10, these never decay).

NOT worth saving: one-off facts, anything already in memory, vague advice
("be careful with X"), or "the session was interrupted / restore / continue".

## Step 3 — Save
For each keeper, call `remember` with a stable key, the insight as one or two
concrete sentences, and honest confidence (observed once = 5-6, confirmed
repeatedly = 8-9). If a durable decision was made in the session but never
logged, call `log_decision` for it.

## Step 4 — Report
List what you saved (key + one line each) and what you deliberately skipped
and why. End with one sentence on the overall pattern of the session(s).
Stop. Do not offer to pick the work back up.
