# Skill: verify — evidence before a completion claim

Use when: you are about to say a task is done, a bug is fixed, tests pass, or a plan step succeeded.

Iron law: no completion claim without a fresh command and its output in this task. An earlier run, a green diff, or "should pass" is not evidence.

## Steps

1. Name the command that proves the claim. A plan step's `Run:` line is that command. If the plan has none, use the project's real test command.
2. Run the full command in this task. Do not reuse output from a previous task.
3. Read the exit code and the failure count. Quote them.
4. If the output does not support the claim, say the actual status and stop. Do not soften it.
5. Only then say the task is complete. If you deviated from the plan, add a line `Ruling: <command> - <reason>` so the ledger records it. A ruling does not replace the command.

## Report

- Claim: one sentence
- Command: the command you ran
- Exit: the code you read
- Gaps: anything the command does not prove

## Rationalizations

- "It passed earlier in the session." Run it again on this tree.
- "The diff looks right." A diff is not an exit code.
- "The linter passed, so the tests pass." Run the tests.
- "I'm confident." Confidence is not output.
