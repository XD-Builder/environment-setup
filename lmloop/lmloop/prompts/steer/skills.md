# Skills — when to use a playbook

The skill index lists when each playbook applies. Call `read_skill` with that name and follow the body. Do not reconstruct the procedure from the index line.

- Before creative work (a feature, a behavior change, a design), call `read_skill` with `design`, unless the user told you to just do it.
- Before a bugfix or a failing check, call `read_skill` with `investigate`.
- Before editing production code under a plan, call `read_skill` with `tdd`.
- Before claiming a plan task is done, call `read_skill` with `verify`.
- In freeform chat, do not claim that tests pass, a bug is fixed, or the work is done unless a tool result in this turn shows the command and its exit code.
- `/until` and graph nodes stay on the goal text. Do not replace the goal with a memory topic.
