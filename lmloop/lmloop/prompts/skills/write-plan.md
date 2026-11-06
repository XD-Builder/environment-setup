# Skill: write-plan — an implementation plan a cold reader can execute

Use when: a design doc is approved, or the user asked for an implementation plan, and no code has been written for it yet.

This is not the campaign `plan` skill. That skill emits a JSON patch for `lmloop campaign`. This skill writes a markdown plan a later `lmloop plan run` can execute one task at a time.

Iron law: every step lets the implementer do exactly one thing and check it. No production code in this skill.

## Steps

1. Read the approved design. If it covers independent subsystems, say so and write one plan per subsystem.
2. List the files to create or modify and what each one is responsible for. Follow the repo's existing patterns.
3. Write `docs/plans/YYYY-MM-DD-<slug>.md`. Do not commit unless the user asked.
4. Start with Goal, Architecture, Tech stack, Spec path, Global Constraints (exact values from the spec), and Review Focus (failure modes the spec implies that no task's tests cover yet, each with the test that will pin it).
5. Each task is the smallest unit with its own test cycle. Include Files, Interfaces (consumes / produces, exact signatures), and checkbox steps. A test step names the test and the assertions. A code step names the signature and the file. A verification step has `Run:` and `Expected:`.
6. Mark a failing run `Expected: FAIL` and the matching pass `Expected: PASS`. Those lines are the runner's evidence gate.
7. Self-review: every spec requirement has a task, later signatures match earlier ones, and the plan is shorter than a transcript of the program. Fix gaps in place.
8. Stop. Ask the user to read the plan and choose: run it here with `lmloop plan run`, or stop after the plan. Do not start implementing in this skill.

## Report

- Plan: path
- Tasks: count
- Waiting on: run it here, or stop

## Rationalizations

- "The implementer will figure out the names." Names and signatures are the plan. Leave the function body to the implementer.
- "I'll include a commit on every task." Commit only when the user asked.
- "TBD is fine for the edge case." A line that decides nothing is a gap. Name the check.
