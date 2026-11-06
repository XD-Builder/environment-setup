# Skill: grill — interview until the design tree is settled

Use when: the user asked to be grilled about a plan or a design.

Index: no

Iron law: this skill runs only because the user started it. Do not edit product code. End the turn after each round of questions.

Interview until you and the user share an understanding. Map the plan as a tree: every decision branches into the decisions that hang off it.

## Rounds

The frontier is every decision whose prerequisites are already settled. Ask that whole frontier in one round, then stop and wait. A question that depends on another question still open in this round waits for the next round.

Number each question. Give a recommended answer, worded so "yes" accepts it. Look up facts with tools. Do not ask the user for anything the repo or the request already answers.

```
Q1. <title>: <question>

Recommended: <answer a yes would accept>
```

The session is done when the frontier is empty and the user says the understanding is shared.

## Record as you go

Saving is not a question. When a term or a hard decision settles, write it. Do not ask whether to record it. If the user said not to save, interview only.

- `GLOSSARY.md` at the workspace root holds canonical terms and nothing else. No implementation detail, no spec, no scratch notes. Create the file when the first term is resolved. If the user uses a term that conflicts with the glossary, say so in this session and resolve it before writing.
- `docs/adr/NNNN-slug.md` only when all three are true: the choice is hard to reverse, surprising without the context, and the result of a real trade-off. Also `log_decision`. Skip the ADR when any of the three is missing.

Glossary entry:

```
### <Term>

<One sentence. What it is, and what it is not when that distinction matters.>
```

ADR:

```
# NNNN. <Title>

## Status

Accepted

## Context

<What forced the choice.>

## Decision

<What was chosen.>

## Consequences

<What this makes easier, and what it rules out.>
```

## Report

- Frontier: open, or empty
- Glossary: path, or not saved
- ADRs: paths, or none
