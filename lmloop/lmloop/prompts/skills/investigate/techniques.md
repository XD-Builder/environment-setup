# Investigate techniques — backward tracing

Use when: `read_skill` is called as `investigate` with part `techniques`, after the failure is reproduced and the bad value is still unexplained.

## Backward trace

1. Start at the line that observed the bad value. Quote it.
2. Name the function that passed it in. Read that caller with `read_file`. Do not imagine the code.
3. Repeat until you reach the site that first produced the value. Fix that site. A guard at the symptom is a second fix, not the first.
4. If the value crosses a process or service boundary, log what enters and what leaves that boundary, run once, and only then decide which side is wrong.

## After the cause is confirmed

Add a check at the source and one at the boundary that hid the bad value, so the next failure names itself. Do not add a third layer "while you are here."

## Waiting

Replace a fixed sleep with a condition: poll the state the test actually needs, with a timeout that fails the test when the condition never arrives. Quote the condition in the test name.
