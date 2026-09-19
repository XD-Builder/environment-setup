# Design: Execution sandbox and verification hardening

**Status:** proposed (nothing here is implemented)
**Date:** 2026-09-19
**Depends on:** `tools.run_shell`, `tools.GatePolicy`, `loop.run_until`, `graph.run_graph`
**Companion:** [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md)

Two questions drive this file:

1. Should autonomous shell execution leave the host? **Yes, opt-in, and only with a
   fail-fast preflight — never a silent fallback to the host.**
2. How does the loop stop believing the model? **Deterministic acceptance commands
   with a recorded failing baseline.** An adversarial LLM auditor is a supplement,
   not the mechanism.

---

## Part 0 — Adversarial review of the obvious proposal

The naive version of this feature ("run `docker run -v $(pwd):/workspace`, exec into
it, add an auditor prompt") has failure modes that must be designed out before any
code lands. Each is a requirement below.

| # | Flaw in the naive proposal | Consequence | Requirement |
|---|---|---|---|
| 1 | Bind-mounting the workspace is described as "blast radius containment" | The workspace is the thing being mounted. `rm -rf /workspace/*` destroys real source. `tools.backup_file` only covers **file-tool** mutations, not shell | **R-SNAP**: git snapshot before every maker step in autonomous runs |
| 2 | `--net=host` recommended as the easy path | Container reaches host LM Studio **and** host Postgres, admin ports, `169.254.169.254`, SSH agent socket. Network blast radius becomes zero-contained; on macOS Docker Desktop it does not behave like Linux at all | **R-NET**: default `bridge`; `host` is opt-in with a printed warning and is documented as Linux/WSL2-only |
| 3 | Fixed container name (`lmloop-sandbox`) | Two lmloop processes in two repos exec into **one** container, i.e. project A's agent runs commands against project B's mount. Silent cross-project corruption | **R-ID**: container name = `<prefix>-<slug>-<sha8(realpath(workspace))>`; a name collision check refuses to start |
| 4 | `subprocess.run(["docker","exec",…], timeout=N)` | The timeout kills the **docker CLI client**, not the process in the container. A runaway `pytest -x` or fork bomb survives every "timeout" for the life of a 24/7 run | **R-TMO**: `timeout --signal=TERM --kill-after=5s <N>s` inside the container **plus** a client-side guard **plus** exec-id tracking |
| 5 | No cgroup limits | `while true; do :; done &` inside a container still eats every host core; unbounded memory triggers host OOM killer, which may kill LM Studio, not the container | **R-LIM**: `--pids-limit`, `--memory`, `--cpus`, `--cap-drop ALL`, `--security-opt no-new-privileges` |
| 6 | Unpinned base image | `lmloop-base-image` is a floating tag. A rebuild six weeks later silently changes Python patch level, `rg` version, and TLS roots. Non-deterministic builds are the thing we are supposedly fixing | **R-PIN**: image referenced by `@sha256:` digest; `Dockerfile` in-repo; `lmloop sandbox build` records the digest into config |
| 7 | Root inside the container writing to the bind mount | Root-owned files land in the user's git repo on Linux; the user's next `git checkout` fails with EACCES | **R-UID**: `--user $(id -u):$(id -g)`, `HOME=/tmp/lmloop-home`, and a preflight that writes and deletes a probe file |
| 8 | Maker in the container, `--check` on the host | Environment skew. `pytest -q` passes on the host because host deps exist, so the gate certifies work done in a different environment. This is worse than no gate | **R-SAME**: `loop.run_check`, acceptance commands, and probe commands use the **same** backend as the maker, or the run refuses to start |
| 9 | Silent fallback to host when Docker is down | At 03:00 the daemon restarts, the agent keeps working — on the host, unsandboxed, with `autonomous_gates: all` | **R-FAIL**: `sandbox_required` (default true when `sandbox_mode: docker`) aborts the run with a clear error; no degraded mode |
| 10 | Mounting `~/.ssh`, `~/.aws`, or the Docker socket "so the agent can push / build" | An autonomous agent with the Docker socket has host root. Credentials in the mount are exfiltratable by any prompt injection from `fetch_url` | **R-SECRET**: explicit non-goal; env passthrough is an empty-by-default allowlist; `/var/run/docker.sock` is refused by preflight |
| 11 | "Auditor LLM formulates 2 shell commands" sold as the honesty mechanism | Same model, same weights, same optimism bias — errors are correlated. On a 7B local model the probe output is frequently unparseable or destructive | **R-DET**: authority lives in deterministic acceptance commands. The probe is opt-in, capped, read-only, and can only ever produce `fail`, never `pass` |
| 12 | "Any nonzero exit blocks completion" | `grep` with no match is 1. `diff` is 1. `test -f` is 1. A blanket rule teaches the model to append `\|\| true`, which actively destroys the signal we are trying to protect | **R-SCOPE**: exit-code authority applies only to designated check/acceptance commands. All other shell exits are evidence, surfaced to the checker, never a hard stop |
| 13 | `signal_task_complete` in the REPL turn loop | Every interactive answer would need a tool call to terminate. Breaks normal use for a problem the REPL does not have (a human is reading the reply) | Non-goal. `until`/`graph` already ignore prose; see "Rejected" below |
| 14 | Bind-mount performance on macOS | virtiofs/osxfs over a large repo makes a repeated `pytest` cycle 2–5× slower, which matters when the loop runs 200 cycles overnight | **R-PERF**: document named volumes for `.venv` / `node_modules`; `:delegated` consistency; measure before defaulting anyone into it |
| 15 | Nothing says how this is operated 24/7 | lmloop is a process that exits. There is no daemon, and adding one is an existing non-goal | Out of scope: the supervisor stays OS-level (systemd/launchd/cron/tmux) calling `lmloop until` / `lmloop graph` |

### What survives review

The core claim holds: **host shell access for an unattended loop is the largest
unmitigated risk in lmloop today**, because `tools.run_shell` is host-native and
`autonomous_gates: all` disables the only thing standing in front of it. The fix is
worth building — with the fifteen requirements above wired in, not the four-line
`docker exec` helper.

### Rejected outright

- **LangGraph / Docker SDK / `docker-py`.** `docker` CLI over `subprocess` keeps the
  dependency set at "a binary that must already exist", which is auditable and
  lockfile-free. A Python SDK adds a transitive tree (`requests`, `urllib3`,
  `websocket-client`) to a project whose agent loop is deliberately `urllib`-only.
- **A `signal_task_complete` tool.** See #13.
- **Podman/nerdctl/Lima as separate backends in v1.** Podman is CLI-compatible enough
  to be handled by `sandbox_docker_bin`; a second backend class earns its keep only
  when someone reports it failing.
- **An in-process scheduler or daemon.** Existing non-goal in
  [DESIGN_GRAPH_ENGINEERING.md](DESIGN_GRAPH_ENGINEERING.md); unchanged.
- **Mounting `~/.lmloop` into the container.** Memory stays host-side because the
  agent process stays host-side. Only shell execution crosses the boundary.

---

## Part 1 — Execution backend

### 1.1 New module: `lmloop/exec.py`

One type owns "where a command runs". `tools.py` stops owning `subprocess`.

```python
@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    backend: str          # "local" | "docker"
    timed_out: bool

class ExecBackend:          # method contract, not an ABC
    name: str
    def run(self, command: str, *, timeout_s: int, shell: bool,
            cwd: Path) -> ExecResult: ...
    def preflight(self) -> "str | None": ...   # None = healthy, else the reason
    def describe(self) -> str: ...             # one line for /stats and run logs
```

- `LocalBackend` is today's `subprocess.Popen` path moved verbatim, including
  `KeyboardInterrupt` kill semantics.
- `DockerBackend` shells out to `docker exec`.
- `exec.build_backend(cfg, workspace_root) -> ExecBackend` is the only factory.

**Import graph:** `exec.py` is a leaf — `subprocess`, `shlex`, `hashlib`, `pathlib`,
`config`. It must not import `tools`, `agent`, `loop`, or `graph`. `tools.py` and
`loop.py` may import `exec`.

**Why a module and not a `tools.py` function:** `loop.run_check`, the acceptance
runner, and `run_shell` all need the identical backend instance (R-SAME). A shared
owning type is the repo's stated pattern for exactly this case.

### 1.2 Container lifecycle

| Operation | Command shape |
|---|---|
| Name | `f"{prefix}-{slug}-{sha256(str(workspace.resolve()))[:8]}"` |
| Start | `docker run -d --name <n> --label lmloop.workspace=<path> --label lmloop.version=<v> -v <ws>:/workspace -w /workspace --user <uid>:<gid> -e HOME=/tmp/lmloop-home --cap-drop ALL --security-opt no-new-privileges --pids-limit <n> --memory <m> --cpus <c> [network flags] <image@digest> sleep infinity` |
| Exec | `docker exec -w /workspace <n> /bin/sh -lc 'timeout --signal=TERM --kill-after=5s <N>s <command>'` |
| Health | `docker inspect -f '{{.State.Running}}{{index .Config.Labels "lmloop.workspace"}}' <n>` |
| Reset | `docker rm -f <n>` then start |

Notes:

- `sleep infinity` over `tail -f /dev/null` — one less file descriptor and it is what
  the image's `sh` provides.
- Exit code 124 from `timeout` maps to `timed_out=True` and the existing
  `ERROR: command timed out after Ns` string, so `check_status_from_output` and every
  current test stay valid.
- The label check is not cosmetic: it is how R-ID detects a stale container from a
  moved/renamed directory that hashed to a reused name.
- Simple (non-shell-syntax) commands still go through `/bin/sh -lc` because `timeout`
  must wrap them; `shlex.quote` the command, and keep `ShellCommand`'s
  `confirm_shell_syntax` classification on the **original** string so gate behavior
  does not change. This is the one place where local and docker semantics differ, and
  it must be tested explicitly.

### 1.3 Preflight (fail fast at boot, never mid-run)

`DockerBackend.preflight()` returns the first failure of:

1. `sandbox_docker_bin` exists on PATH.
2. `docker version --format '{{.Server.Version}}'` succeeds within 10s (daemon up).
3. Configured image reference contains `@sha256:` and is present locally
   (`docker image inspect`), else a clear "run `lmloop sandbox build`".
4. Workspace path is not `/`, not `$HOME`, and is inside a git repo when
   `autonomous_snapshot: git`.
5. No `docker.sock` in any configured mount (R-SECRET).
6. Write probe: exec `touch .lmloop-probe && rm .lmloop-probe`, then confirm on the
   **host** that no root-owned artifact remains (R-UID).
7. Container name is free, or the existing container's `lmloop.workspace` label
   matches this workspace (R-ID).

`cli.py` runs preflight **before** the first maker step of `until` / `graph` and before
the REPL starts when `sandbox_mode: docker`. Failure with `sandbox_required: true`
(default) aborts with a nonzero exit and a one-line reason. There is no partial mode.

### 1.4 Ports and networking

| `sandbox_network` | Behavior |
|---|---|
| `bridge` (default) | Publish `sandbox_ports` ranges; inject `host.docker.internal` (Linux: `--add-host host.docker.internal:host-gateway`) so the agent can reach the host LM Studio if it ever needs to |
| `none` | No network. Correct default for a pure refactor loop; breaks `pip install` and `web_search` **inside** the sandbox (host-side `web_search` is unaffected because it runs in the lmloop process) |
| `host` | Opt-in, prints `[sandbox · host network — no network isolation]` once per run, documented Linux/WSL2-only |

`sandbox_ports` default `"3000-3010,8000-8010"`. When a range is published, a generated
line is appended to the injected steering block (`steer.py` already owns always-on
text): *"Test servers must bind 0.0.0.0 inside the sandbox on a port in 3000-3010 or
8000-8010; other ports are unreachable from the host."* Binding `0.0.0.0` inside a
container with a published range is correct and is not a host exposure.

### 1.5 Image

`lmloop/sandbox/Dockerfile`, pinned by digest, deliberately small:

```dockerfile
FROM debian:bookworm-slim@sha256:<pinned>
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git ripgrep curl python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
```

- No language toolchain zoo. Projects that need Node or Go override with
  `sandbox_image`; the contract is "has `sh`, `timeout`, `git`, and whatever the
  project's checks need".
- `lmloop sandbox build` builds, reads back `docker image inspect --format '{{index
  .RepoDigests 0}}'`, and writes that digest into `~/.lmloop/config.json`. A tag is
  never persisted.
- Digest drift is detected on preflight; the run aborts rather than executing against
  an image nobody audited.

### 1.6 Snapshots (R-SNAP)

Bind mounts make workspace damage real, so the sandbox alone is not a safety story.

`autonomous_snapshot: git` (default when the workspace is a git repo, else `off`):
before each maker step in `until` / `graph`, run the equivalent of
`git stash create` + `git update-ref refs/lmloop/<run-ts>/<step> <sha>` — i.e. a real,
recoverable commit object that is **not** on any branch, does not touch the index, and
does not disturb the user's working state. The ref name is written into the run JSONL
row, so recovery from an overnight disaster is `git checkout refs/lmloop/…`.

Cost is one `git` invocation per maker step. Refs are pruned by age with the same
14-day horizon as `trash/`. Untracked files are included via
`git stash create --include-untracked` semantics; when the tree is clean, no ref is
written and the row records the current `HEAD`.

---

## Part 2 — Verification hardening

### 2.1 What is authoritative

| Signal | Authority | Why |
|---|---|---|
| `--check <cmd>` exit code | **Authoritative** (already shipped) | Deterministic, no model in the path |
| `--accept <cmd>` exit codes (new) | **Authoritative** | Same, and they are the human's definition of done |
| Negative baseline transition (new) | **Authoritative when enabled** | Proves the probe can fail, so a trivially-passing check is caught |
| Eval `STATUS:` line | Necessary, not sufficient | An LLM reading an LLM |
| Probe commands (new, opt-in) | Can only downgrade `pass` → `fail` | Correlated-error supplement |
| Maker prose | Zero | Already true in `loop.py`; keep it that way |
| Any other shell exit code | Evidence, surfaced to the checker | R-SCOPE: a blanket rule teaches `\|\| true` |

### 2.2 Acceptance commands

Syntax, repeatable, on `until` and on graph nodes:

```
lmloop until --check 'pytest -q' --accept 'ruff check .' --accept 'python -m build -n' <goal>
node build until --check 'pytest -q' --accept 'ruff check .' implement the agreed change
```

Semantics:

1. Acceptance commands run **after** `--check` passes or eval returns `STATUS: pass`,
   never before, and always through the maker's backend (R-SAME).
2. All must exit 0. The first nonzero flips the cycle to `fail` and the clipped output
   is appended to the handoff as
   `Acceptance failed: <cmd> exited N` + output.
3. They run under the until/graph `GatePolicy` with denials dropped, exactly like
   `--check` today, so an acceptance command cannot be turned into a mutation vector.
4. They are recorded as their own `accept` row in the run JSONL (role `accept`,
   status `pass|fail`, handoff = clipped output), so the log shows why a run that the
   eval called `pass` did not finish.
5. They are **never LLM-authored.** `graph propose`
   ([companion doc](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md)) may *suggest* one in a draft a
   human approves; nothing writes an acceptance command at runtime.

### 2.3 Negative baseline (`require_negative_baseline`, default false)

Implements "show me the failure first" deterministically:

1. Before the first maker step, run every acceptance command and record the exit codes
   as a `baseline` row.
2. A run may only reach `done/pass` if **at least one** acceptance command went
   nonzero at baseline and zero at the end.
3. If every acceptance command already passed at baseline, the run stops immediately
   with `blocked` and the message *"acceptance commands already pass — nothing to
   prove; tighten the check or the goal"*.

This catches the single most common hallucinated-success pattern: a check so weak it
passes before any work is done.

### 2.4 Unverified-exit annotation (cheap, deterministic, always on)

`loop.py` already hands the maker's last assistant summary to the checker. Add one
deterministic line, computed from the maker's tool transcript, not from prose:

```
Unverified: the maker's last shell command exited 2 (`pytest -q tests/test_loop.py`).
```

No judgment, no model call — the checker simply sees the evidence it would otherwise
have to ask for. Omitted when the last shell exit was 0 or there was no shell call.

### 2.5 Adversarial probe (`eval_probe`, default false)

Only after eval says `pass` **and** all acceptance commands pass:

- One isolated `act()` with `readonly=True`, `max_rounds=1`, a frozen prompt, and no
  access to the maker's transcript beyond the handoff.
- Output contract: a fenced block with **≤ `eval_probe_max_cmds` (default 2)** shell
  commands, one per line. Parsing failure → probe skipped, logged as `probe/skipped`.
  A skipped probe never blocks (R-DET: it can only downgrade).
- Commands run through the maker backend under the `GatePolicy`; anything destructive
  is `DENIED` and dropped, exactly like eval today.
- Any nonzero exit → cycle becomes `fail`, output goes back to the maker.
- Auto-disabled when the detected context window is below `probe_min_context`
  (default 8192) — on a small local model the probe is noise.

### 2.6 Rejected in this part

- Blanket "nonzero exit blocks the turn" (R-SCOPE).
- A second model or second `base_url` for the auditor: doubles operational surface for
  a project whose premise is one local server. The deterministic acceptance list is
  the better answer to correlated errors.
- Parsing `"I am finished"`: already not done; do not introduce it via a "completion
  heuristic".

---

## Part 3 — Config, commands, telemetry

### 3.1 New config keys (all default to today's behavior)

| Key | Default | Meaning |
|---|---|---|
| `sandbox_mode` | `off` | `off` \| `docker` |
| `sandbox_required` | `true` | When `docker`, abort rather than fall back to host |
| `sandbox_image` | `""` | Must contain `@sha256:`; written by `lmloop sandbox build` |
| `sandbox_docker_bin` | `docker` | `podman` works here |
| `sandbox_network` | `bridge` | `bridge` \| `none` \| `host` |
| `sandbox_ports` | `3000-3010,8000-8010` | Published ranges when `bridge` |
| `sandbox_memory` | `4g` | `--memory` |
| `sandbox_cpus` | `2` | `--cpus` |
| `sandbox_pids` | `512` | `--pids-limit` |
| `sandbox_env_passthrough` | `""` | Comma-separated allowlist, empty = none |
| `sandbox_idle_reap_h` | `24` | `lmloop sandbox status` reaps older idle containers |
| `autonomous_snapshot` | `git` | `git` \| `off` |
| `require_negative_baseline` | `false` | Section 2.3 |
| `eval_probe` | `false` | Section 2.5 |
| `eval_probe_max_cmds` | `2` | Cap |
| `probe_min_context` | `8192` | Below this, probe is skipped |

Every key gets a README row in the same diff (DEVELOPMENT.md rule: no config key
without a reader **and** a README line).

### 3.2 New command stem

`sandbox` is one new `CommandMeta` with `arg_choices = ("build","status","shell","reset","rm")`:

| Command | Effect |
|---|---|
| `lmloop sandbox build` | Build the pinned image, persist the digest |
| `lmloop sandbox status` | Preflight result, container state, mounts, limits, published ports; reaps idle containers past `sandbox_idle_reap_h` |
| `lmloop sandbox shell` | Interactive `docker exec -it` for humans debugging the environment |
| `lmloop sandbox reset` | `rm -f` + recreate (the "wipe the container, keep the workspace" flow) |
| `lmloop sandbox rm` | Remove the container, leave the image |

`/sandbox` mirrors it in the REPL (status/reset only). `ui.Console` gains one status
line in `/stats`: `sandbox: docker · lmloop-sbx-repo-1a2b3c4d · bridge · 3000-3010`.

### 3.3 Run-log additions (additive, backward compatible)

New roles in the until/graph JSONL: `baseline`, `accept`, `probe`. New optional fields
on existing rows: `snapshot_ref`, `backend`. Readers already tolerate unknown keys
(`.get` throughout `UntilRun`/`GraphRun`), and `next_role`'s table gains
`("accept","fail") -> "maker"`, `("accept","pass") -> after_pass`,
`("probe","fail") -> "maker"`, `("probe","pass"|"skipped") -> after_pass`.

Old logs replayed by a new binary see no new roles and behave exactly as today. New
logs replayed by an old binary hit `next_role`'s `return "maker"` fallback — degraded
but not corrupt. That asymmetry is the rollback story: **downgrade is safe, it just
re-runs a maker step.**

---

## Part 4 — Task breakdown

Each task is one reviewable diff with tests and docs in the same change. Phases ship
independently; nothing below changes default behavior until a config key is flipped.

### Phase S — sandbox

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| S1 | `exec.py` with `ExecResult`, `ExecBackend` contract, `LocalBackend` extracted verbatim from `run_shell`; `build_backend` factory | `exec.py`, `tools.py` | `test_exec.py`: local run, nonzero exit, timeout kill, `KeyboardInterrupt` kill; all existing `test_tools.py` shell tests unchanged | `run_shell` has no `subprocess` import and the suite is green with zero behavior delta |
| S2 | `DockerBackend`: name derivation, run/exec/inspect, `timeout` wrapper, exit-124 mapping, `shlex.quote` of the original command | `exec.py` | Fake `subprocess` asserting exact argv; name stability for the same path, divergence for different paths; 124 → `timed_out` | Argv is asserted, not smoke-tested |
| S3 | `preflight()` with all seven checks + `sandbox_required` abort wiring in `cli.py` | `exec.py`, `cli.py`, `status.py` | Each check fails independently with its own message; abort is nonzero exit; no silent fallback | A dead daemon aborts `lmloop until` before the first model call |
| S4 | Same-backend enforcement: `run_check`, acceptance, probe all take the backend from the caller | `loop.py`, `graph.py`, `tools.py` | A test proves `run_check` cannot execute locally while the maker is containerized | R-SAME is a unit test, not a convention |
| S5 | `lmloop sandbox` stem + `/sandbox` + `/stats` line + idle reap | `commands.py`, `cli.py`, `repl.py`, `ui.py` | Routing tests; `status` output with a fake docker | `/help` lists `sandbox` once |
| S6 | Image: `sandbox/Dockerfile` with a pinned base digest, `sandbox build` digest persistence, digest-drift detection | `sandbox/Dockerfile`, `exec.py`, `config.py` | Config rejects a reference without `@sha256:` | A tag can never be persisted as `sandbox_image` |
| S7 | Port ranges → generated steering line; `host` network warning | `steer.py`, `exec.py` | Steering block contains the configured ranges; warning printed once per run | The model is told the port contract only when ranges exist |
| S8 | Docs: README (Safety, config table, troubleshooting rows), ARCHITECTURE (component map row, tools table `run_shell` safety cell), DEVELOPMENT (module row, import-graph line) | docs | — | No doc claims sandboxing is on by default |

### Phase V — verification

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| V1 | `--accept` parsing (CLI, `/until`, graph `node` lines), stored on `UntilRun` meta and `NodeDef` | `loop.py`, `graph.py`, `cli.py`, `repl.py` | Quoted commands survive `shlex`; repeated flags accumulate; graph parse errors are fail-closed | A graph with `--accept` round-trips through `parse_graph` |
| V2 | Acceptance runner + `accept` row + `next_role` entries | `loop.py`, `graph.py`, `status.py` | Eval `pass` + acceptance fail → maker; output is clipped and cited; denial is dropped not re-asked | An eval `pass` can no longer finish a run by itself |
| V3 | `require_negative_baseline`: `baseline` row, transition rule, "already passes" block | `loop.py`, `status.py` | Baseline all-pass → `blocked` with the exact message; baseline fail → pass → `done` | A check that passes before any work cannot certify the run |
| V4 | `Unverified: last shell exited N` annotation from the maker transcript | `loop.py`, `agent.py` (transcript accessor only) | Nonzero last shell → line present with the command; zero → absent | The checker sees exit evidence without a model call |
| V5 | `eval_probe`: frozen prompt, ≤2 command parse, readonly execution, downgrade-only, context-gated | `loop.py`, `status.py` | Unparseable output → `skipped`, never blocks; destructive probe → `DENIED` + dropped; nonzero → `fail` | Probe can only ever downgrade |
| V6 | Docs: README until/graph sections + config rows; ARCHITECTURE goal-loop bullet; this file → `Status: shipped` | docs | — | The `until` flow diagram shows baseline/accept/probe |

### Phase R — recovery

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| R1 | `autonomous_snapshot: git`: pre-maker snapshot ref, `snapshot_ref` on the row, 14-day prune | `loop.py`, `graph.py`, new `snapshot.py` (leaf) | Clean tree → HEAD recorded, no ref; dirty tree → ref resolves to a real commit containing the dirt; non-git workspace → `off` with a printed note | `git checkout <ref>` recovers an overnight loop's pre-step tree |
| R2 | Docs + a README "recover from an autonomous run" row next to the `trash/` row | docs | — | Recovery is documented next to the existing backup story |

### Sequencing and independence

- **S1 alone is worth landing** even if the sandbox is never enabled: it removes
  `subprocess` from `tools.py` and creates the seam.
- **V1–V4 do not depend on Phase S.** Acceptance commands, the negative baseline, and
  the unverified-exit annotation improve honesty on the host today.
- **S4 is the gate for calling the sandbox "done"**; shipping S2 without S4 produces
  exactly hazard #8.
- **R1 should land before anyone is told to run unattended**, sandboxed or not.

---

## Part 5 — Risk register

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| Docker absent/broken mid-run | P0 | R-FAIL abort + preflight before first model call | Run stops; `/continue` resumes after the human fixes the daemon |
| Agent destroys the mounted workspace | P0 | R-SNAP git refs; gates unchanged inside the container | Untracked ignored files (`.venv`) are not snapshotted by design |
| Cross-project container reuse | P0 | R-ID hash + label verification | Two checkouts of the same path at different times share a container: `sandbox reset` |
| Runaway process survives timeout | P1 | R-TMO in-container `timeout` + client guard | A process that ignores SIGTERM is SIGKILLed after 5s |
| Host resource exhaustion | P1 | R-LIM cgroup flags | Disk is not limited in v1; document `docker system df` |
| Image drift | P1 | R-PIN digest + drift abort | Rebuild requires an explicit `sandbox build` |
| Acceptance commands become a rubber stamp | P1 | `require_negative_baseline` | Off by default; recommended in README for unattended runs |
| Probe emits destructive commands | P1 | readonly + GatePolicy + cap 2 + downgrade-only | Probe wastes a cycle when the model is weak; context-gated |
| Bind-mount slowness on macOS | P2 | Named volumes for `.venv`/`node_modules`, `:delegated` | Measure per project |
| Two lmloop processes, one project | P2 | Pre-existing single-writer assumption, now also on the container | Documented, not enforced |

---

## Part 6 — Test matrix (no network, no real Docker)

Every Docker interaction is asserted at the **argv** level against a fake
`subprocess`, in the style of `test_tools.py`'s existing `Popen` patching. There is no
"integration test that needs a daemon" in the suite; a `sandbox smoke` documented
manual check covers the real thing.

- Argv: `run` flags include `--user`, `--cap-drop`, `--pids-limit`, `--memory`,
  `--cpus`, `--security-opt`; `exec` wraps in `timeout --signal=TERM --kill-after=5s`.
- Names: same path → same name across processes; different path → different name;
  label mismatch → refuse.
- Exit mapping: 0 → pass, 1 → fail, 124 → timeout string, docker-CLI failure → `ERROR:`
  distinguishable from a command failure (a daemon error must not read as "your tests
  failed").
- Gates: destructive command inside the sandbox still hits `GatePolicy`; `DENIED`
  string unchanged.
- Backend parity: `check_status_from_output` passes identically for local and docker
  outputs (the `[exit code: N]` suffix is produced by `run_shell`, not the backend).
- Verification: acceptance fail after eval pass → maker; baseline all-pass → blocked;
  probe unparseable → skipped; probe nonzero → fail.
- Regression: with `sandbox_mode: off` and no new flags, every existing test in
  `test_tools.py` / `test_loop.py` / `test_graph.py` passes unmodified. That is the
  acceptance criterion for "no behavior delta".
