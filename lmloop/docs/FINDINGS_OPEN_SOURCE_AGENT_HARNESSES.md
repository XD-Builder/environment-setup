# Findings: Open-Source Agentic AI Harnesses & Prompt Evolution

**Date:** 2026-10-09  
**Scope:** Top 30 popular open-source agentic AI projects (GitHub popularity + ecosystem influence, Oct 2026)  
**Focus:** Harness design (especially **prompts**), **harness evolution** loops, and patterns that yield the highest **performance ROI** per token and per engineering hour  
**Audience lenses:** CTO / Distinguished Engineer · Chief Scientist / AI Architect · General AGI resolution (deep reflection + output framework)  
**Companions:** [DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md) ·
[DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md](DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md) ·
[DESIGN_EVOLVING_SPIRIT.md](DESIGN_EVOLVING_SPIRIT.md)

---

## Executive summary

1. **“Harness” has converged on a layered stack**, not a single system prompt: static core instructions + dynamic context + tool/ACI contracts + memory/skills injection + control logic (graphs, condensers, critics) + optional sub-agents. Projects that win on benchmarks usually optimize **interfaces** (SWE-agent ACI, Deep Agents subagents, OpenHands static/dynamic split) as much as raw model choice.

2. **The highest ROI prompt content is procedural and negative** (“act don’t narrate,” “one command per step,” “cite evidence,” “minimal diff,” “do not modify tests”) rather than persona fluff. Role/goal/backstory (CrewAI, MetaGPT) helps multi-agent **routing** and human UX; for single-agent coding agents, **tool discipline + verification loops** dominate leaderboard gains.

3. **Harness evolution is splitting into three mature tracks:**
   - **Runtime adaptation** (skills, microagents, AGENTS.md, path-triggered rules) — cheap, human-auditable.
   - **Trajectory-driven optimization** (DSPy/GEPA, dspyground, Life-Harness, HarnessX, LangSmith evals, OpenHands Quality Flywheel via ADK+GEPA) — expensive offline, strongest when tied to **metrics**.
   - **Co-evolution** (environment/task mutation + harness search + failure diagnosis) — research frontier; aligns with paired feasible/infeasible tasks and abstention-aware selection (see §2).

4. **For the three personas you named**, the same skeleton harness works; what changes is **depth of reflection**, **output schema**, and **evidence bar** — not a different product per persona.

5. **lmloop** already sits in the “lean harness + skills + memory + eval loop” quadrant (`skills/system.md`, persona skills like `/ceo`, `/retro`, `DESIGN_EVOLVING_SPIRIT.md`, `DESIGN_USAGE_EVALS_AND_SELF_IMPROVEMENT.md`). The gap vs frontier is less “more prompt” and more **closed-loop harness evolution** (trajectory → gap → prompt/skill patch → regression eval).

---

## 1. Methodology

### 1.1 Selection criteria

| Criterion | Rule |
|-----------|------|
| Open source | Public repo with OSS license |
| Agentic | Tool use, multi-step autonomy, or explicit agent orchestration (not pure RAG UI) |
| Popularity | GitHub stars (Oct 2026 API snapshot) plus category leadership when stars mislead (e.g. AutoGen maintenance mode) |
| Harness signal | Documented prompts, templates, skills, or evolution tooling |

Star counts are **attention proxies**, not quality. Maintenance status and release velocity matter (e.g. AutoGen → Microsoft Agent Framework; Aider release cadence vs OpenHands).

### 1.2 Reference architecture (co-evolution)

Your diagram frames the research target: **environment pool** and **harness** co-evolve from **paired rollouts** and **failure diagnosis** \(F_k\). In industry repos, partial implementations exist:

| Diagram component | Industry analog |
|-------------------|-----------------|
| Harness \(H_k\): prompts, memory, sub-agents, skills, tools, control | Deep Agents, OpenHands, lmloop skills, LangGraph state |
| Failure diagnosis \(F_k\) | LangSmith trajectories, SWE-agent review-on-submit, lmloop `usage.jsonl` + `/retro` |
| Harness optimizer | GEPA/DSPy, dspyground, Life-Harness layers, HarnessX (WIP meta-search) |
| Environment designer | SWE-bench, GAIA, TerminalBench, MetaGPT SOP tasks, synthetic data flywheels |

