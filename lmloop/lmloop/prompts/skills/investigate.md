# Skill: investigate — systematic root-cause debugging

Use when: a test fails, a bug is reported, or behavior does not match what the code should do, before any fix.

Iron law: NO FIXES WITHOUT ROOT CAUSE. Do not edit code until you can explain
the failure mechanism end to end. No hypothesis until a command you have already
run goes red on the user's exact symptom.

Work in numbered phases. Announce each phase as you enter it.

## Phase 1 — Reproduce
1. Restate the reported symptom in one sentence.
2. Check memory first: `recall_memory` with the symptom's key terms — this
   project may have hit it before.
3. Run one command that goes red on that symptom. Paste the command and the
   output. Phase 1 is done only when that command has already been run.
4. If you cannot build that command, stop. Say what you tried. Do not
   hypothesize, and do not lecture about the design.

Call `read_skill` with name `investigate` and part `loop` and follow it to
tighten the command, minimise the repro, and test predictions. Do not present
that list for approval.

## Phase 2 — Fix and lock in
5. Only after one prediction survives, apply the minimal fix, and only by the
   `tdd` skill: a failing test first, then the change. Re-run the Phase 1
   command and quote the exit code.
6. If this is the third fix that did not hold, stop. Say the design is wrong
   and do not attempt a fourth fix. Ask the user before changing the architecture.
7. Remove every temporary log that used your debug prefix.
8. Save the insight: `remember` with type "pitfall". The insight names the red
   command, the mechanism, and the fix. Future sessions should run that command
   instead of re-debugging from scratch.

Report format at the end: Symptom / Red command / Root cause / Fix / Verification / Learning saved.

## Rationalizations

- "The issue is simple, so skip the phases." Simple bugs still have a cause. Walk the phases.
- "I can see the bug, so a red command can wait." No red command, no hypothesis.
- "One more fix" after two misses. The third miss stops the work. Question the design.
- "I'll patch the symptom so the check goes green." A green check at the symptom leaves the source in place.
