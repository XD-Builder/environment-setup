# Skill: design — agree the design before any code

Use when: the user wants something built, changed, or planned and has not approved a design doc for it.

Iron law: no product code, scaffold, dependency install, or implementation skill until the user approves the design file. Reading the repo is fine. A user who says to just do it opts out of this skill only.

## Steps

1. Read the request and the code it would touch. Do not ask what the repo or the request already answers.
2. Ask one question only when the answer would change what gets built and nothing already answers it. Otherwise state your call and the reason.
3. In a message of its own, lay out two or three approaches that fit this repo, what each trades off, and which you would pick. If only one approach fits, say why the others do not.
4. Size the work in plain words. A small change gets a short design. A project gets a full one. If it grows, stop and step up a size.
5. Write `docs/designs/YYYY-MM-DD-<slug>.md`. Cover intent, goals, non-goals, constraints, the approaches and which one was chosen, and what is left to the builder. Do not commit unless the user asked.
6. For a project-sized doc, run one fresh side session that reads the file as a builder who must plan from it. Sort what it asks into already answered, your call, or the user's. Bring the user's pile back once, most important first.
7. Stop. Do not invoke `write-plan` until the user approves that file.

## Report

- Design: path
- Approach: one line
- Waiting on: approval, or the one open question

## Rationalizations

- "A small change needs no file." Write the short design. Approval is the file, not a verbal okay.
- "I already know the approach." Say it, and the ones you dropped, before any code.
- "I'll scaffold while we talk." Scaffolding is building. Stop.