---

## 2. Harness taxonomy (cross-project)

Use this vocabulary when comparing repos:

| Layer | Purpose | Typical artifacts | Evolution lever |
|-------|---------|-------------------|-----------------|
| **L0 — Core identity** | Stable behavior contract | `system.md`, `instructions`, YAML `system_template` | GEPA on system prompt; A/B via evals |
| **L1 — Task framing** | Per-instance goal | `instance_template`, PR description blocks, user task | Template variables; few-shot in template |
| **L2 — Tool / ACI contract** | How actions are expressed | Function calling, DISCUSSION+COMMAND, Thought/Code/Observation | Tool bundles (SWE-agent); `ToolDef` (lmloop) |
| **L3 — Context engineering** | What enters each turn | Condenser, file map (Aider), subagent isolation (Deep Agents) | Dynamic `@dynamic_prompt`, path rules |
| **L4 — Skills / SOP** | On-demand playbooks | `SKILL.md`, Crew tasks, MetaGPT Actions | Authoring + `/retro` promotion; keyword/path triggers |
| **L5 — Memory** | Cross-session state | mem0, Letta, lmloop learnings/decisions, Crew memory | Decay, confidence, spirit/self (lmloop design) |
| **L6 — Control logic** | When to stop, branch, escalate | LangGraph, Crew Flows, critic, max rounds, intent nudge (lmloop) | Graph edits; gap-driven threshold tuning |
| **L7 — Evolution loop** | Improve harness from data | Evals, GEPA, flywheel | **Highest long-term ROI** when automated |

---

## 3. Top 30 projects — harness profile matrix

Stars ≈ GitHub `stargazers_count` (2026-10-09). “Harness emphasis” is qualitative.

