# Structural decomposition — implementation plan

**Goal:** Move loop and CLI ownership onto the modules named in the design, and fix append, parse, and tool-wave races, without a new dependency or a sixth module in a full category.

**Design:** [../designs/2026-11-05-structural-decomposition.md](../designs/2026-11-05-structural-decomposition.md)

**Tech stack:** Python 3.14, stdlib (`fcntl`, `shlex`, `contextvars`), existing unittest suite.

## Global constraints

- Package root stays `__init__.py`, `__main__.py`, `agent.py`, `loop.py`, `graph.py`.
- Counted category modules stay at most five. After this plan, `entry/` and `core/` are full. `company/` returns to five (`allowlist`, `manifest`, `orchestrator`, `worker`, `worktree`).
- `act(cfg, model, messages, **kwargs)` keyword names do not change. `lmloop.loop.agent.act` remains the until/graph patch site.
- `build_tools` still returns `(specs, impls)`. `impls` is a `dict` subclass (`ToolImpls`) carrying `.defs` and `.output_limit`.
- `append_jsonl(path, row)` signature does not change.
- Workers still do not append campaign or company logs. The lock only serializes writers of one file.

## Review focus

- A second `build_tools` during an in-flight wave must not change that wave’s truncation limit. Test: `ConcurrentToolTests.test_build_limit_survives_later_build`.
- Quoted `--check` must stay one command and must not swallow the rest of the node line. Test: `UnverifiedCheckTests.test_quoted_check_stays_one_command`.
- Two threads appending one JSONL file must leave one JSON object per line. Test: `MemoryTests.test_append_jsonl_concurrent_writers`.
- `RoundDisplay` lookups of the spinner and live printer are on `lmloop.model.display`, not `lmloop.agent`.

## Tasks

### 1. Company packet lives on the manifest

**Files:** `lmloop/company/manifest.py`, `lmloop/company/worker.py`, `lmloop/company/orchestrator.py`, delete `lmloop/company/protocol.py`, `tests/test_package_layout.py`

**Produces:** `PacketError`, `parse_packet`, `envelope`, `parse_envelope`, `REQUIRED`, `STATUSES` defined in `manifest.py`.

- [ ] Move the protocol module body onto `manifest.py`. Point worker and orchestrator imports at `manifest`. Delete `protocol.py`.
- [ ] Add `"company"` to the layout category list (modules excluding `__init__.py`).
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_package_layout -v`
- [ ] Expected: PASS. `company` has five modules.

### 2. Locked JSONL append

**Files:** `lmloop/memory/__init__.py`, `tests/test_memory.py`

**Produces:** `append_jsonl` opens with `O_APPEND | O_CREAT | O_WRONLY`, `fcntl.flock(fd, LOCK_EX)`, writes one line, unlocks in `finally`.

- [ ] Test `test_append_jsonl_concurrent_writers`: 4 threads × 40 rows, 160 lines, each `json.loads`.
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_memory.MemoryTests.test_append_jsonl_concurrent_writers -v`
- [ ] Expected: PASS.

### 3. shlex for checks and restore

**Files:** `lmloop/flow/graph_runner.py`, `lmloop/entry/repl.py`, `tests/test_graph.py`, `tests/test_cli.py`

**Produces:** `annotate_unverified_commands` reads the `--check` operand with `shlex.split`. `parse_restore_arg` uses `shlex.split` and returns `None` on `ValueError`.

- [ ] Test quoted `--check 'not-a-real-bin -m unittest'` annotates that command and not the following goal words.
- [ ] Test `parse_restore_arg('session "my notes"')` is `("session", "my notes", False)`.
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_graph.UnverifiedCheckTests tests.test_cli.RestoreArgTests -v`
- [ ] Expected: PASS. (`RestoreArgTests` already calls `parse_restore_arg`; add the quote case there.)

### 4. Completion and sandbox CLI

**Files:** `lmloop/entry/completion.py`, `lmloop/entry/sandbox_cli.py`, `lmloop/entry/cli.py`

**Produces:**

- `zsh_completion_script() -> str` builds the script from `CommandMeta`, `DEFAULTS`, `MEMORY_ARG_CHOICES`, `GRAPH_PROPOSE_VERB`, and `EVAL_FLAGS` by concatenation. No chained `.replace()` on a template.
- `cmd_completion(shell, console) -> int` and `cmd_completion_cli(cfg, words, console) -> int`.
- `cmd_sandbox`, `sandbox_build`, `sandbox_ready`, `activate_sandbox(cfg, args, console) -> int`.

`cli.py` imports those names. `cli_handlers()` raises `RuntimeError` when its keys differ from `cli_subcommand_names()`.

- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_cli.CliRoutingTests.test_completion_zsh tests.test_cli.RegistryTests -v`
- [ ] Expected: PASS. The script still contains `#compdef lmloop`, each CLI stem as `'name:`, `compadd - propose`, and `compadd - --json --design --design-doc`.

