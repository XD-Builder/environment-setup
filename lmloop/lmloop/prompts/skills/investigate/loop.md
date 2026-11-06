# Investigate loop — a red command, then a smaller one

Use when: `read_skill` is called as `investigate` with part `loop`, while you are building or tightening the command that goes red.

## The red command

Phase 1 is done when you can name one command you have already run, and it:

- goes red on the user's exact symptom, not a nearby failure
- returns the same verdict on each run
- you can run unattended

Paste the invocation and the output. If you do not have that command, stop and say what you tried. Do not read code to build a theory first.

Prefer, in order: a failing test at the seam that reaches the bug, a CLI invocation with a fixture, a replay of a captured request, a throwaway harness that calls the path once. A fixed sleep is not a loop. Poll the state the test needs, with a timeout that fails when it never arrives.

## Tighten and minimise

Make the command faster and the assertion sharper. Then cut inputs, callers, config, and steps one at a time. Keep a cut only when the command still goes red. Done when removing any remaining piece makes it go green.

## Hypotheses

After the minimised command is red, rank a few falsifiable predictions. Each one states: if this is the cause, then changing one named thing makes the bug disappear or get worse. Test one change at a time. Do not show the list for approval.

Tag every temporary log with a unique prefix, for example `[DEBUG-a4f2]`. Remove every line with that prefix before the report.

When the bad value is still unexplained, call `read_skill` with name `investigate` and part `techniques` and follow that trace.

## Regression test

Write the regression test before the fix, at a seam that exercises the real bug. If no such seam exists, say so in the pitfall. Do not pretend a shallow test locks the bug down.