| # | Project | Stars ≈ | Primary harness shape | Prompt / template highlight | Evolution / improvement path |
|---|---------|---------|------------------------|------------------------------|------------------------------|
| 1 | **Langflow** | 155k | Visual agent/workflow builder | Node-level prompts; less single canonical harness | Export + manual iteration |
| 2 | **Dify** | 158k | App + agent orchestration platform | Workflow prompts, RAG + tool nodes | Ops analytics; template versioning in product |
| 3 | **AutoGPT** | 187k | Autonomous goal loops (historical) | Goal + constraint prompts | Community forks; high star/debt ratio |
| 4 | **Claude Code** | 150k | Productized coding agent | Strong built-in tool + permission harness (repo not fully prompt-transparent) | Anthropic iteration; `.claude` project rules |
| 5 | **LangChain** | 147k | Composable chains/agents | `create_agent`, middleware, provider templates | LangSmith evals + prompt hub patterns |
| 6 | **OpenAI Codex CLI** | 128k | Terminal coding agent | CLI system + project context | OpenAI release iteration |
| 7 | **browser-use** | 117k | Browser automation agent | Task + DOM/action prompt | Trajectory logs; community prompts |
| 8 | **Gemini CLI** | 107k | Google terminal agent | Built-in tool loop | Google model + CLI updates |
| 9 | **MCP servers** | 91k | Tool transport layer | Tool **descriptions** = micro-prompts | GEPA MCP adapter optimizes descriptions |
| 10 | **RAGFlow** | 92k | RAG + agent workflows | Retrieval-conditioned prompts | Dataset/eval driven |
| 11 | **OpenHands** | 90k | Sandbox coding agent | **Static** system template + **dynamic** context block (cache-friendly) | Skills/microagents; condenser; critic |
| 12 | **mem0** | 67k | Memory layer for agents | Memory injection prompts | Memory graph evolution |
| 13 | **MetaGPT** | 71k | SOP multi-agent company | Role profile + constraints + Action `PROMPT_TEMPLATE` | **Self-referential constraint prompt updates** from handover feedback |
| 14 | **Cline** | 70k | IDE agent | Plan/act modes; rules files | User rules; checkpointing |
| 15 | **CrewAI** | 60k | Role-based crews | **Role / goal / backstory** + optional `system_template` | Custom templates; Flows; hosted harness runtime |
| 16 | **LiteLLM** | 60k | Model router (adjacent) | Proxy-level prompt logging | Cost/latency optimization more than prompt |
| 17 | **AutoGen** | 61k | Conversational multi-agent | Agent `system_message` per participant | Maintenance mode; use **Agent Framework** |
| 18 | **LlamaIndex** | 52k | Workflows + agents | Workflow event prompts | LlamaIndex eval patterns |
| 19 | **Aider** | 49k | Git-native pair programmer | **Repo map** + edit format in system context | Architect mode; user `.aider.conf` |
| 20 | **Agno** | 43k | Lightweight agent framework | Agent instructions + toolkit | Fast prototyping |
| 21 | **LangGraph** | 43k | Graph orchestration (low-level) | Prompts live in nodes, not framework | Checkpoint + replay for diagnosis |
| 22 | **Continue** | 36k | IDE assistant | Configurable rules + prompts | dev-only iteration |
| 23 | **Deep Agents** | 30k | **Opinionated harness** on LangGraph | **Layered** system prompt: base + user + memory + skills + FS + subagents | Middleware; AGENTS.md; skills dirs |
| 24 | **OpenAI Agents SDK** | 30k | Handoff-centric multi-agent | `instructions` + **`RECOMMENDED_PROMPT_PREFIX`** for handoffs | Trace-driven refinement |
| 25 | **smolagents** | 30k | Code-as-tool-call loop | YAML **Thought / Code / Observation** + planning templates | `prompt_templates` override |
| 26 | **Semantic Kernel** | 29k | Enterprise plugins + planners | Semantic functions as prompts | Microsoft stack integration |
| 27 | **Letta** | 25k | Stateful agents (memory OS) | Core memory blocks in prompt | Memory editing policies |
| 28 | **Google ADK** | 22k | GCP agent dev kit | Agent instruction + tool defs | **`adk optimize` (GEPA)** quality flywheel |
| 29 | **SWE-agent** | 21k | Research **ACI** coding agent | YAML templates: system / instance / next_step | Tool bundles; **review_on_submit** |
| 30 | **pydantic-ai** | 21k | Typed agent framework | System prompt + structured output | Eval + prompt iteration in app code |

**Honorable mentions (harness evolution research):** DSPy/GEPA, dspyground, Life-Harness (arxiv 2605.22166), HarnessX, Microsoft Agent Framework (~14k), CAMEL (~18k), RD-Agent (~15k).

---

## 4. Deep dives — patterns that move benchmarks

### 4.1 Agent–Computer Interface (ACI) beats longer prompts

**SWE-agent** (Princeton) showed that **designing the action surface**—file viewer window, structured edit, search tools, single-command steps, env vars to kill pagers—often beats swapping models. Prompts are split:

- `system_template` — generic assistant + computer use
- `instance_template` — PR description, **minimal non-test changes**, numbered workflow (repro → fix → verify → edge cases)
- `next_step_template` — observation wrapping

Legacy ACI used explicit **DISCUSSION** + one **command** block; modern configs use **function calling** with tool bundles (`registry`, `edit_anthropic`, `review_on_submit_m`).

**Takeaway (CTO):** Budget engineering time for **tool grammar + observation format** before prompt tuning.  
**Takeaway (Scientist):** Treat ACI as an **identifiability** problem: reduce ambiguous action parses to shrink \(F_k\) entropy.

### 4.2 Static vs dynamic system prompt (cache + correctness)

**OpenHands** separates:

- `static_system_message` — cacheable across conversations  
- `dynamic_context` — repo, skills, secrets, runtime (second block, no cache marker)

