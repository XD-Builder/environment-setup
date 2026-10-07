---
name: lmloop-baseline
description: Floor rules every other loop in this repo inherits. Nothing routes here.
version: 1
abstract: true
warrant:
  read: []
  draft: []
  change: []
  send: []
  ask:
    - adding a third-party dependency
    - a new config key
    - rewriting the engineering bar
    - disabling tests, or deleting tests
    - spending money or provisioning paid infrastructure
    - git hooks, CI workflows, cron, or scheduled jobs
  never:
    - secrets, tokens, passwords, or private keys in any file
    - force push
    - push to master, or push to main
    - unimplemented feature as current behavior
    - swallowing exceptions to hide bugs
    - a parallel registry of the same names
    - disabling this warrant, or weakening this warrant
reserved:
  - merging a pull request
  - any change to what an agent is allowed to do
record:
  - which baseline rule applied and where the work stopped
---

# Brief

These rules hold for every job in this repo. A child loop inherits them and
cannot delete an `ask` or a `never`.

The product rules live in `lmloop/DEVELOPMENT.md`. This warrant is the part a
check can enforce. If they disagree, fix both in the same diff.

# Procedure

Change this baseline only when the rule is true for every job. A rule that
belongs to one job stays in that job's loop.
