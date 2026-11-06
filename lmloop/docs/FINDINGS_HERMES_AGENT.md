# Findings: Hermes Agent's learning loop, and event-driven learning for lmloop

**Date:** 2026-11-06  
**Primary source:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — source, docs, tests, and the issue tracker. Shallow clone of `main` at `ca436069` (2026-11-06); issue and PR figures were checked against GitHub on the same date. Hermes paths are relative to that repo; lmloop paths are relative to this `lmloop/` directory, on `master` after PR #81.  
**Status:** research input — not a build track. Section 9 proposes phases L0–L5 for a future design doc.  
**Scope:** How lmloop should learn after every run and every long run **without a fixed reflection schedule**, and where that process hooks into the REPL, `until`, `plan`, graph, and campaign paths. Out of scope: weight training (Hermes trajectories and Atropos), external memory providers (Honcho and others), and the skills hub.  
**Audience:** Memory and spirit owners, until/graph/plan runner owners, harness editors (`retro`, `improve`)  
**Companions:** [FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md](FINDINGS_OPEN_SOURCE_AGENT_HARNESSES.md) · [FINDINGS_HARNESS_LEARNING.md](FINDINGS_HARNESS_LEARNING.md) · [DESIGN_MEMORY_RETRIEVAL.md](DESIGN_MEMORY_RETRIEVAL.md) · [archived/DESIGN_EVOLVING_SPIRIT.md](archived/DESIGN_EVOLVING_SPIRIT.md) · [archived/DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](archived/DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md) · [DESIGN_ROADMAP.md](DESIGN_ROADMAP.md) (config budget, latency-neutral bar)

---

## Executive summary

Hermes markets itself as the agent with "a built-in learning loop". In the source, that loop is
six cooperating parts:

1. **A post-delivery review fork.** After the reply is shown, a background copy of the agent sees
   the finished conversation and writes to memory and skills through the normal tools, with a tool
   whitelist and hard budgets.
2. **Activity counters as triggers.** Memory review fires every 10 user turns; skill review fires
   every 10 tool-calling iterations. A counter resets when the agent saves something on its own.
3. **An idle queue for local servers.** On a managed llama.cpp server, reviews wait until the
   server has been idle for a settle window, coalesce per session, and age out.
4. **Quality rules in the review prompt.** Ground every fact in the conversation; one fact goes to
   one store; never capture environment failures, negative tool claims, or unresolved failures;
   patch the skill that was loaded before creating a new one; write lessons, not logs.
5. **Procedural memory.** Task lessons go into the skill for that task, which loads on demand.
   Always-on memory is capped at 2,200 + 1,375 characters and forces consolidation when full.
6. **Usage telemetry plus an inactivity-triggered curator** that marks unused agent-created skills
   stale and archives them (never deletes), with pins, locks, a ledger, and rollback.

Hermes' **own issue tracker** shows where the counter-based cadence breaks, and those failures
answer the question "how to learn without a fixed schedule":

