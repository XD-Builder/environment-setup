# Changes: assertion-driven evaluation

**Status:** shipped (deterministic pyramid, local traces, CI gates)
**Date:** 2026-10-11
**Depends on:** `contracts.py`, `traces.py`, `tools.validate_tool_arguments`, `agent.act`, `evals/golden/`
**Companions:** [DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md) ·
[ARCHITECTURE.md](ARCHITECTURE.md) · [DEVELOPMENT.md](../DEVELOPMENT.md)

This is the change record for treating lmloop generations like deterministic software:
a four-layer assertion pyramid, three CI gates, and a local trace queue. Usage-gap
reports (`lmloop eval` with no gate flag) are unchanged.

---

## What changed

| Area | Change |
|---|---|
| Layer 1 types | `contracts.py` checks a JSON Schema subset, Python `ast`, the graph DSL, live `ToolDef` arguments, and trace-span shape. `tools.validate_tool_arguments` is the single parser `dispatch` uses. |
| Layer 2 contracts | Exact match, regex, ROUGE-1, BLEU-1, bag-of-words cosine, forbidden tokens, secret leaks, and a citation/faithfulness rubric. No judge model. |
| Layer 3 trajectories | Tool order, redundant calls, repeated tool-name cycles, state-key allowlists, rollback equality, and abstention (empty calls + `status: abstain`). |
| Layer 4 perturbations | Frozen cases with injected distractors, schema mutations, and secrets placed after filler text. |
| Golden set | `lmloop/evals/golden/cases.json` plus `baseline.json`. Cases are code. |
| CI gates | `lmloop eval --gate commit\|pr\|nightly`. Workflow: `.github/workflows/lmloop-eval-gates.yml`. |
| Traces | `act()` writes parent/child spans to `~/.lmloop/traces.jsonl` and enqueues the parent. `lmloop eval --drain` scores the queue off the turn and anonymizes failures into `~/.lmloop/evals/golden_inbox.jsonl`. |

---

## Pyramid

```
                ▲
               / \     Layer 4  nightly perturbations
              /---\    distractors, schema mutations, secret-after-filler
             /     \
            /-------\  Layer 3  trajectories
           /         \ order, cycles, rollback, abstain
          /-----------\
         /             \ Layer 2  contracts
        /---------------\ invariants, rouge/bleu/cosine, rubric
       /                 \
      /-------------------\ Layer 1  syntax and types
     /                     \ schema, ast, graph DSL, tool args, span shape
    -------------------------
```

A case **passes** when `expect` is `pass` and the checker is silent, or when `expect`
is `fail` and the checker reports a violation. Expected failures are how the suite
locks regressions: the golden row stays, and a checker that stops noticing the bug
fails the case.

| Gate | When | Layers | Block rule |
|---|---|---|---|
| `commit` | `git push` | 1 | any drop from the baseline (100% while the baseline is 1) |
| `pr` | pull request | 1–3 | pass-rate drop **greater than 2%** blocks merge |
| `nightly` | schedule | 1–4 | job fails as an **alert**; `blocks_merge` stays false |

Rates are compared in basis points so a drop of exactly 2.00% does not trip the PR
gate. The committed matrix is smaller than 50 cases, so one failure is already more
than 2% and the PR gate is fail-closed until the set grows. That is intentional.

```bash
lmloop eval --gate commit          # layer 1, exit 1 on any miss
lmloop eval --gate pr --json       # layers 1–3
lmloop eval --gate nightly         # includes perturbations
lmloop eval                        # usage gaps, same as before
lmloop eval --drain                # score ~/.lmloop/eval_queue.jsonl
lmloop eval --inbox                # anonymized failures waiting for review
```

Commit and PR gates are in-process and do not call a model, so they sit inside the
60-second commit budget. The nightly job is the same runner plus layer 4; it is an
alert channel, not a two-hour model fuzz.

---

## Production loop (local)

Inference and evaluation are separate files:

1. **Spans.** Each `act()` opens a trace. Tool calls become child spans (`name: "tool"`,
   `parent_span_id` set) after they return. Arguments are not stored — shell commands
   and file bodies are where secrets show up. The parent records rounds, tool names,
   `ok`, and `interrupted`.
2. **Queue.** The parent is appended to `eval_queue.jsonl`. The turn does not wait
   for a score. `lmloop eval --drain` is the consumer (the local stand-in for a
   stream worker).
3. **Inbox.** Spans that fail `contracts.check_span` (missing fields, non-object
   attributes, secret-shaped text) are anonymized and appended to
   `evals/golden_inbox.jsonl` with `flagged: true`. A person promotes a row into
   `evals/golden/cases.json`. The loop does not commit by itself.

Opt out of spans with `LMLOOP_TRACE=0` or `LMLOOP_USAGE=0`. Set `LMLOOP_TRACE_CONTENT=1`
to keep a 240-character redacted excerpt of the last assistant message.

---

## Stack mapping

The industry stack this design is answering (promptfoo, DeepEval, Ragas, Langfuse,
Outlines, Instructor, Giskard, Kafka) does not land as dependencies. lmloop stays
on the stdlib plus the existing REPL extras, and nothing leaves the machine.

| Requested piece | Here |
|---|---|
| JSON Schema / Pydantic / structured decoding | `contracts.check_schema` (type, required, properties, enum, additionalProperties, items, min/max) and `validate_tool_arguments` |
| Grammar / AST | `ast.parse`; graph lines through `parse_graph` |
| Exact, regex, BLEU, ROUGE, cosine | `contracts.check_metric` (ROUGE-1 F1, BLEU-1, bag-of-words cosine) |
| Small rubric judge | `faithfulness` + required citation substring. A local model can replace the function later; the case file stays |
| Trajectory DAG, state, abstention | `check_trajectory` |
| Golden datasets as code | `evals/golden/cases.json` |
| OpenTelemetry traces | JSONL spans with `trace_id`, `span_id`, `parent_span_id`, `name`, `attributes` |
| Async eval queue | `eval_queue.jsonl` + `--drain` |
| Feedback into the golden set | anonymized inbox, human promotion |

Not built, on purpose:

- A hosted collector, Kafka, SQS, or any export off the machine.
- Frontier or SLM judges inside the gate. The rubric is deterministic so CI has no token cost and no network.
- Auto-commit of inbox rows. Same human gate as `lmloop eval --design`.
- Live adversarial generation. Layer 4 mutates frozen strings; it does not call the model.

---

## Case shape

```json
{
  "id": "trajectory-abstain",
  "layer": 3,
  "kind": "trajectory",
  "expect": "pass",
  "expect_abstain": true,
  "status": "abstain",
  "calls": []
}
```

`kind` selects a checker in `contracts.CHECKERS`: `schema`, `python`, `grammar`,
`tool_args`, `span`, `invariant`, `metric`, `rubric`, `trajectory`, `perturbation`.
Perturbations copy an inner case, then inject text or patch the schema, and cannot nest.

Add a regression by appending a case that fails on today's bug and passes once the
fix exists — or an `expect: fail` case that records a violation the checker must
keep reporting. Do not weaken a threshold to go green.

---

## Tests

`tests/test_contracts.py` locks the checkers, the 2% edge, and the packaged golden
file. `tests/test_traces.py` locks parent/child spans, redaction, drain → inbox,
and a mocked `act()` that enqueues a span.

```bash
cd lmloop && PYTHONPATH=. python -m unittest tests.test_contracts tests.test_traces -q
```
