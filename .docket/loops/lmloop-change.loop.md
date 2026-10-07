---
name: lmloop-change
description: Change lmloop, the editor config, or the docs under DEVELOPMENT.md.
version: 1
extends: lmloop-baseline
goal: A feature-branch change whose tests pass and whose docs still describe the program.
triggers:
  - fix a bug
  - refactor
  - add a feature
  - update tests
  - lmloop
warrant:
  read:
    - lmloop
    - docs
    - tests
    - README
    - DEVELOPMENT
    - shell
    - nvim
    - vim
    - tmux
    - setup.sh
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
    - shell
    - nvim
    - vim
    - tmux
    - setup.sh
    - .cursor
    - .docket
  send:
    - git push of the feature branch to origin
    - a draft pull request for the feature branch
  ask:
    - merging a pull request
    - building a deferred roadmap item
  never: []
stop:
  - unittest is green for any lmloop behavior change
  - README, ARCHITECTURE, and DEVELOPMENT still match the code
  - the change would add a dependency, a config key, or a deferred feature
budget:
  attempts: 3
  parallelism: 1
record:
  - what changed and which module owns it
  - tests run and the result, including failures
  - which docs were updated
  - where work stopped
---

# Brief

This is the default loop for work in this repo that is not "implement a design
doc". The bar is `lmloop/DEVELOPMENT.md`. Architecture is
`lmloop/docs/ARCHITECTURE.md`. User-facing behavior is `lmloop/README.md`.

lmloop stays on the stdlib plus `prompt_toolkit`, `rich`, and `ddgs`. The agent
loop uses `urllib`. Do not add a package, a config key, or a second list of
the same names unless the baseline `ask` is answered by a human.

# Procedure

1. Extend the owning type. Tools are `ToolDef` rows. Commands are `CommandMeta`.
   Session state is `SessionState`. Do not add a parallel name list.
2. Keep the import graph. `stream` does not import `agent`. `loop` does not
   import `graph`. `agent` does not import `loop` or `graph`.
3. Parse with `shlex`, `html.parser`, `json`, `pathlib`, or `argparse`. Regex
   only for a named charset, ANSI strip, a slug, or `@path`.
4. Tests ship in the same diff: a happy path, one edge, and a regression that
   would have failed before the fix. Prefer fakes over the network.
5. Catch a specific exception at a boundary. `act()` rolls back and re-raises.
   Do not wrap a bug as `ServerError`, and do not swallow it.
6. Push the feature branch and open a draft pull request. Do not merge it.