- Short sessions never reach the threshold, so a one-turn correction is lost
  ([#58669](https://github.com/NousResearch/hermes-agent/issues/58669),
  [#18369](https://github.com/NousResearch/hermes-agent/issues/18369)).
- One agentic prompt with fifty tool rounds counts as one "turn"
  ([#94226](https://github.com/NousResearch/hermes-agent/issues/94226)).
- Session boundaries such as `/new`, exit, and compression are not triggers
  ([#31597](https://github.com/NousResearch/hermes-agent/issues/31597)).
- A review cancelled by the next message is dropped, and its counter has already been reset
  ([#145920](https://github.com/NousResearch/hermes-agent/issues/145920)).
- On a local llama.cpp server the concurrent fork exhausts the KV cache and halves response speed
  ([#54115](https://github.com/NousResearch/hermes-agent/issues/54115)).
- Write-freely skill edits produced irrelevant and unrecoverable changes
  ([#66350](https://github.com/NousResearch/hermes-agent/issues/66350),
  [#70128](https://github.com/NousResearch/hermes-agent/issues/70128)).
- Archival based on incomplete usage telemetry archived skills that were still in use
  ([#136030](https://github.com/NousResearch/hermes-agent/issues/136030)).

Measurements that Hermes users posted point the same way:

- **Verified sessions are the lever.** On PAST-Bench, exam pass rates were 8% with no learning
  and 33% after the default review of the agent's own sessions. Reviewing a single
  verified-correct session reached 56%
  ([#134531](https://github.com/NousResearch/hermes-agent/issues/134531)).
- **Eager writing does not help.** A skill the review edited 77 times scored 18.88 vs 18.84 out of
  20 against its earlier version in a blind replay of 40 runs
  ([#136931](https://github.com/NousResearch/hermes-agent/issues/136931)).
- **Most unprompted writes are junk.** In windows where the user taught nothing, 27 of 30 reviews
  wrote something, and 20 of 22 memory writes were junk or duplicates
  ([#128884](https://github.com/NousResearch/hermes-agent/issues/128884)).

Nothing in the Hermes tree measures whether a saved memory or skill helps a later session; an
RFC for that is still open ([#96704](https://github.com/NousResearch/hermes-agent/issues/96704)).

**Recommendation.** lmloop should not copy the counters. It should copy the review fork's
discipline (whitelist, budgets, quality rules, provenance) and **trigger learning on evidence**:
score every turn and every run from signals lmloop already records, without a model call. Review
only when something salient happened, at the next natural boundary, from a durable queue so
nothing is lost. Close the loop by measuring whether learnings are used and whether the same
mistake recurs.

| # | Initiative | Why it is significant | Why it cannot regress live runs |
|---|---|---|---|
| **L0** | Fix the mining-model defect, cap the retro transcript, and record learning use and failure fingerprints | Makes the loop measurable. Without a baseline, no later change can be judged | Measurement only, plus routing mining to `eval_model` (a no-op when `eval_model` is empty) |
| **L1** | **Salience ledger and durable review queue** | Replaces "every N turns" and "daily" with "when something happened"; a happy path costs nothing | Deterministic and append-only. Queue-only: nothing runs automatically |
| **L2** | **Boundary reviews over salient windows**, with Hermes' quality blocks ported into `retro` | Learns after every run that taught something, including failed runs, which are not mined today | Never mid-turn and never concurrent with a live call. `retro.md` changes go through `--gate harness-adopt` |
| **L3** | Outcome attribution: a cited learning that held is refreshed; a contradicted one triggers review | The first evidence-based retention signal; replaces time-only decay for used rows | Changes ranking inputs only; nothing is deleted |
| **L4** | Per-project skill notes (procedural memory), staged and gated | Moves task lessons out of the always-on block into the step they affect, which matters most for small local contexts | Packaged skills stay immutable; notes are staged and scanned before they load |
| **L5** | Evidence-triggered campaign reflection and spirit distill; optional idle-prompt reviews | Removes the last calendar trigger (`campaign_daily_reflect`) | Off the hot path; the idle mode cancels on the first keystroke |

---

## 1. Hermes' learning loop, from the source

### 1.1 The background review fork

The fork replaced inline memory and skill nudges, which had "polluted 43% of user messages with
backward-looking system instructions" ([PR #2235](https://github.com/NousResearch/hermes-agent/pull/2235)).
lmloop's equivalent rule: learning prompts never enter the live conversation.

| Aspect | Behavior | Source |
|---|---|---|
| When it spawns | After the turn is delivered, only if there is a final response, the turn was not interrupted, and a memory or skill trigger fired | `agent/turn_finalizer.py:591-616`, `:863-873` |
| What it is | A forked `AIAgent` that shares the system prompt, `tools[]`, and session id, so request #1 is a warm prefix-cache read of the whole conversation. Making the cache key byte-identical cut per-review cost by about 89% and end-to-end cost by about 26% on Sonnet 4.5 ([PR #25434](https://github.com/NousResearch/hermes-agent/pull/25434)) | `agent/background_review.py:170-182`, `build_cache_parity_fork` `:979` |
| Routed to another model | The cache is cold anyway, so it replays a digest: the last 24 messages verbatim and older turns summarized. The PR's benchmark: Haiku was about 39× cheaper than Opus but caught 4/6 skill and 3/4 memory signals versus 5/6 and 4/4, so routing stays opt-in ([PR #49252](https://github.com/NousResearch/hermes-agent/pull/49252)) | `background_review.py:332-362`, `:1225-1232` |
| Tools | Dispatch-side whitelist: the skills toolset plus `read_file`/`search_files`; the memory tool only on memory-triggered reviews, so a skill review cannot act on "consolidate now" and delete entries nobody reviewed (#105921). Read tools were added after denying them caused a "denial storm" (about 142 denials and 204 read-before-write refusals over 2 days on one deployment) that starved the loop. Tool schemas stay identical for cache parity | `_review_tool_whitelist` `:1114-1135` |
| Budgets | At most 16 iterations; aggregate input of 75% of the review model's context, capped at 600k tokens (120k fallback) | `:173-182` |
| Safety | Dangerous commands resolve to "deny" instead of `input()`, which deadlocked against the parent's prompt (#15216); secret prompts are skipped; the prompts forbid deleting a skill (the curator consolidates) | `background_review.py:1062-1088`; prompts `:456-578` |
| Live-turn priority | A new user turn hard-cancels the review, waiting at most a bounded timeout for acknowledgement | `cancel_background_review_for_live_turn` `:129-167` |
| Output | Writes go straight to the stores. One summary line is shown in chat (`display.memory_notifications`); usage is attributed to the parent session | `summarize_background_review_actions` `:718`; `spawn_background_review_thread` `:1351` |
| Explicit run | `/refine [focus]` runs the same review on demand | `website/docs/user-guide/features/memory.md` |
| Cost | Hermes' own comment puts a review at about 30K tokens per event; cron skips reviews because it has "no human-in-the-loop need" for them | `cron/scheduler.py:2511` |

### 1.2 Triggers: activity counters, not wall-clock time

| Counter | Unit | Default | Reset | Source |
|---|---|---|---|---|
| `memory.nudge_interval` | Top-level user turns | 10 | When it fires (before the review runs), or on an organic `memory` call | `agent/turn_context.py:765-772`; `agent/tool_executor.py:737` |
| `skills.creation_nudge_interval` | Tool-calling iterations | 10 in code; 15 in `cli-config.yaml.example` | When it fires, or on an organic `skill_manage` call | `agent/turn_iteration_prep.py:167`; `turn_finalizer.py:865-869`; `tool_executor.py:739`; `agent/agent_init.py:1279-1281` |

Counters are rehydrated from history on resume (`turn_context.py:757-762`). Reviews are skipped for
cron, interrupted turns, and empty responses. Delegated children are meant to be skipped too, but
[#141318](https://github.com/NousResearch/hermes-agent/issues/141318) reports that they still run
reviews.

Two details matter for lmloop:

- **The organic-save reset is the right idea.** If the agent already saved, the next review would
  only find duplicates.
- **Resetting at fire time is the root cause of [#145920](https://github.com/NousResearch/hermes-agent/issues/145920).**
  A cancelled review is not retried on cloud providers, so for chatty users memory is "effectively
  never written". The reporter measured 0 bytes written when the next message arrived 0.35 s after
  the review started.

### 1.3 The idle queue (local single-slot servers)

`agent/review_idle_queue.py` defers reviews on a managed llama-server:

- It waits for `_IDLE_SETTLE_S = 15` seconds of quiet, polling every 5 s, and confirms through the
  server's `/slots` that no slot `is_processing` (`:26-28`, `:210`).
- It keeps one slot per session; the newest snapshot wins and the original enqueue time is kept
  (`enqueue` `:97`).
- Items older than 30 minutes dispatch regardless of idleness (`_MAX_AGE_DEFAULT_S`).
- A preempted review is requeued at most 3 times (`run_agent.py:825-838`), but only when
  `_review_should_defer` is true (`run_agent.py:172`), which means only on a managed local server.

The module docstring states the problem: "On the managed llama-server the post-turn review fork
monopolizes the GPU the next prompt needs and the next live turn cancels it (decode cost paid,
learning lost)." [#54115](https://github.com/NousResearch/hermes-agent/issues/54115) reports the
same thing from the field. Two concurrent slots (the main conversation plus an uncompressed
review replay) exceeded `n_ctx`, and even without running out of memory, "GPU compute resources
are split between two slots running concurrently, halving the main conversation's response
speed." That is lmloop's default deployment: LM Studio, one model, one slot.

### 1.4 Review prompt quality rules

The review prompts are the most transferable artifact. Each block carries a comment naming the
failure it prevents.

- **Memory routing** (`background_review.py:370-389`, added for #30220 after the reviewer wrote the
  same lesson into both stores until both filled):
  - "One fact goes to ONE store, never both."
  - "Save only facts GROUNDED in this conversation — a user message or tool result you can point
    to."
  - "Never turn a one-off request into a standing preference … unless the user marked it general
    ('always', 'from now on', 'remember')."
- **Lesson layer** (`:401-428`):
  - Procedure first, with pitfalls attached to the step they affect.
  - "A pitfall is a generalizable rule + one clause of WHY."
  - "No PR/issue numbers, dates, ticket IDs."
  - "The same lesson learned twice is ONE rule."
  - Do not restate what always-loaded context already teaches.
  - "Fix the skill in place when it is wrong … do not append 'UPDATE: actually...'".
- **Do not capture** (`:431-453`):
  - Environment-dependent failures: missing binaries, "command not found", credentials.
  - Negative claims about tools ("these harden into refusals the agent cites against itself for
    months").
  - Transient errors that resolved ("the lesson is the retry pattern").
  - One-off task narratives.
  - **Unresolved failures**: "do NOT write those attempts up as a 'reliable workflow' … That
    presents an untested sequence of failures as validated guidance."
- **Preference order for skill writes:** patch a skill loaded this session, then patch an umbrella
  skill, then add a support file, then create a class-level umbrella. Read before write is enforced
  by the tool guards.
- **Bias toward writing.** "Be ACTIVE — most sessions produce at least one update … 'Nothing to
  save.' is a real option but should NOT be the default" (`:457`, `:524`). The wording came from
  [PR #17213](https://github.com/NousResearch/hermes-agent/pull/17213); the curator
  ([PR #17277](https://github.com/NousResearch/hermes-agent/pull/17277)) landed the same day to
  prune what it writes. [#128884](https://github.com/NousResearch/hermes-agent/issues/128884)
  measured the cost of that bias: on windows where the user taught nothing, 27 of 30 reviews wrote
  something. The same issue proposes letting the reviewer skip "a review the clock fired, because
  nothing of that kind was learned since the last one". lmloop's `retro` takes the opposite stance
  ("a noisy memory is worse than none"). Event-driven triggers let lmloop keep that bar: it reviews
  less often, but every review it runs has evidence behind it.

### 1.5 Stores and routing

| Store | Shape | Notes | Source |
|---|---|---|---|
| `MEMORY.md` / `USER.md` | `§`-delimited entries; 2,200 / 1,375 character caps | An over-budget `add` is rejected with the current entries shown ("reissue as ONE batch that removes or shortens…"). After 3 failed consolidations in a turn, the model is told to stop retrying | `tools/memory_tool_store.py:188-192`, `:329-337`, `:206-221` |
| Frozen snapshot | System-prompt memory is captured once per session | Mid-session writes persist to disk but appear only next session, "preserving the prefix cache" | `memory_tool_store.py:649-652` |
| Skills | `SKILL.md` + `references/`/`templates/`/`scripts/`; index descriptions ≤ 60 characters | Progressive disclosure: an always-on index, bodies on demand. Mid-session index changes go on the per-turn user-message note channel, not into the system prompt (#104414) | `tools/skill_manager_tool.py:837-853`; `agent/skills_index_delta.py:1-14` |
| `session_search` | SQLite FTS5 over all sessions; discover/scroll/read/browse | No LLM calls; cron sessions are demoted | `tools/session_search_tool.py:1-32`, `:664-792` |

The memory tool's `WHEN` text is the routing rule lmloop lacks: "Anything learned while doing a
task (procedures, pitfalls, and the user's preferences and corrections for that kind of work)
belongs in the task's skill … memory is injected into every turn and must stay small"
(`tools/memory_tool.py:375-396`).

### 1.6 Telemetry and the curator

- **Usage sidecar.** `skills/.usage.json` records views, uses, patches, and timestamps per skill.
  `created_by` (`agent`, `learn`, or none) records provenance and decides which skills the curator
  manages (`tools/skill_usage.py:1-6`, `:493-533`).
- **Reuse after patch.** `bump_use` reports `reuse_after_patch` when a skill is used again after a
  patch (`:497-508`). It is the closest Hermes signal to "the lesson was useful", but it records
  only that the skill was reused, not that the reuse succeeded.
- **Curator trigger.** The curator runs on inactivity, not cron (`agent/curator.py:1-7`): when
  `last_run_at` is older than 7 days and the agent has been idle for 2 hours or more
  (`should_run_now` `:160-179`, `maybe_run_curator` `:1204`). The first observation only seeds the
  timer, after #18373 found a fresh install mutating the library on its first tick. CLI and
  gateway callers pass infinite idle, so in practice only the interval gates them.
- **Deterministic pass.** active → stale at 14 days → archived at 30 days (`:209-263`). Pinned,
  locked, and cron-referenced skills are exempt; never-used skills younger than the stale window
  are kept. The deterministic pass has been the source of bugs: it staled 71 built-in skills on
  first run (#79295), and it archived skills used only through scripts or other skills (#136030,
  #126613).
- **LLM consolidation** into class-level umbrellas is opt-in (`DEFAULT_CONSOLIDATE = False`,
  `:35`). It is snapshot-backed with `hermes curator rollback`, and archives rather than deletes.

### 1.7 Safety rails around self-written content

- **Threat scan on every memory write** (`tools/threat_patterns.py`): invisible Unicode,
  injection, exfiltration, and secret patterns. Hits are masked as `[BLOCKED: …]` in the snapshot
  but kept in the live file so the user can remove them.
- **Agent-created skill guard** (`skills.guard_agent_created`, default off): a dangerous verdict
  rolls back the write.
- **Provenance** separates foreground writes from background-review writes
  (`tools/skill_provenance.py`).
- **An append-only skill ledger** with content-addressed blobs supports per-edit rollback.
- **Pins and locks:** a pin blocks archival; a lock blocks content changes.
- **`write_approval`** stages memory and skill writes for `/memory pending`. It ships off, which
  #70128 calls out because the resulting edits are unrecoverable.

### 1.8 What Hermes does not have

- **No quality eval for the learning loop.** `tests/agent/test_background_review_*.py` and
  `test_curator_*.py` are contract tests (cache parity, whitelist, budgets, summaries, rollback).
  `evals/` covers wire and provider probes only. The curator's only outcome metric is a run counter
  bucketed by action.
- **No outcome attribution.** Nothing links "this memory or skill was in context" to "the turn
  succeeded or was corrected". Both gaps are open requests: an eval harness RFC
  ([#96704](https://github.com/NousResearch/hermes-agent/issues/96704)) and "Curator selects
  skills by usage, not outcome" ([#138653](https://github.com/NousResearch/hermes-agent/issues/138653)).
- **No structured run outcomes.** Hermes reviews conversations. lmloop's `until`, `plan`, and graph
  runs record check exit codes, red/green transitions, abstains, and gates, which are far richer
  learning signals than a chat transcript.

---

## 2. Failure modes from the Hermes issue tracker

| Issue | Symptom | Lesson for lmloop |
|---|---|---|
| [#58669](https://github.com/NousResearch/hermes-agent/issues/58669), [#18369](https://github.com/NousResearch/hermes-agent/issues/18369) | Sessions shorter than the nudge interval never get reviewed; `/new` resets the counters | Trigger on salient events, not counts. One correction is enough |
| [#94226](https://github.com/NousResearch/hermes-agent/issues/94226) | Memory cadence counts user prompts while one prompt drives dozens of tool rounds | Accrue debt in units of work (tool rounds, errors), not prompts |
| [#31597](https://github.com/NousResearch/hermes-agent/issues/31597) | No review at reset, close, or compression — "the natural moments when memory is *most* useful" | Session boundaries are first-class review points |
| [#145920](https://github.com/NousResearch/hermes-agent/issues/145920) | A cancelled review is lost and its counter is already reset | Clear debt only after a review completes; keep the queue on disk |
| [#54115](https://github.com/NousResearch/hermes-agent/issues/54115) | A concurrent review slot exceeds `n_ctx` or halves speed on local llama.cpp | Never run a review concurrently with a live call on a single slot; bound the review input |
| [#82708](https://github.com/NousResearch/hermes-agent/issues/82708) | The memory-review fork still creates skills when the skill trigger is disabled | Check the kill switch at dispatch, per store, not only at trigger time |
| [#141318](https://github.com/NousResearch/hermes-agent/issues/141318) | Delegated subagents run their own background reviews | Never review inside `isolated_act` children or reviews themselves |
| [#66350](https://github.com/NousResearch/hermes-agent/issues/66350), [#70128](https://github.com/NousResearch/hermes-agent/issues/70128), [#64926](https://github.com/NousResearch/hermes-agent/issues/64926) | Reviews write unrelated content into skills; edits ship un-gated and cannot be undone | Relevance gate (only skills loaded in the window), staging, append-only history |
| [#110689](https://github.com/NousResearch/hermes-agent/issues/110689), [#126685](https://github.com/NousResearch/hermes-agent/issues/126685) | All-or-nothing review batches strand independent additions | Apply review writes independently; one bad write must not drop the rest |
| [#136030](https://github.com/NousResearch/hermes-agent/issues/136030), [#126613](https://github.com/NousResearch/hermes-agent/issues/126613) | Usage-based archival with incomplete telemetry archives live skills | Telemetry may demote from injection; it must never delete or hide from recall |
| [#117294](https://github.com/NousResearch/hermes-agent/issues/117294) | Default-on review on one host: 471 review runs; 96.8% of skill mutations unattended; 241 skills in a 73 MB library, 51 never used | Skip happy paths; report the review tax |
| [#88429](https://github.com/NousResearch/hermes-agent/issues/88429) | "User corrections ossify into permanent global prohibitions" | Scope a correction to the task or command it was about; let attribution reopen it |
| [#66180](https://github.com/NousResearch/hermes-agent/issues/66180) | Auto-created skills are never loaded during task execution | Attach lessons to something that already loads (learnings, notes on loaded skills), not to new skills that must be discovered |

### 2.1 Measured results

| Source | Setup | Result | Implication for lmloop |
|---|---|---|---|
| [#134531](https://github.com/NousResearch/hermes-agent/issues/134531) (PAST-Bench) | 6 procedural helpdesk families, `gpt-5.4-mini`, judge-free grading from an audit log | No learning: 8%. Default review of the agent's own sessions: 33%. Review of one **verified-correct** session: 56% (20/36). The SOP verbatim as a skill: 89%. Only about 38% of learning episodes were actually solved | Learn from verified segments. lmloop's fail→pass and red→green transitions are verification it already has |
| [#136931](https://github.com/NousResearch/hermes-agent/issues/136931) | One skill: 7.4 KB → 40.2 KB over 77 edits; blind replay of before/after bytes, 40 runs, 64 judgments | 18.88 vs 18.84 out of 20: no measurable difference | More edits are not better. Measure before trusting a write path |
| [#128884](https://github.com/NousResearch/hermes-agent/issues/128884) | 10 windows where the user taught nothing, 3 samples each | 27 of 30 runs wrote something; 20 of 22 memory writes junk or duplicate. Yet "most skill writes in those same windows are real lessons from the work" | Trigger on evidence, including tool-recovery accrual, not on user turns alone |
| [PR #49252](https://github.com/NousResearch/hermes-agent/pull/49252) | Same real session, Opus vs Haiku digest | Haiku about 39× cheaper; caught 4/6 skill and 3/4 memory signals vs 5/6 and 4/4 | Keep the review on the main model by default (`eval_model` empty) |
| [PR #25434](https://github.com/NousResearch/hermes-agent/pull/25434) | Byte-identical cache key for the fork | About −89% per review call, −26% end-to-end (Sonnet 4.5) | Cache parity is worth measuring for REPL reviews on llama.cpp |

---

## 3. lmloop today

| Capability | Where | Behavior | Gap |
|---|---|---|---|
| `until` mining | `run/until_runner.py:336-363` (`next_role`), `:1171`, `:1470-1478` | `("check","pass")` and `("eval","pass")` go to `mine` when `until_mine` (default true). Synchronous, over every session in the run | Only **passing** runs are mined. Abstain, gate-no, pause, and an exhausted budget are not. A one-cycle trivial pass is mined anyway |
| Retro input | `memory/__init__.py:676` `read_session` (20,000 characters each), `:716` `format_sessions_for_retro` | Joins every session, with no total cap | A long run produces up to 20k × N characters, so the moment that taught something is diluted or crowded out of a local context |
| Graph mining | `flow/graph_runner.py:496-514` (terminal pass), `:529-534` (`mine` node) | `graph_mine` default true | Same as `until`: pass-only |
| Plan runner | `run/plan_runner.py` | No mining | Red→green transitions and blocking review findings are the strongest signals lmloop records, and they are never mined |
| REPL | `entry/repl.py:307-311` (`_hud_line`), `:315` (`_maybe_mine_on_exit`), `:285` (`_cmd_new`), `:569` (`_cmd_compact`) | Manual `/memory mine`; a static "long session — /memory mine" hint at ≥ 8 user turns; `mine_on_exit` default false | `/new` and `/compact` discard context without harvesting it. The hint counts turns, not evidence |
| Mining model | `entry/cli.py:427`, `:580`; `entry/repl.py:433-476` | `make_session_miner(cfg, model, …)` and `isolated_act(cfg, model, …)` pass no `model_override` | **Defect:** `core/config.py:97` and the roadmap's config table both say mining uses `eval_model`; the code always uses the main model |
| Terminal run hooks | `flow/workflow_finish.py` (`finish_until_run`, `finish_graph_run`) | Fires for **every** outcome; records usage and a knowledge-graph run row | The natural seam for run-level salience, unused for learning |
| Spirit | `spirit/__init__.py:162-176` (`looks_like_correction`, `note_user_text`), `:497` (`note_distill_request`) | A user correction becomes an action row. `spirit_distill_after_mine` default false | The strongest interactive signal triggers nothing |
| Campaign | `host/campaign.py:470-475` (`prepare_resume`), `:258-290` (`reflect`) | The daily tick fires when `last_reflect_date != today`, with observations `{"goal": …}` only | Calendar-driven, and blind to what happened since the last tick |
| Learnings | `memory/__init__.py:31` (`DECAY_DAYS = 30`), `:211` (`_effective_confidence`), `:1000` (`context_block`) | Append-only, latest-wins by key, top `context_learnings` (8) by decayed confidence | Decay is time-only, so a learning used every day ages like one never used |
| Use citations | `capabilities/tools.py:70`, `:1174` | `recall_memory` tells the model to say "Prior learning applied: <key>" | Emitted, never parsed: no usage or outcome telemetry |
| Turn traces | `harness/traces.py:63-106` | Per-turn spans: tool ok/fail, rounds, interrupted, read-only | Already holds the turn-level salience inputs |
| Long-run context | `run/until_runner.py:68-84` (`MAKER_PROMPT`) | Each maker cycle gets the goal, the budget, and the **prior handoff** only | A lesson from cycle 3 can be gone by cycle 8 |
| Procedural memory | `prompts/skills/` (packaged), `/improve` + `--gate harness-adopt` | Skills change only through human-gated harness edits | No per-project place for "in this repo, step 3 of `investigate` needs X" |

---

## 4. Design: learn on evidence, at boundaries, within a budget

**Principle.** A review runs because something happened, not because time or turns passed. It
runs at the next point where the user is not waiting on the model, and its input is the part of
the record that taught something.

### 4.1 Salience signals

Every signal below except citation outcomes (added in L0) is already recorded somewhere in
lmloop. Computing them costs no model call.

| Signal | Class | Source | Typical yield | Evidence tier |
|---|---|---|---|---|
| User correction (first token "no", "wrong", "don't", "undo", "stop") | Instant | `spirit.looks_like_correction` | `preference`, or a contradicted learning | User-stated |
| `/undo` of the last turn | Instant | `entry/repl.py:550` (`_cmd_undo`) | Same as a correction | User-stated |
| A destructive confirm gate denied | Instant | `DENIED:` tool results (`capabilities/tools.py:658`, `:823`) | `preference` (what not to run) | User-stated |
| Check or red test **fail → pass** after ≥ 2 attempts | Instant | `until` run events; plan red/green ledger | `pitfall` naming the red command and the mechanism | Verified |
| Abstain, infeasible, blocked, gate-no, or exhausted budget | Instant | `until_outcome_label`; plan and graph terminal rows | Spirit `thought` (hypothesis), never a learning | Unverified |
| Repeated-tool halt or forced final answer | Instant | `agent.py:170` (`note_tools` fingerprint repeat) | `pitfall` about the loop, or a thought | Observed |
| A cited learning followed by a correction or failed check | Instant | Citation parse (L0) + outcome | Fix or demote that learning | Verified-negative |
| Tool error → later success with the same tool | Accrual (+3) | `traces.note_tool` ok/fail sequence | `tool` / `operational` | Verified |
| Tool-calling round | Accrual (+1) | `traces.finish_turn(rounds=…)` | — | — |
| A check command not yet in the knowledge graph | Accrual (+2) | `record_workflow_run` commands (only when `use_graph` is on) | `tool` | Observed |
| Read-only or tool-free turn | Zero | `finish_turn(readonly=…, no_tools=…)` | — | — |
| Organic `remember` / `log_decision` in the turn | **Resets debt** (Hermes' rule) | Tool calls | Already saved | — |

**Learning debt** is the running sum of accrual weights since the last completed review or organic
save, kept per session and per run. A review is **due** when any instant signal fires or debt
reaches `LEARN_DEBT_THRESHOLD` (start at 20 and calibrate on L1 data). Hermes' two counters are the
special case "weight 1 per turn, threshold 10" and "weight 1 per tool round, threshold 10".

This fixes each counter failure in Section 2:

- A one-turn correction is due immediately (#58669, #18369).
- A fifty-round single prompt accrues through its tool rounds (#94226).
- A smooth run that passed on the first try accrues little, so it costs nothing, where today
  `until_mine` reviews it anyway.

### 4.2 Boundaries: where a due review runs

| Boundary | Seam | Why the user is not waiting |
|---|---|---|
| End of an `until` / `plan` / graph run, for any outcome | `flow/workflow_finish.py` (`finish_until_run`, `finish_graph_run`); a plan-runner equivalent | Autonomous runs are unattended; the review replaces today's unconditional `mine` role |
| `/new` | `repl._cmd_new` (`:285`) | The user chose to drop the context; harvest it first |
| `/compact` | `repl._cmd_compact` (`:569`) | Detail is about to be summarized away. This is Hermes' `on_pre_compress` seam (#31597) |
| Exit | `repl._maybe_mine_on_exit` (`:315`) | Shown as one line, "reviewing (user correction) — Ctrl-C to skip"; a skipped review stays queued |
| Idle prompt (optional, L5) | `entry/prompt.py:483` (`default_buffer.on_text_changed`) | Starts after a settle window; the first keystroke cancels it. lmloop's model client has no cooperative cancel (interrupts are Ctrl-C in the main thread), so this review runs as a child `lmloop memory mine --pending` process that the REPL terminates. Verify that LM Studio stops generating when the client disconnects |

Rules:

- **Never mid-turn.**
- **Never concurrently with a live model call** on a single-slot server (#54115).
- **Never inside `isolated_act` children or inside a review** (#141318).
- **Never on startup.** The user just arrived. Show "1 review pending (fail→pass in run …)" in the
  HUD instead.

### 4.3 Durable review queue

Store pending reviews in `~/.lmloop/projects/<slug>/review_queue.jsonl`, append-only and
latest-wins by key, matching the learnings store:

- **One item per session or run key**, carrying `reasons[]` (signal names), the source paths, the
  salient windows (4.4), `attempts`, and `enqueued_at`. A newer snapshot of the same key replaces
  the older one but keeps the original `enqueued_at`, as Hermes does.
- **A cancelled or skipped review stays pending**, with `attempts += 1`. It is dropped after 3
  attempts, with a `usage.record` row so the drop is visible (#145920 inverted).
- **Debt is cleared only when a review completes.**
- **Items carry no time-to-live.** A queue that waits for the next boundary needs none. Instead,
  `QUEUE_MAX_ITEMS` (for example 20) keeps the highest-salience items when the queue is full.
- **Draining.** Boundaries drain the queue. `lmloop memory mine --pending` and `/memory mine
  pending` drain it on demand, which serves cron and supervisor users.

### 4.4 The review itself

**Input: salient windows, not whole transcripts.** PAST-Bench is the reason (Section 2.1):
reviewing one verified-correct session beat reviewing the agent's own sessions, 56% to 33%.
A deterministic episode digest comes first:
outcome, cycles, check commands, and signal list. Then come windows anchored on each signal, with
a total cap of `REVIEW_INPUT_CHARS` (for example 12,000).

| Signal | Window |
|---|---|
| Fail → pass | The check command; the first failing output lines from the run event `results`; the maker handoff that turned it green; the tail of that maker session |
| Correction | The previous assistant reply (clipped), the correction, and the next reply |
| Tool error → recovery | The failing call and error, and the succeeding call |
| Abstain or blocked | The reason and the last eval handoff |
| Contradicted learning | The learning row, the citing reply, and the contradiction |

This replaces `format_sessions_for_retro`'s "20k characters × every session" input
(`memory/__init__.py:716`) and bounds review cost regardless of run length.

**Prompt.** Keep `retro.md`'s bar and add the Hermes blocks it lacks (Section 7). Route each
finding by evidence tier:

| Tier | Destination |
|---|---|
| Verified (fail→pass with a command, recovery) | `remember` as `pitfall`, `tool`, or `operational` |
| User-stated | `remember` as `preference` (no decay) |
| Unverified (abstain, unresolved) | A spirit thought, kind `hypothesis` — never a learning |
| Decision made | `log_decision` |
| Procedure for a loaded skill | Skill note (L4) |

**Tools, model, cache.**

- Keep `tool_only=tools.MINE_TOOLS` (`capabilities/tools.py:1098`) and the existing
  "`recall_memory` first" check (`entry/repl.py:422-429`).
- Pass `model_override=eval_model_name(cfg, model)` (`model/server.py:282`). When `eval_model` is
  empty that is the main model, which is already loaded in LM Studio, so there is no model swap.
  Keep that default: Hermes measured a cheaper reviewer missing signals (Section 2.1).
- For REPL boundary reviews there is an optional cache-parity variant: replay the live thread
  unchanged and append the review instruction as the last user message. llama.cpp can then reuse
  the slot's KV prefix, as Hermes does with provider caches. This only works if the tool schemas
  stay identical, with `MINE_TOOLS` enforced at dispatch rather than by dropping schemas. Measure
  time-to-first-token before adopting it, because the salient-window input is already small.

**Budget.** `REVIEW_MAX_ROUNDS` is 6 (Hermes allows 16; lmloop's reviews have no file tools).
Review input is capped as above. Record `usage.record("learn.review", trigger=…, writes=…,
tokens=…)` and report the **review tax**, review tokens divided by work tokens, in `lmloop flow`.

**Output.**

- One dim line, for example `learned: pitfall pytest-xdist-tmpdir · thought: hypothesis` or
  `review: nothing to save (env failure)`.
- Writes apply independently, so one invalid write does not drop the rest (#110689).
- Each row carries `source="review"` and the trigger, for provenance.

### 4.5 Long runs: lessons inside the run, review at the end

A long `until` run needs two things a session review does not provide:

1. **Run lessons (deterministic, inside the run).**
   - When a check flips fail → pass, append one line to a bounded "Lessons this run" block: the
     check, the first error line, and what fixed it (the first line of the maker handoff).
   - Pass the block to every later maker and eval prompt next to the prior handoff, capped at
     1,500 characters. The meta-control path already has a slot for it: `MAKER_PROMPT_CONTROL`
     carries a compact "Run state" block (`until_runner.py:86-107`).
   - It goes into the task text, not the frozen system prompt, so the prefix cache is untouched.
     This is the same move as Hermes' per-turn note channel (`skills_index_delta.py`).
   - It costs no model call. It fixes "only the last handoff survives" (`until_runner.py:68-84`).
   - It changes maker prompt text, so it ships behind `--gate harness-adopt`.
2. **One review at run end over the salient windows**, for every outcome:
   - **Pass:** learnings, routed as in 4.4.
   - **Fail or abstain:** a pitfall only when the run itself verified the cause; otherwise a
     thought (Hermes' "unresolved failures" rule).
   - **Pause:** stays queued until the run ends.

Campaigns are the longest runs. They get the same treatment in 4.8.

### 4.6 Closing the loop: outcome attribution

1. **Parse citations.** `PRIOR_LEARNING_APPLIED` (`capabilities/tools.py:70`) appears in assistant
   text. Parse it where the turn finishes (`harness/traces.py:106`) and append
   `{key, session, turn, ts}` to `learning_uses.jsonl`.
2. **Label the outcome** of the citing turn or run:
   - `held`: the next user turn is not a correction, or the run passed.
   - `contradicted`: the next turn is a correction, or the check failed on the cited step.
   - `unknown` otherwise.
3. **Effects.**
   - A `held` citation refreshes the decay anchor in `_effective_confidence`, so a used learning
     stops aging like an unused one.
   - A `contradicted` citation is an instant signal (4.1) that queues a review of that row.
   - Exposure without any citation over many sessions may **demote the row from injection** but
     never deletes it or hides it from `recall_memory` / FTS (#136030).
4. **North-star metric: repeat-mistake rate.**
   - Fingerprint failures deterministically: the check command plus a normalized first error line,
     and the tool name plus error class.
   - When a fingerprint recurs after a review already wrote a learning from it, count a repeat.
   - A falling repeat rate is the evidence the loop works. A rising rate after a `retro.md` change
     is a regression for `--gate harness-adopt` to catch.
   - Neither Hermes nor lmloop measures this today.

### 4.7 Curation on evidence, not weekly

Hermes' curator waits for a 7-day interval and 2 idle hours. lmloop should curate when the store
needs it:

- **Triggers:**
  - More than `CURATE_AFTER_WRITES` (for example 25) new learnings since the last curation.
  - Injection pressure: eligible rows above 3 × `context_learnings`.
  - A contradiction cluster, meaning two rows citing the same fingerprint with conflicting advice.
- **Deterministic pass first:**
  - Merge rows with identical insight text under different keys.
  - Demote never-cited, decayed rows from injection.
  - Never touch `user-stated` rows.
- **The model pass stays manual**, through the existing `/memory audit` and `/memory reconcile`
  (`entry/repl.py:346-349`), and the HUD suggests it when triggers fire. This matches Hermes'
  default (`DEFAULT_CONSOLIDATE = False`) and the curator bugs in its tracker (#79295, #144026,
  #83580).

### 4.8 Campaigns and spirit: remove the calendar trigger

**Campaign (`host/campaign.py:470-475`).** Replace "`last_reflect_date != today`" with an evidence
tick. Synthesizer reflection runs when there is:

- a `blocked` tick, or an abstain or failure streak, which needs action now rather than tomorrow;
- a milestone; or
- `REFLECT_AFTER_RUNS` (for example 3) `run_end` ticks since the last synthesizer pass.

The observations become those ticks and outcomes instead of `{"goal": …}`. With no activity there
is no reflection. The daily digest file stays as a rendered view. `campaign_daily_reflect` keeps
its meaning as the on/off switch for synthesizer reflection.

**Spirit.** Queue a distill when `OPEN_THOUGHTS` (3, `spirit/__init__.py:19`) open thoughts share
a kind, or when corrections repeat in the same area. Applying a distill to `self.md` stays
human-gated (`/spirit distill --patch`).

### 4.9 Procedural memory: per-project skill notes (L4)

Hermes' strongest structural idea is that task lessons load only with the task. lmloop injects 8
learnings into every prompt of a small local context, and that is the scarce resource. Notes
attach to skills that already load, which avoids Hermes' discovery problem: new auto-created
skills that are never loaded when needed (#66180).

- **Store:** `~/.lmloop/projects/<slug>/skill_notes/<skill>.jsonl`, append-only and latest-wins by
  key, rendered as a `## Learned in this project` section after the packaged body whenever
  `read_skill(<skill>)` loads it.
- **Packaged skills stay immutable.** Changing them is still a harness edit through `/improve` and
  `--gate harness-adopt`.
- **Relevance gate (#66350).** A review may add a note only for a skill loaded inside its salient
  window, preferring to update an existing note (Hermes' "patch the loaded skill first").
- **Read before write.** Add `read_skill` to the review's tool set when notes ship, and refuse a
  note write for a skill the review has not read. Hermes learned that a write guard without the
  matching read tool starves the loop.
- **Budget.** Each skill's notes are capped (for example 1,500 characters). Over the cap, the write
  is rejected with the current notes shown, using Hermes' "IF FULL" consolidation semantics.
- **Safety.** Notes load as trusted instructions, so they need stronger rails than fenced
  learnings:
  - Writes are staged for approval by default (Hermes ships staging off, and #70128 is the
    result).
  - Every write is scanned with `spirit.POISON_PHRASES` plus an invisible-Unicode check.
  - Notes may be written only from local evidence (tool results), never from web content.
- **Graduation.** A note that held across several runs becomes an `/improve` candidate for folding
  into the packaged skill. That connects per-project learning to harness learning (H1–H3 in
  [FINDINGS_HARNESS_LEARNING.md](FINDINGS_HARNESS_LEARNING.md)).

---

## 5. Hook map

| Seam | Today | Proposed | Model calls added |
|---|---|---|---|
| `harness/traces.py:106` `finish_turn` | Writes the turn span | Score the turn (4.1); parse citations (4.6) | 0 |
| `entry/repl.py:217-265` `_run_turn` | Act, footer, HUD | Add the turn score to session debt; enqueue when due; the HUD shows the pending reason instead of the static ≥ 8-turn hint (`:307-311`) | 0 |
| `spirit.note_user_text` (`spirit/__init__.py:169`) | Correction → action row | Also an instant signal | 0 |
| `flow/workflow_finish.py` | Usage + knowledge-graph rows for every outcome | Score the run from `run.events`; enqueue for every outcome | 0 |
| `run/until_runner.py:336-363` `next_role`, `:1470-1478` | Mine after every pass | Mine only when the run is salient; other terminal outcomes take the failure-tier review | 0 on happy paths (saves one today) |
| `run/until_runner.py:68-84` maker prompts | Prior handoff only | Add the bounded "Lessons this run" block (4.5) | 0 |
| `run/plan_runner.py` | No learning | Red→green and blocking findings feed the same ledger | 0 to enqueue; 1 review at the end |
| `repl._cmd_new` / `_cmd_compact` / `_maybe_mine_on_exit` | Discard / summarize / optional mine | Drain the queue first, cancellable with Ctrl-C | 1 per due review |
| `entry/repl.py:433-476` `mine_sessions` | Full-transcript retro, main model | Salient windows, `eval_model_name`, independent writes, provenance | Same or fewer tokens |
| `host/campaign.py:470-475` | Calendar tick | Evidence tick (4.8) | Fewer |
| `entry/prompt.py:483` | Clears the exit hint | (L5) Cancels an idle review on the first keystroke | 0 |

Module placement follows [DEVELOPMENT.md](../DEVELOPMENT.md):

- **`memory/salience.py`** (new leaf: stdlib, `memory`, `config`): signal scoring, debt, the queue,
  failure fingerprints, and the window builder. It must not import `agent`, `loop`, or `graph`.
- **`entry/repl.py`**: the review runner stays next to `mine_sessions`.
- **`until` / graph**: they keep receiving a `mine` callback, so the review runner adds no new
  import edge.

---

## 6. Efficiency on a single-slot local server

| Lever | Effect |
|---|---|
| Skip happy paths | Most turns and many runs score below threshold, so they cost zero. Today every passing `until` run pays a full retro |
| Salient windows | Review input is bounded by `REVIEW_INPUT_CHARS`, not by run length |
| Boundaries only | No contention with a live turn (#54115); no second slot |
| Same loaded model by default | `eval_model_name` falls back to the main model, so there is no LM Studio model swap |
| Organic-save reset | No review just to find duplicates |
| Deterministic first | Run lessons, fingerprints, attribution, and the curator's first pass cost no model call |
| Cache parity (optional) | REPL reviews can reuse the slot's KV prefix; adopt only if measured |

This meets the roadmap's bar ("every feature must be latency-neutral on a single-slot laptop"): the
hot path gains only deterministic bookkeeping, and model work moves to points where nobody is
waiting.

---

## 7. Quality rules to port into `retro.md`

`retro.md` already requires that a pitfall names its command, that preferences be explicit, and
that `recall_memory` comes first. Add the following (as a harness change, through `--gate
harness-adopt`):

1. **Ground every fact** in a user message or tool result you can point to. Never infer
   preferences from tone or topic. A one-off request is not a standing preference unless the user
   said "always", "from now on", or "remember".
2. **Do not capture:**
   - environment failures (lmloop already classifies exit codes 126/127 as `blocked`);
   - negative claims about tools;
   - transient errors that resolved (save the retry pattern instead);
   - one-off narratives;
   - **unresolved failures**, which become a thought, never a workflow.
3. **One fact, one store.** Update the existing key to correct it (latest-wins), rather than
   adding a second row or an "UPDATE:" line.
4. **Lessons, not logs.** An imperative rule plus one clause of why; no PR numbers, dates, or
   quoted narration.
5. **Do not restate** what `system.md`, `AGENTS.md`, or a tool schema already teaches.
6. **Scope corrections.** Save a correction for the task, command, or file it was about, not as a
   global prohibition (#88429). A `preference` never decays in lmloop, so an over-broad one is
   permanent until attribution (4.6) reopens it.
7. **Route by evidence tier** (4.4). An empty review is a valid result. Unlike Hermes, lmloop
   should not push toward writing; the trigger already guarantees there is evidence to look at.

---

## 8. Evaluation plan

1. **Contract tests** using the scripted model and `tests/e2e_harness.py` stub:
   - the trigger matrix (each signal enqueues or not);
   - debt reset on organic saves and on completion only;
   - queue coalescing, cancel-stays-pending, and the attempts cap;
   - the per-store kill switch checked at dispatch (#82708);
   - no review inside children;
   - window caps;
   - happy-path skip.
2. **Golden review fixtures.** Each fixture is a set of salient windows paired with the expected
   tool calls, asserted with the existing Layer 2 contracts and Layer 3 trajectory checks from
   [archived/DESIGN_ASSERTION_DRIVEN_EVALS.md](archived/DESIGN_ASSERTION_DRIVEN_EVALS.md)
   (`recall_memory` first, call types, forbidden tokens). Live-model runs go through the usage-eval
   path; CI replays recorded trajectories with no judge model and no network. Cases:
   - correction → `preference`;
   - "command not found" → nothing saved;
   - fail→pass → `pitfall` naming the red command;
   - unresolved failure → thought, not learning;
   - a known key → updated, not duplicated.
3. **Online metrics** in `usage.jsonl` and `lmloop flow`: review yield (reviews with ≥ 1 write),
   review tax, citation rate, contradiction rate, and **repeat-mistake rate**.
4. **Gate.** Any `retro.md` or maker-prompt change carries a harness fingerprint (H1) and must pass
   `--gate harness-adopt` with no rise in repeat-mistake rate on the held-out split (H3).

---

## 9. Phased plan

| Phase | Deliverable | Default behavior change | Ship when |
|---|---|---|---|
| **L0** | Route mining through `eval_model_name`; cap `format_sessions_for_retro` in total; `learning_uses.jsonl`; failure fingerprints; `learn.review` / `mine` usage rows | None (`eval_model` empty = today) | Now; these are measurement and a defect fix |
| **L1** | `memory/salience.py`: scoring, debt, durable queue, windows; HUD shows the pending reason; `/memory mine pending` and `lmloop memory mine --pending` | None automatic; the queue fills, and the user drains it | After L0. Calibrate the constants once trigger rates cover enough real use (for example 50 REPL sessions and 20 autonomous runs) |
| **L2** | Boundary reviews (run end for all outcomes, `/new`, `/compact`, exit); salient-window input; Section 7 rules; failure-tier routing; "Lessons this run" | Yes: salience-gated review replaces unconditional `until_mine` / `graph_mine` | L1 data shows a review tax under 10% and golden fixtures pass `--gate harness-adopt` |
| **L3** | Attribution effects: held refreshes decay; contradicted triggers review; demote from injection | Ranking inputs only | L0 citation rate is high enough to be meaningful |
| **L4** | Per-project skill notes with staging, scan, cap, and graduation to `/improve` | Notes render only after approval | L2 is stable; reviews keep producing task-specific procedures that do not fit a one-line learning |
| **L5** | Campaign evidence tick; spirit distill queueing; optional idle-prompt review (cancel on keystroke); optional cache-parity replay | Campaign reflection timing | Campaign users report daily ticks with nothing to say, or blocked campaigns waiting for the next day |

**Config.** One user-facing key replaces four:

| Key | Values | Replaces |
|---|---|---|
| `auto_learn` | `off` · `queue` · `boundary` (default from L2) | `until_mine`, `graph_mine`, `mine_on_exit`, `spirit_distill_after_mine` |

During migration, the old keys keep working as overrides and are removed after a deprecation
window, with README rows updated in the same diff. Weights and thresholds are named constants:
`LEARN_DEBT_THRESHOLD`, `REVIEW_INPUT_CHARS`, `REVIEW_MAX_ROUNDS`, `QUEUE_MAX_ITEMS`,
`CURATE_AFTER_WRITES`, `REFLECT_AFTER_RUNS`, and the skill-note cap. Per the roadmap's budget, none
of these differs enough between users to earn a key.

---

## 10. Non-goals

| Item | Why not | Revisit when |
|---|---|---|
| Hermes-style eager writing ("Be ACTIVE") | Contradicts lmloop's curated memory; evidence triggers already target reviews where they matter | Golden fixtures show salient reviews missing real lessons |
| Background review thread concurrent with live turns | Single slot: halves speed or exhausts `n_ctx` (#54115) | `model_concurrency` > 1 on a remote endpoint |
| Automatic deletion or archival of learnings | Telemetry is incomplete (#136030); recall stays the safety net | Never for deletion; archival only with attribution data |
| Autonomous creation of packaged skills | Harness changes are human-gated and eval-gated | Skill-note graduation proves out (L4) |
| LLM consolidation by default | Hermes ships it off, and its tracker has open consolidation and restore bugs (#144026, #83580) | Deterministic curation leaves visible clutter in `/memory audit` |
| User modeling service (Honcho-like), trajectories and RL | Out of scope for a local single-user CLI | A remote multi-user deployment |
| Calendar or interval reflection of any kind | The point of this document | — |

---

## Appendix: Hermes source map

| Topic | Files |
|---|---|
| Review fork, prompts, budgets, cancel | `agent/background_review.py` |
| Idle queue | `agent/review_idle_queue.py`; `run_agent.py:172`, `:752-846` |
| Triggers and resets | `agent/turn_context.py:757-772`; `agent/turn_iteration_prep.py:167`; `agent/tool_executor.py:737-739`; `agent/turn_finalizer.py:591-616`, `:863-873` |
| Memory store, limits, frozen snapshot | `tools/memory_tool.py`, `tools/memory_tool_store.py` |
| Threat scan | `tools/threat_patterns.py` |
| Skills tool, guards, provenance, ledger | `tools/skill_manager_tool.py`, `tools/skill_manager_guards.py`, `tools/skill_provenance.py`, `tools/skill_ledger.py` |
| Skill index delta (cache-safe mid-session changes) | `agent/skills_index_delta.py` |
| Usage telemetry | `tools/skill_usage.py` |
| Curator | `agent/curator.py`, `agent/curator_backup.py`, `website/docs/user-guide/features/curator.md` |
| Session search | `tools/session_search_tool.py` |
| `/learn`, journey | `agent/learn_prompt.py`, `agent/learning_graph.py`, `agent/learning_mutations.py` |
| Verify-on-stop | `agent/turn_stop_gates.py`, `agent/verification_stop.py`, `agent/verify_hooks.py` |
| Hooks | `hermes_cli/plugins.py:109-204` (`VALID_HOOKS`) |
| Tests | `tests/agent/test_background_review_*.py`, `tests/agent/test_curator_*.py`, `tests/agent/test_learning_graph.py`, `tests/agent/test_learn_prompt.py` |
