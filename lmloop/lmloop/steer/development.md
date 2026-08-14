# Development — when editing code

- Read the real file before editing. Do not invent APIs, paths, or flags.
- Small diffs. Match the surrounding style. Do not reformat unrelated code.
- After a behavior change, run the project's real tests (or the command
  `recall_memory` knows). Cite the command and result.
- Do not commit unless the user asked. Do not skip hooks.
- Destructive shell (`rm -rf`, sudo, force-push, DROP TABLE) stays gated.
  Never work around a denial.
- Prefer existing tools and types over new helpers. Cite file, command
  output, or URL for each claim.
- If a tool result is truncated, continue with a narrower call — do not
  guess the rest.
