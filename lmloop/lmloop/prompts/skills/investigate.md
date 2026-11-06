# Skill: investigate — systematic root-cause debugging

Use when: a test fails, a bug is reported, or behavior does not match what the code should do, before any fix.

Iron law: NO FIXES WITHOUT ROOT CAUSE. Do not edit code until you can explain
the failure mechanism end to end.

Work in numbered phases. Announce each phase as you enter it.

## Phase 1 — Reproduce
1. Restate the reported symptom in one sentence.
2. Check memory first: `recall_memory` with the symptom's key terms — this
   project may have hit it before.
3. Reproduce the failure with a command or minimal steps. Paste the actual
   error output. If you cannot reproduce, say so and ask for the missing detail.

## Phase 2 — Localize
4. Follow the error backward: stack trace -> file -> function. Read the real
   code with `read_file`; never reason from imagined code.
5. Form at most two hypotheses. For each, state what evidence would confirm or
   kill it.

## Phase 3 — Confirm
6. Test the leading hypothesis with the cheapest decisive check (a log line, a
   one-liner script, a targeted command). Iterate until one hypothesis survives.
7. State the root cause as a mechanism: "X happens because Y, triggered by Z."

## Phase 4 — Fix and lock in
8. Only now apply the minimal fix, and only by the `tdd` skill: a failing test first, then the change. Re-run the reproduction from Phase 1 and quote the exit code.
9. If this is the third fix that did not hold, stop. Say the design is wrong and do not attempt a fourth fix. Ask the user before changing the architecture.
10. Save the insight: `remember` with type "pitfall" — symptom, root cause, fix. Future sessions should never re-debug this from scratch.

When the bad value is still unexplained after Phase 1, call `read_skill` with name `investigate` and part `techniques` and follow that trace.

Report format at the end: Symptom / Root cause / Fix / Verification / Learning saved.

## Rationalizations

- "The issue is simple, so skip the phases." Simple bugs still have a cause. Walk the phases.
- "One more fix" after two misses. The third miss stops the work. Question the design.
- "I'll patch the symptom so the check goes green." A green check at the symptom leaves the source in place.
