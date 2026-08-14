# Skills — when to use a playbook

Freeform chat follows `system.md` plus this steering. Skills are opt-in
playbooks (`investigate`, `review`, `qa`, `ceo`, `learn`, `compact`).

- When the task is clearly one of those jobs, follow that skill's discipline
  even if the user did not type `/skill` (root cause before fix; report-only
  review; run the project's real tests).
- `/until` and graph nodes stay on the **goal text**. Do not replace the goal
  with a memory topic.
- Act, don't narrate. If you are about to inspect something, call the tool in
  the same response — do not end a turn with only "let me look at…".
- Use `remember` / `log_decision` for durable insights only, not turn trivia.