### 5. Continue policy

**Files:** `lmloop/core/continue_policy.py`, `lmloop/agent.py`, `tests/test_agent_stream.py`

**Produces:** `_line_has_intent`, `_ends_with_continue_intent`, `_has_continue_intent`, `_looks_grounded`, `_should_nudge_continue(content, nudges, max_nudges)`, `_is_noise`, `_should_nudge_halt`, `_warn_stopped_unfinished`. No `turn_tools` parameter.

`GatherTurn.after_empty_gather` calls `_should_nudge_continue` / `_should_nudge_halt` / `_warn_stopped_unfinished` and does not repeat the intent and grounded checks inline.

- [ ] Tests call `lmloop.core.continue_policy`. Add `_looks_grounded("see main.go:42")` is true and `_line_has_intent("Let me")` is true.
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_agent_stream.ContinueNudgeTests -v`
- [ ] Expected: PASS.

### 6. Round display

**Files:** `lmloop/model/display.py`, `lmloop/agent.py`, `tests/test_agent_stream.py`

**Produces:** `RoundDisplay` in `display.py`. It calls `_open_live_display` and `_GeneratingIndicator` in that module. `act()` constructs `RoundDisplay` from `display`.

- [ ] Retarget `patch.object(agent, "_open_live_display"` and `patch.object(agent, "_GeneratingIndicator"` to `display`.
- [ ] Keep `from .model.chat import _chat, _chat_stream` in `agent.py`.
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_agent_stream.ActIntegrationTests -v`
- [ ] Expected: PASS.

### 7. ActTurn

**Files:** `lmloop/agent.py`

**Produces:**

```python
@dataclass
class ActTurn:
    confirm_gate: object = None
    stats: dict | None = None
    session_log: Path | None = None
    echo_delta: object = None
    on_thinking: object = None
    readonly: bool = False
    no_tools: bool = False
    max_rounds: int | None = None
    extra_readable: list | None = None
    tool_only: frozenset | None = None
```

`act()` builds one `ActTurn` from its keywords and reads that object in the loop. Callers keep passing keywords.

- [ ] Covered by `ActIntegrationTests` in task 6. No signature break.

### 8. Tool registry carried by the build

**Files:** `lmloop/capabilities/tools.py`, `tests/test_tools.py`

**Produces:** `ToolImpls(dict)` with `.defs` and `.output_limit`. Each impl closes over that limit via a `ContextVar` so `_truncate` does not read a later `build_tools`. `tool_wave`, `concurrent_groups`, `_run_wave`, `run_tool_calls`, `validate_tool_arguments`, and `dispatch` take the defs mapping when the caller has one. `MAX_OUTPUT` stays the module default and is not assigned inside `build_tools`.

- [ ] Test `test_build_limit_survives_later_build`: first build limit 1000, second build limit 20000, `run_tool_calls` on the first impls still truncates.
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest tests.test_tools.ConcurrentToolTests.test_build_limit_survives_later_build -v`
- [ ] Expected: PASS.

### 9. Docs in lockstep

**Files:** `docs/ARCHITECTURE.md`, `DEVELOPMENT.md`, `docs/IMPLEMENTATION_PLAN.md`

- [ ] Component map lists `entry/completion.py`, `entry/sandbox_cli.py`, `core/continue_policy.py`, `RoundDisplay` on `display.py`, and the company packet on `manifest.py`.
- [ ] Run: `cd lmloop && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v`
- [ ] Expected: PASS.

## Out of scope

MCP. Splitting the capped god modules. A control-message enum. An `ActContext` type. README edits.