Skills evolved: **microagents** → **Agent Skills** standard (`.agents/skills/*/SKILL.md`), with **keyword**, **task**, and **path** triggers; path rules inject into tool observations (`<EXTRA_INFO>`), not the user message.

**Takeaway:** Highest ROI for production cost is **prompt caching architecture**, not shorter prose.

### 4.3 Layered harness (Deep Agents)

Deep Agents document explicit **context engineering order**:

1. User `system_prompt` (prepended)  
2. Base harness prompt (planning, verify, filesystem)  
3. Memory (`AGENTS.md`)  
4. Skills catalog (metadata first; full body on invoke)  
5. Virtual FS + execute guidance  
6. Subagent `task` tool instructions  
7. Custom middleware prompts  

Subagents exist primarily for **context quarantine**—main agent sees **final** subagent result, not tool spam.

**Takeaway:** Subagents are a **compression** mechanism; evolution should optimize **delegation boundaries**, not add more top-level prose.

### 4.4 Role–goal–backstory (CrewAI) — when it pays

CrewAI’s default injects formatting/behavior beyond your templates unless you override with explicit `system_template` / `prompt_template`. The framework’s own guidance: **specific roles**, outcome-focused goals, backstory that encodes **working style and quality bar**.

**ROI:** High for **human-readable multi-agent demos** and **delegation clarity**; lower for single-agent SWE if L2–L3 layers are weak.

**Anti-pattern:** Long backstory without `expected_output` on tasks → pretty logs, vague artifacts.

### 4.5 SOP-as-code (MetaGPT) + constraint evolution

MetaGPT encodes **Standard Operating Procedures** as Role → Action graphs with shared message pool. Research describes **recursive constraint prompt modification** from handover feedback stored in long-term memory—early **harness evolution** without weight updates.

**Takeaway:** SOPs are **prompt programs**; evolution targets **constraint paragraphs** per role, not the entire stack.

### 4.6 Code-loop vs tool-call-loop (smolagents)

**smolagents** `CodeAgent` uses YAML templates for **Thought / Code / Observation**, embeds tools as Python-callable functions in prompt, optional planning intervals, and `instructions` inserted into system prompt.

**Takeaway:** Code-loop harnesses improve **compositional tool use**; evolution via `prompt_templates["system_prompt"]` is straightforward but needs **sandbox safety** tests.

### 4.7 Handoff prefix (OpenAI Agents SDK)

Minimal, high-leverage multi-agent prompt addition:

```text
# System context
You are part of a multi-agent system ... Handoffs ... transfer_to_<agent_name> ...
Transfers ... in the background; do not mention ... transfers ...
```

**Takeaway:** ~150 tokens that prevent a class of **user-visible routing leaks**—cheap win.

### 4.8 lmloop (this repo) — harness snapshot

| Component | Implementation |
|-----------|----------------|
| L0 | `skills/system.md` — act don’t narrate, edit discipline, web/memory rules |
| L4 | Slash skills (`/ceo`, `/retro`, `/investigate`, …) — procedural playbooks |
| L6 | Gather/answer loop, duplicate tool-set guard, intent-prefix nudge, context pressure |
| L5 | `remember`, `log_decision`, checkpoint recovery in system context |
| L7 (partial) | `usage.jsonl`, `lmloop eval`, gap → `DESIGN_*.md`; `/retro` mines sessions |
| L7 (proposed) | `DESIGN_EVOLVING_SPIRIT.md` — actions → thoughts → knowledge → **self** |

lmloop’s `/ceo` skill is already a **CTO-grade review harness** (alternatives, scope modes, review passes). `/retro` is **failure-diagnosis → memory** without resuming work—aligned with \(F_k\) → harness/memory updates.

---

## 5. Harness evolution — landscape

### 5.1 Manual / org-process (still default)

- Project rules: `.cursor/rules`, `AGENTS.md`, OpenHands `repo.md`, Claude project instructions  
- PR review of prompt changes with benchmark suites (SWE-bench, internal evals)

**ROI:** Best when traffic is low; does not scale.

