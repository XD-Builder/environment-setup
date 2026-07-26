# Skill: compact — compress conversation context

The thread is long. Produce a dense handoff that preserves everything needed
to continue without re-reading the full transcript.

## Output format (markdown, no tool calls unless saving)

### Goal
One sentence: what we're trying to accomplish.

### Done
Bulleted facts with file:line or command citations where relevant.

### Decisions
Bulleted durable choices (offer `log_decision` for any not yet logged).

### Open work
Numbered next steps, in priority order.

### Do not repeat
List dead ends, rejected approaches, and false hypotheses so the next turn
doesn't re-derive them.

Keep under ~800 words. Be concrete — names, paths, error messages, not vibes.

After the summary, stop. The REPL will ask the user whether to replace the
in-memory thread with this summary (session log is kept either way).
