---
name: implement-design
description: Implement one roadmap slice from the lmloop design docs, with tests and honest docs.
version: 1
extends: lmloop-baseline
goal: One roadmap slice on a feature branch, tests green, and docs that describe only what shipped.
triggers:
  - implement the docs
  - implement a design doc
  - roadmap step
  - DESIGN_
warrant:
  read:
    - lmloop
    - docs
    - tests
    - README
    - DEVELOPMENT
  draft:
    - source changes
    - tests
    - documentation updates
    - a pull request description
  change:
    - lmloop
    - tests
    - docs
    - README
    - DEVELOPMENT
    - ARCHITECTURE
  send:
    - git push of the feature branch to origin
    - a draft pull request for the feature branch
  ask:
    - parallel agents
    - embeddings rerank
    - per-cycle probe
    - docker sandbox
    - OpenRouter
    - multi-agent company
    - merging a pull request
  never: []
stop:
  - the new behavior has a regression test and the unittest suite is green
  - README and ARCHITECTURE describe the slice as current behavior
  - the design doc status is shipped, or the leftover stays under non-goals
  - the slice is deferred in DESIGN_ROADMAP — stop and hand it back
budget:
  attempts: 3
  parallelism: 1
reserved:
  - deciding to build a deferred roadmap item
record:
  - which roadmap slice this was, and which design section it came from
  - tests run and the result, including failures
  - docs updated, and any design status that flipped to shipped
  - where work stopped
---

# Brief

Build from `lmloop/docs/DESIGN_ROADMAP.md`. The order there is the order here.
Read `lmloop/DEVELOPMENT.md` before editing code: one registry, the import
graph, tests in the same diff, docs in lockstep.

Already shipped, so do not rebuild them and do not describe them as proposals:

- Step 0. Spawn errors and exit 126/127 are `blocked`. `until_max_steps` is in
  `config.DEFAULTS`. `GraphView` is the memory hot path. `graphs/company.md`
  does not hardcode `pytest`.
- Step 1. `snapshot.py` writes a git snapshot under `refs/lmloop/` before each
  autonomous maker step. `autonomous_snapshot` defaults to `git`. The README
  troubleshooting row tells the user how to restore.

Still design, not current behavior: derived checks, the frozen memory block,
targeted tests, the host lock, DAG fan-out, FTS5, and the sandbox. Deferred
until the roadmap's revisit trigger: parallel agents, the per-cycle probe,
embeddings rerank, and the multi-agent company (`--docker` plus OpenRouter).

# Procedure

1. Pick the earliest unshipped core step. Do not start a deferred item.
2. Read that step's design section and the code it names. Implement one slice,
   not the whole document.
3. Ship tests with the behavior. Patch the module where the name is looked up.
4. Move shipped behavior into `README.md` and `docs/ARCHITECTURE.md`. Flip the
   design doc status to `shipped`. Leave leftovers under non-goals. Never
   describe an unimplemented feature as current behavior.
5. Push only the feature branch and open a draft pull request. Merging stays
   with a human.