### 5.2 Trajectory optimizers (strongest automated prompt ROI)

| System | What evolves | Mechanism |
|--------|--------------|-----------|
| **GEPA / DSPy** | Signatures, instructions, full programs | Reflection LM + Pareto archive; **67%→93%** MATH cited for full program evolution |
| **dspyground** | System prompt for AI SDK agents | Multi-metric: tone, accuracy, efficiency, tool accuracy, guardrails |
| **Life-Harness** | 4 layers: env contract, skill injection (BM25), action realization, trajectory regulation | DSPy modules + GEPA/MIPROv2 |
| **HarnessX** | Processor pipeline (9 dimensions) | Trajectory reward + search (meta-evolution WIP) |
| **Google ADK `optimize`** | Agent instructions + tools | GEPA in quality flywheel |
| **LangSmith** | Prompts in production graphs | Eval datasets + regression |

**Efficient prompt evolution recipe ( distilled ):**

1. Fix **metric** and **dataset** (50–200 real failures beats 10k synthetic).  
2. Log **trajectories** (tool args, observations, final outcome).  
3. Run **reflective mutation** on **one layer at a time** (system prompt before rewriting control flow).  
4. **Pareto-select** on accuracy vs token cost vs tool error rate.  
5. Ship winning prompt to **git** (skill markdown / template YAML), lock with **regression eval**.

### 5.3 Co-evolution (research → product)

Pair **feasible** and **infeasible** tasks; optimize harness for **correct execution** and **correct abstention**. Life-Harness and HarnessX explicitly separate **harness evolution** vs **model evolution** (SFT/RL on annotated trajectories).

**Chief Scientist view:** This is the right object-level target for AGI scaffolding—not monolithic prompt soup.

---

## 6. What provides the most valuable performance improvements?

Ranked by **empirical leverage** across projects (coding agents + general agents):

| Rank | Intervention | Typical gain | Cost |
|------|--------------|--------------|------|
| 1 | **ACI / tool design** (observation shape, single-action rule, review gate) | Large on SWE-bench class tasks | Eng weeks |
| 2 | **Verification loop in prompt** (repro script, rerun tests, cite file:line) | Large | Prompt + tools |
| 3 | **Context quarantine** (subagents, condenser, static/dynamic split) | Medium–large on long horizons | Architecture |
| 4 | **Path/keyword skills** (inject only when relevant) | Medium on domain repos | Authoring |
| 5 | **Negative constraints** (“don’t narrate”, “don’t guess URLs”, “minimal diff”) | Medium | Low |
| 6 | **Role/backstory** (multi-agent) | Medium for delegation | Low–medium |
| 7 | **Automated prompt evolution (GEPA)** | Medium–large on fixed benchmarks | Compute + eval design |
| 8 | **Persona prose** without procedures | Low / negative | Low |

**Distinguished Engineer heuristic:** If a harness change does not alter **tool traces** on a fixed eval set, assume placebo until proven.

---

## 7. Three persona harness kits

Each kit uses the **same lmloop-style stack** (L0–L7) with different **skill invocation** and **output contract**.

### 7.1 CTO / Distinguished Software Engineer

**Job:** Decision quality, risk surface, shipping tradeoffs, architectural integrity.

**Invoke:** `/ceo` (+ optional `/review` for code-centric passes).

**L0 additions (minimal tokens):**

- “You optimize for ** reversible decisions**, **blast radius**, and **operational ownership**.”
- “Every recommendation names **tradeoff**, **default**, and **rollback**.”

**Output schema (mandatory sections):**

```markdown
## Decision (one line)
## Context (≤5 bullets)
## Options (2–3): effort / risk / reversibility
## Recommendation + why
## Landmines (ranked)
## Scope changes (explicit opt-in list)
## Open questions (max 3)
```

**Evolution:** Log `log_decision` entries from `/ceo` outputs; `/retro` promotes recurring landmines into learnings; spirit layer (`DESIGN_EVOLVING_SPIRIT`) stores **heuristics** (“always check migration rollback”).

**Benchmark:** Reduce **silent scope expansion** and **missing rollback plans** in sampled `/ceo` transcripts (human rubric 1–5).

---

### 7.2 Chief Scientist / AI Architect Researcher

**Job:** Hypothesis clarity, falsifiability, related work, experiment design, honest limits.

**Invoke:** custom skill (recommended name: `/research`) modeled on `/investigate` + stricter epistemics.

**L0 additions:**

- “Distinguish **observation**, **interpretation**, **prediction**.”
- “Prefer **mechanism** over benchmark leaderboard claims.”
- “Cite primary sources; mark **speculative** chains explicitly.”

**Output schema — IRIS framework:**

| Block | Content |
|-------|---------|
| **I — Issue** | Precise question; why it matters |
| **R — Related** | Prior art (papers, repos); what is already known |
| **I — Intervention** | Proposed method, harness change, or architecture |
| **S — Study** | Metrics, baselines, ablations, failure taxonomy |

Plus **Failure Diagnosis table** aligned with co-evolution:

| Symptom | Likely layer (L0–L7) | Test | Fix candidate |
|---------|----------------------|------|---------------|

**Evolution:** Pair eval tasks (feasible/infeasible); optimize prompts with GEPA on **metric + calibration** (abstention). Store promoted patterns in skills, not ad-hoc chat.

**Benchmark:** Calibration error + task success on held-out research QA set.

---

### 7.3 General AGI question resolution (deep reflection + thought framework)

**Job:** Integrate technical, strategic, and philosophical dimensions without losing rigor; produce **actionable** conclusions.

**Invoke:** `/investigate` or dedicated `/resolve` skill with **multi-pass** structure (do not single-shot).

**Thought framework — REFLECT-6 (run in order, skip only if N/A):**

1. **Reframe** — Restate question; list hidden assumptions.  
2. **Evidence** — What would change your mind? Fetch/read before claiming.  
3. **Frames** — CTO / Scientist / User impact lenses (1 paragraph each).  
4. **Limits** — What current agents **cannot** do; avoid anthropomorphic shortcuts.  
5. **Converge** — Synthesis with confidence **0–100%** per major claim.  
6. **Trace** — Next actions: experiments, decisions, or **explicit defer**.

**Output schema:**

```markdown
## Question (reframed)
## Short answer (≤120 words)
## Confidence table
| Claim | Confidence | Key evidence |
## Deep analysis (by REFLECT-6)
## Implications (build / buy / wait)
## Harness recommendations (if any)
## Open problems
```

**Anti-patterns to block in L0:**

- Tool-less speculation on factual subquestions.  
- Unified “AGI will …” without time-bounded, testable predictions.

**Evolution:** `/retro` extracts **which REFLECT steps were skipped** when users score outcomes low → patch skill.

---

## 8. Cross-persona “minimum viable elite harness”

If you only ship **one** system prompt upgrade, merge these **≤40 line** principles (already mostly in lmloop `system.md`):

1. **Act, don’t narrate** — tool call same turn as intent.  
2. **Ground claims** — file, command output, or URL.  
3. **One step per turn** when using fragile ACIs.  
4. **Minimal change bias** for code.  
5. **Explicit uncertainty** + next verification step.  
6. **Stop contract** — 2–5 sentence completion summary.  
7. **Memory discipline** — recall before re-derive; log decisions.  
8. **Skill invocation** for mode switches (CEO vs retro vs implement).

Add **persona-specific output schema** via skill, not by bloating L0.

---

## 9. Recommendations for lmloop (prioritized)

| Priority | Action | Rationale |
|----------|--------|-----------|
| P0 | **Static/dynamic prompt split** in `agent.py` (cache-friendly core + per-session block) | OpenHands/Deep Agents pattern; cost + consistency |
| P0 | **Trajectory export** for eval (JSONL: messages, tools, outcome, user corrections) | Enables GEPA/dspyground offline |
| P1 | **`/research` skill** with IRIS + Failure Diagnosis table | Serves scientist persona natively |
| P1 | **`/resolve` skill** with REFLECT-6 | AGI-style structured resolution |
| P2 | **Path-triggered skill stubs** (OpenHands-style) on `read_file`/`update_file` | Inject repo conventions without fat L0 |
| P2 | **Spirit pipeline** from `DESIGN_EVOLVING_SPIRIT.md` | Long-horizon harness evolution beyond facts |
| P3 | **GEPA experiment** on `system.md` + `/ceo` against fixed transcript eval set | Quantify prompt evolution ROI |

### 9.1 Mapping to lmloop continual harness design

[DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md](DESIGN_CONTINUAL_HARNESS_AND_SANDBOX_EVOLUTION.md)
operationalizes the survey’s co-evolution diagram for this repo:

| Survey concept (§1–2) | lmloop design anchor |
|-------------------------|----------------------|
| Failure diagnosis \(F_k\) | `usage.jsonl`, `/retro`, future trajectory export |
| Paired feasible / infeasible tasks | Usage evals V5 Act / Abstain / Pair fixtures |
| Harness optimizer (GEPA, layers) | Offline prompt/skill evolution; Life-Harness-style layers as future middleware |
| Environment evolution | Derived checks + sandbox profiles (Docker governance Part 7) |
| Runtime-bound controls vs prompt-only | `GatePolicy`, `--docker`, check authority in `loop.py` |

---

## 10. Risks and limitations of this study

- **Star ranking ≠ maintenance** (AutoGPT, AutoGen, Aider caveats).  
- **Closed products** (Claude Code, Codex) hide full prompts—patterns inferred from docs and community.  
- **Benchmarks ≠ your repo**—always run internal trajec eval before deploying evolved prompts.  
- **Over-evolution** without abstention tests → agents that **try too hard** on impossible tasks.

---

## 11. References (primary)

| Resource | URL / location |
|----------|----------------|
| SWE-agent ACI paper | https://arxiv.org/abs/2405.15793 |
| SWE-agent config templates | https://swe-agent.com/latest/config/ |
| OpenHands static/dynamic system message | https://docs.openhands.dev/sdk/api-reference/openhands.sdk.agent |
| OpenHands Skills | https://docs.openhands.dev/overview/skills |
| Deep Agents overview & context engineering | https://docs.langchain.com/oss/python/deepagents/overview |
| CrewAI agents & custom templates | https://docs.crewai.com/edge/en/concepts/agents |
| MetaGPT multi-agent SOP | https://docs.deepwisdom.ai/main/en/guide/tutorials/multi_agent_101.html |
| OpenAI Agents handoff prompt | https://openai.github.io/openai-agents-python/ref/extensions/handoff_prompt/ |
| smolagents `code_agent.yaml` | https://github.com/huggingface/smolagents/blob/main/src/smolagents/prompts/code_agent.yaml |
| GEPA / DSPy | https://github.com/gepa-ai/gepa |
| Life-Harness | https://github.com/OctAg0nO/harness |
| HarnessX | https://github.com/Darwin-Agent/HarnessX |
| dspyground | https://github.com/Scale3-Labs/dspyground |
| lmloop harness | `lmloop/lmloop/skills/system.md`, `lmloop/docs/DESIGN_*` |

---

## Appendix A — Prompt snippet gallery (high-signal)

**SWE-agent instance workflow (abridged):** numbered steps — locate code → repro script → fix → rerun → edge cases; explicit “do not modify tests.”

**OpenAI handoff prefix:** declares `transfer_to_*`, hides transfers from user.

**smolagents loop:** Thought → Code (`{{code_block}}`) → Observation → `final_answer`.

**CrewAI override pattern:** custom `system_template` with `{role}`, `{goal}`, `{backstory}` to disable unwanted default injections.

**MetaGPT evolution (research):** handover feedback → updated **constraint prompt** per role before next project.

---

*Document produced for internal research; star counts and project status should be re-verified before external citation.*
