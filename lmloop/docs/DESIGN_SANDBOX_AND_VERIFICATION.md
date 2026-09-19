# Design: Execution sandbox and verification hardening

**Status:** proposed (nothing here is implemented)
**Date:** 2026-09-19
**Depends on:** `tools.run_shell`, `tools.GatePolicy`, `loop.run_until`, `graph.run_graph`
**Companion:** [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md)

Two questions drive this file:

1. Should autonomous shell execution leave the host? **Only when the user asks for it
   on the command line.** The default stays exactly what ships today: local execution,
   local model, no container runtime anywhere in the path.
2. How does the loop stop believing the model? **Deterministic acceptance commands with
   a recorded failing baseline.** An adversarial LLM auditor is a supplement, not the
   mechanism.

## Activation model (read this before anything else)

| Invocation | Shell execution | Container |
|---|---|---|
| `lmloop …` (default, unchanged) | Host `subprocess`, exactly as today | None. `docker` is never invoked, never probed |
| `lmloop --docker …` | Inside an **ephemeral** container removed when the process exits | Created at start, `rm -f` at exit, orphans reaped on next start |
| `lmloop --docker-persist …` | Inside a **long-lived** container that survives process exit | Named, reattached each run, for 24/7 supervisors |

There is no `sandbox_mode` config key, and no config value can turn the sandbox on. A
config key that silently containerizes a user who typed `lmloop` is exactly the kind of
action-at-a-distance this project avoids. Config only *parameterizes* a sandbox the
flag already requested. `--docker-persist` implies `--docker`.

Consequences that are deliberate:

- A user who never types `--docker` sees no behavior change, no new dependency, no new
  failure mode, and no startup latency. `exec.build_backend()` returns `LocalBackend`
  without touching `shutil.which("docker")`.
- Once the flag **is** present, there is no fallback to the host. A broken daemon aborts
  the run (see §1.3). "Degraded to host" is the failure this whole file exists to prevent.
- The git snapshot safety net (§1.6) is on by default in **both** modes, because the
  default mode is the one with no container at all.

---

## Part 0 — Adversarial review of the obvious proposal

The naive version ("run `docker run -v $(pwd):/workspace`, exec into it, add an auditor
prompt") has failure modes that must be designed out before any code lands. Each becomes
a numbered requirement.

| # | Flaw in the naive proposal | Consequence | Requirement |
|---|---|---|---|
| 1 | Bind-mounting the workspace is described as "blast radius containment" | The workspace is the thing being mounted. `rm -rf /workspace/*` destroys real source. `tools.backup_file` only covers **file-tool** mutations, not shell | **R-SNAP**: git snapshot before every maker step, in every mode |
| 2 | `--net=host` recommended as the easy path | Container reaches host LM Studio **and** host Postgres, admin ports, `169.254.169.254`, SSH agent socket. Network blast radius becomes zero-contained; on macOS Docker Desktop it does not behave like Linux at all | **R-NET**: default `bridge`; `host` is opt-in with a printed warning, documented Linux/WSL2-only |
| 3 | Fixed container name (`lmloop-sandbox`) | Two lmloop processes in two repos exec into **one** container, i.e. project A's agent runs commands against project B's mount | **R-ID**: name derived from workspace realpath hash; ephemeral names also carry the pid; label verified before reuse |
| 4 | `subprocess.run(["docker","exec",…], timeout=N)` | The timeout kills the **docker CLI client**, not the process in the container. A runaway `pytest -x` survives every "timeout" for the life of a 24/7 run | **R-TMO**: `timeout --signal=TERM --kill-after=5s <N>s` inside the container **plus** a client-side guard |
| 5 | No cgroup limits | `while true; do :; done &` still eats every host core; unbounded memory triggers the host OOM killer, which may kill the LM Studio process the agent depends on | **R-LIM**: `--pids-limit`, `--memory`, `--cpus`, `--cap-drop ALL`, `--security-opt no-new-privileges` |
| 6 | Unpinned base image | A rebuild six weeks later silently changes Python patch level, `rg` version, and TLS roots | **R-PIN**: `@sha256:` digest only; `Dockerfile` in-repo; drift aborts |
| 7 | Root inside the container writing to the bind mount | Root-owned files land in the user's git repo; the next `git checkout` fails with EACCES | **R-UID**: `--user $(id -u):$(id -g)`, `HOME=/tmp/lmloop-home`, write probe in preflight |
| 8 | Maker in the container, `--check` on the host | Environment skew: the gate certifies work done somewhere else. Worse than no gate, because the log says `pass` | **R-SAME**: check, acceptance, and probe commands use the maker's backend or the run refuses to start |
| 9 | Silent fallback to host when Docker is down | At 03:00 the daemon restarts and the agent keeps working — on the host, unsandboxed | **R-FAIL**: `--docker` makes the sandbox mandatory; preflight failure aborts before the first model call |
| 10 | Mounting `~/.ssh`, `~/.aws`, or the Docker socket | An autonomous agent with the Docker socket has host root. Credentials in the mount are exfiltratable by prompt injection from `fetch_url` | **R-SECRET**: explicit non-goal; env passthrough is an empty-by-default allowlist; `docker.sock` refused by preflight |
| 11 | "Auditor LLM formulates 2 shell commands" sold as the honesty mechanism | Same model, same weights, correlated errors. On a small local model the probe output is frequently unparseable or destructive | **R-DET**: authority lives in deterministic acceptance commands; the probe is opt-in, capped, read-only, downgrade-only |
| 12 | "Any nonzero exit blocks completion" | `grep` no-match is 1, `diff` is 1, `test -f` is 1. A blanket rule teaches the model to append `\|\| true`, destroying the signal | **R-SCOPE**: exit-code authority applies only to designated check/acceptance commands |
| 13 | `signal_task_complete` in the REPL turn loop | Every interactive answer would need a tool call to terminate, for a problem the REPL does not have | Non-goal; see "Rejected" |
| 14 | Bind-mount performance on macOS | A repeated `pytest` cycle runs 2–5× slower, which compounds over 200 overnight cycles | **R-PERF**: named volumes for `.venv` / `node_modules`, `:delegated`; measure before recommending |
| 15 | Sandbox enabled by a config key | A user who typed plain `lmloop` suddenly runs in a container with different tooling, and cannot tell why their shell commands changed behavior | **R-FLAG**: activation is a CLI flag only |
| 16 | A persistent container is just "the same container, kept" | Over weeks it accumulates `pip install`s, apt packages, and multi-GB writable layers. The environment the agent verified in stops resembling the image anyone pinned — the drift R-PIN exists to prevent, arriving through the back door | **R-DRIFT**: persist mode tracks age and writable-layer size, warns past thresholds, and `sandbox reset` is a documented routine |
| 17 | `sleep infinity` as PID 1 in a long-lived container | PID 1 does not reap. Every `docker exec` that leaves a child behind becomes a zombie; after weeks the pid table fills and `--pids-limit` starts rejecting legitimate commands with confusing errors | **R-INIT**: `--init` in persist mode (and ephemeral, for symmetry) |
| 18 | Ephemeral container leaked when the process is killed | `kill -9` on lmloop leaves a container holding the mount and CPU limits forever | **R-REAP**: `finally` + `atexit` removal, plus a label/pid-based orphan sweep on the next `--docker` start |

### What survives review

The core claim holds: **host shell access for an unattended loop is the largest
unmitigated risk in lmloop today**, because `tools.run_shell` is host-native and
`autonomous_gates: all` disables the only thing in front of it. The fix is worth
building — as an opt-in flag with the eighteen requirements above, not as a default and
not as a four-line `docker exec` helper.

### Rejected outright

- **Docker SDK / `docker-py`.** The `docker` CLI over `subprocess` keeps the dependency
  at "a binary that must already exist". A Python SDK adds `requests` / `urllib3` /
  `websocket-client` to a project whose agent loop is deliberately `urllib`-only.
- **A `signal_task_complete` tool.** See #13.
- **Podman/nerdctl as separate backend classes.** `sandbox_docker_bin` covers
  CLI-compatible runtimes; a second class earns its keep when someone reports a break.
- **An in-process scheduler or daemon.** Existing non-goal; 24/7 means an OS supervisor
  (systemd / launchd / tmux) invoking `lmloop --docker-persist until …`.
- **Mounting `~/.lmloop` into the container.** Memory stays host-side because the agent
  process stays host-side. Only shell execution crosses the boundary.

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
  `KeyboardInterrupt` kill semantics. **This is the default and the only backend most
  users will ever construct.**
- `DockerBackend` shells out to `docker`.
- `exec.build_backend(cfg, workspace_root, *, docker: bool, persist: bool)` is the only
  factory. With `docker=False` it returns `LocalBackend` immediately — no PATH lookup,
  no daemon probe, no import cost.

**Import graph:** `exec.py` is a leaf — `subprocess`, `shlex`, `hashlib`, `pathlib`,
`config`. It must not import `tools`, `agent`, `loop`, or `graph`. `tools.py` and
`loop.py` may import `exec`.

**Why a module and not a `tools.py` function:** `loop.run_check`, the acceptance runner,
and `run_shell` all need the identical backend instance (R-SAME).

### 1.2 Container lifecycle

Two modes, one code path, differing in name, flags, and teardown.

| | `--docker` (ephemeral) | `--docker-persist` |
|---|---|---|
| Name | `lmloop-sbx-<slug>-<sha8>-<pid>` | `lmloop-sbx-<slug>-<sha8>` |
| Labels | `lmloop.mode=ephemeral`, `lmloop.pid=<pid>`, `lmloop.workspace=<path>`, `lmloop.image_digest=<d>` | `lmloop.mode=persist`, same others |
| Create | At process start, after preflight | On first use; reattached if already running |
| Restart policy | none | `sandbox_persist_restart` (default `unless-stopped`) so a host reboot restores the 24/7 sandbox |
| Teardown | `docker rm -f` in `finally` + `atexit` (R-REAP) | Survives; removed only by `lmloop sandbox rm/reset` |
| Orphan handling | Next `--docker` start sweeps `lmloop.mode=ephemeral` containers whose `lmloop.pid` is no longer alive | Age and size warnings (R-DRIFT) |

```
run:   docker run -d --init --name <n> \
         --label lmloop.workspace=<ws> --label lmloop.mode=<m> --label lmloop.pid=<pid> \
         --label lmloop.image_digest=<digest> \
         -v <ws>:/workspace -w /workspace \
         --user <uid>:<gid> -e HOME=/tmp/lmloop-home \
         --cap-drop ALL --security-opt no-new-privileges \
         --pids-limit <n> --memory <m> --cpus <c> [network flags] [restart flags] \
         <image@digest> sleep infinity

exec:  docker exec -w /workspace <n> /bin/sh -lc \
         'timeout --signal=TERM --kill-after=5s <N>s <quoted command>'
```

Notes:

- `--init` (R-INIT) is what keeps a month-old persist container from filling its pid
  table with zombies. It costs nothing in ephemeral mode, so it is unconditional.
- Exit code 124 from `timeout` maps to `timed_out=True` and the existing
  `ERROR: command timed out after Ns` string, so `check_status_from_output` and every
  current test stay valid.
- Simple (non-shell-syntax) commands still go through `/bin/sh -lc` because `timeout`
  must wrap them; the command is `shlex.quote`d, and `ShellCommand`'s
  `confirm_shell_syntax` classification still runs on the **original** string so gate
  behavior does not change. This is the one semantic difference between backends and it
  gets its own test.
- The `lmloop.image_digest` label is compared with `sandbox_image` on every attach. A
  persist container built from an older digest is refused with "run `lmloop sandbox
  reset`" rather than silently used (R-PIN through R-DRIFT).

### 1.3 Preflight (fail fast at boot, never mid-run)

Runs only when `--docker` was passed. `DockerBackend.preflight()` returns the first
failure of:

1. `sandbox_docker_bin` exists on PATH.
2. `docker version --format '{{.Server.Version}}'` succeeds within 10s (daemon up).
3. `sandbox_image` contains `@sha256:` and is present locally, else "run `lmloop sandbox build`".
4. Workspace is not `/`, not `$HOME`, and is inside a git repo when `autonomous_snapshot: git`.
5. No `docker.sock` in any configured mount (R-SECRET).
6. Write probe: `touch .lmloop-probe && rm .lmloop-probe`, then confirm on the **host**
   that no root-owned artifact remains (R-UID).
7. Container name is free, or the existing container's `lmloop.workspace` **and**
   `lmloop.image_digest` labels match (R-ID, R-DRIFT).
8. Persist mode only: container age ≤ `sandbox_persist_max_age_d` and writable layer
   (`docker ps -s`) ≤ `sandbox_persist_disk_warn_gb`. Exceeding either **warns and
   continues** — it is a hygiene signal, not a correctness failure.

`cli.py` runs preflight before the first maker step of `until` / `graph` and before the
REPL starts. Failure aborts with a nonzero exit and a one-line reason (R-FAIL). There is
no partial mode.

### 1.4 Ports and networking

| `sandbox_network` | Behavior |
|---|---|
| `bridge` (default) | Publish `sandbox_ports` ranges; `--add-host host.docker.internal:host-gateway` so a containerized command can reach the host model server if it ever needs to |
| `none` | No network. Correct for a pure refactor loop. Note the agent's own `web_search` is unaffected — it runs in the lmloop process on the host |
| `host` | Opt-in, prints `[sandbox · host network — no network isolation]` once per run, documented Linux/WSL2-only |

`sandbox_ports` default `"3000-3010,8000-8010"`. When ranges are published, a generated
line is appended to the injected steering block (`steer.py` owns always-on text): *"Test
servers must bind 0.0.0.0 inside the sandbox on a port in 3000-3010 or 8000-8010; other
ports are unreachable from the host."*

### 1.5 Image

`lmloop/sandbox/Dockerfile`, digest-pinned, deliberately small:

```dockerfile
FROM debian:bookworm-slim@sha256:<pinned>
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git ripgrep curl python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
```

`lmloop sandbox build` builds, reads back the repo digest, and writes it to
`~/.lmloop/config.json`. A tag is never persisted. Projects needing other toolchains set
`sandbox_image` to their own digest-pinned image; the contract is "has `sh`, `timeout`,
`git`, and whatever the project's checks need".

### 1.6 Snapshots (R-SNAP) — on by default, both modes

The default mode has no container at all, so this is the safety net that matters most.

`autonomous_snapshot: git` (default when the workspace is a git repo, else `off`):
before each maker step in `until` / `graph`, create a real commit object off-branch:

```bash
sha=$(git stash create --include-untracked) && \
  git update-ref "refs/lmloop/${RUN_TS}/${STEP}" "${sha:-$(git rev-parse HEAD)}"
```

The index and working tree are untouched, nothing lands on a branch, the ref name goes
into the run JSONL row, and recovery is `git checkout refs/lmloop/…`. Refs prune on the
same 14-day horizon as `trash/`. Clean tree → record `HEAD`, write no ref.

---

## Part 2 — Verification hardening

### 2.1 What is authoritative

| Signal | Authority | Why |
|---|---|---|
| `--check <cmd>` exit code | **Authoritative** (already shipped) | Deterministic, no model in the path |
| `--accept <cmd>` exit codes (new) | **Authoritative** | The human's definition of done |
| Negative baseline transition (new) | **Authoritative when enabled** | Catches a check that passes before any work |
| Eval `STATUS:` line | Necessary, not sufficient | An LLM reading an LLM |
| Probe commands (new, opt-in) | Can only downgrade `pass` → `fail` | Correlated-error supplement |
| Maker prose | Zero | Already true in `loop.py` |
| Any other shell exit code | Evidence, surfaced to the checker | R-SCOPE |

### 2.2 Acceptance commands

```
lmloop until --check 'pytest -q' --accept 'ruff check .' --accept 'python -m build -n' <goal>
node build until --check 'pytest -q' --accept 'ruff check .' implement the agreed change
```

1. They run **after** `--check` passes or eval returns `STATUS: pass`, always through the
   maker's backend (R-SAME).
2. All must exit 0. The first nonzero flips the cycle to `fail`; the clipped output is
   appended as `Acceptance failed: <cmd> exited N`.
3. They run under the until/graph `GatePolicy` with denials dropped, like `--check`
   today, so they cannot become a mutation vector.
4. They are recorded as an `accept` row in the run JSONL.
5. They are **never LLM-authored**. `graph propose` may suggest one in a draft a human
   approves; nothing writes an acceptance command at runtime.

### 2.3 Negative baseline (`require_negative_baseline`, default false)

1. Before the first maker step, run every acceptance command; record exits as a
   `baseline` row.
2. A run may reach `done/pass` only if **at least one** acceptance command went nonzero
   at baseline and zero at the end.
3. If all passed at baseline, stop with `blocked`: *"acceptance commands already pass —
   nothing to prove; tighten the check or the goal"*.

### 2.4 Unverified-exit annotation (deterministic, always on)

One line computed from the maker's tool transcript, not its prose:

```
Unverified: the maker's last shell command exited 2 (`pytest -q tests/test_loop.py`).
```

Omitted when the last shell exit was 0 or there was no shell call.

### 2.5 Adversarial probe (`eval_probe`, default false)

Only after eval says `pass` **and** acceptance passes:

- One isolated `act()`, `readonly=True`, `max_rounds=1`, frozen prompt, handoff only.
- Contract: a fenced block with ≤ `eval_probe_max_cmds` (default 2) commands. Parse
  failure → `probe/skipped`, never blocks (R-DET).
- Commands run through the maker backend under the `GatePolicy`; destructive requests are
  `DENIED` and dropped.
- Any nonzero exit → cycle becomes `fail`.
- Auto-disabled below `probe_min_context` (default 8192) — on a small local model the
  probe is noise. Note this interacts with the capacity model in the companion doc: a
  probe is an extra serialized model call, and on a single-slot local server it costs
  real wall-clock per cycle.

### 2.6 Rejected in this part

Blanket "nonzero exit blocks the turn" (R-SCOPE); a second model or second `base_url`
for the auditor; parsing `"I am finished"`.

---

## Part 3 — Config, commands, telemetry

### 3.1 New config keys

All sandbox keys are **inert unless `--docker` was passed**.

| Key | Default | Meaning |
|---|---|---|
| `sandbox_image` | `""` | Must contain `@sha256:`; written by `lmloop sandbox build` |
| `sandbox_docker_bin` | `docker` | `podman` works here |
| `sandbox_network` | `bridge` | `bridge` \| `none` \| `host` |
| `sandbox_ports` | `3000-3010,8000-8010` | Published ranges when `bridge` |
| `sandbox_memory` | `4g` | `--memory` |
| `sandbox_cpus` | `2` | `--cpus` |
| `sandbox_pids` | `512` | `--pids-limit` |
| `sandbox_env_passthrough` | `""` | Comma-separated allowlist, empty = none |
| `sandbox_persist_restart` | `unless-stopped` | Restart policy for persist containers only |
| `sandbox_persist_max_age_d` | `7` | Warn and suggest `sandbox reset` past this age (R-DRIFT) |
| `sandbox_persist_disk_warn_gb` | `5` | Warn when the writable layer exceeds this |
| `autonomous_snapshot` | `git` | **Applies to host runs too**; `git` \| `off` |
| `require_negative_baseline` | `false` | §2.3 |
| `eval_probe` | `false` | §2.5 |
| `eval_probe_max_cmds` | `2` | Cap |
| `probe_min_context` | `8192` | Below this, probe is skipped |

Every key gets a README row in the same diff.

### 3.2 Flags and the one new stem

Root-parser flags, accepted by every subcommand: `--docker`, `--docker-persist`
(implies `--docker`), `--docker-image <ref@sha256:…>` (one-run override).

`sandbox` is one new `CommandMeta` with
`arg_choices = ("build","status","shell","reset","rm")`:

| Command | Effect |
|---|---|
| `lmloop sandbox build` | Build the pinned image, persist the digest |
| `lmloop sandbox status` | Preflight result, container state/mode/age/size, mounts, limits, ports; sweeps dead-pid ephemeral orphans |
| `lmloop sandbox shell` | `docker exec -it` for a human debugging the environment |
| `lmloop sandbox reset` | `rm -f` + recreate — the documented cure for persist drift |
| `lmloop sandbox rm` | Remove the container, keep the image |

`sandbox status/reset` mirror into the REPL as `/sandbox`. `/stats` gains one line, and
it must be honest about the default:

```
exec: local (host)
exec: docker · persist · lmloop-sbx-repo-1a2b3c4d · age 2d · layer 1.4G · bridge 3000-3010
```

### 3.3 Run-log additions (additive, backward compatible)

New roles `baseline`, `accept`, `probe`; new optional fields `snapshot_ref`, `backend`
(`local` / `docker:ephemeral` / `docker:persist`). `next_role` gains
`("accept","fail") -> "maker"`, `("accept","pass") -> after_pass`,
`("probe","fail") -> "maker"`, `("probe","pass"|"skipped") -> after_pass`.

Old logs under a new binary behave identically. New logs under an old binary hit
`next_role`'s `return "maker"` fallback — degraded, not corrupt. **Downgrade is safe; it
re-runs a maker step.**

---

## Part 4 — Task breakdown

### Phase S — sandbox (opt-in)

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| S1 | `exec.py` with `ExecResult`, `ExecBackend`, `LocalBackend` extracted verbatim; `build_backend(docker=False)` short-circuit | `exec.py`, `tools.py` | Local run, nonzero exit, timeout kill, interrupt kill; `build_backend` with `docker=False` never touches PATH; existing shell tests unchanged | `run_shell` has no `subprocess` import, zero behavior delta |
| S2 | `--docker` / `--docker-persist` / `--docker-image` on the root parser, threaded to `build_backend` | `cli.py`, `repl.py` | No flag → `LocalBackend`; `--docker-persist` implies `--docker`; no config key can enable it | R-FLAG is a test |
| S3 | `DockerBackend`: name derivation per mode, run/exec/inspect argv, `--init`, `timeout` wrapper, 124 mapping, `shlex.quote` | `exec.py` | Fake `subprocess` asserting exact argv; name stable per path, differs per path, ephemeral carries pid | Argv asserted, not smoke-tested |
| S4 | Lifecycle: ephemeral `finally`/`atexit` teardown + dead-pid orphan sweep; persist attach, restart policy, digest-label check | `exec.py` | `kill -9` simulation leaves an orphan that the next start reaps; digest mismatch refuses with the reset hint | R-REAP and R-DRIFT are tests |
| S5 | `preflight()` (all eight checks) + abort wiring | `exec.py`, `cli.py`, `status.py` | Each check fails independently with its own message; abort is nonzero and precedes any model call | A dead daemon aborts before the first token |
| S6 | Same-backend enforcement for check/acceptance/probe | `loop.py`, `graph.py`, `tools.py` | A test proves `run_check` cannot run locally while the maker is containerized | R-SAME is a unit test |
| S7 | `sandbox` stem, `/sandbox`, `/stats` exec line | `commands.py`, `cli.py`, `repl.py`, `ui.py` | Routing; `status` output against a fake docker; `/stats` says `local (host)` by default | `/help` lists `sandbox` once |
| S8 | Image + digest persistence + drift detection | `sandbox/Dockerfile`, `exec.py`, `config.py` | Config rejects a reference without `@sha256:` | A tag can never be persisted |
| S9 | Port ranges → steering line; `host` network warning | `steer.py`, `exec.py` | Steering contains configured ranges only when publishing; warning printed once | The model learns the port contract only when it applies |
| S10 | Docs: README (flags, Safety, config table, troubleshooting), ARCHITECTURE (component map, `run_shell` safety cell), DEVELOPMENT (module row, import-graph line) | docs | — | Docs state plainly that the default is host execution |

### Phase V — verification (no sandbox required)

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| V1 | `--accept` parsing (CLI, `/until`, graph nodes), stored on `UntilRun` meta and `NodeDef` | `loop.py`, `graph.py`, `cli.py`, `repl.py` | Quoted commands survive `shlex`; repeats accumulate; parse errors fail closed | `--accept` round-trips through `parse_graph` |
| V2 | Acceptance runner + `accept` row + `next_role` entries | `loop.py`, `graph.py`, `status.py` | Eval pass + acceptance fail → maker; output clipped and cited; denial dropped | An eval `pass` cannot finish a run alone |
| V3 | `require_negative_baseline` | `loop.py`, `status.py` | Baseline all-pass → `blocked` with the exact message; fail→pass → `done` | A pre-passing check cannot certify a run |
| V4 | `Unverified: last shell exited N` annotation | `loop.py`, `agent.py` (transcript accessor) | Present on nonzero, absent on zero | The checker sees exit evidence with no model call |
| V5 | `eval_probe` | `loop.py`, `status.py` | Unparseable → skipped; destructive → DENIED; nonzero → fail | Probe can only downgrade |
| V6 | Docs + `Status: shipped` | docs | — | The until flow shows baseline/accept/probe |

### Phase R — recovery (applies to the default host path)

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| R1 | `autonomous_snapshot: git` pre-maker refs, `snapshot_ref` on rows, 14-day prune | `loop.py`, `graph.py`, `snapshot.py` (leaf) | Clean tree → HEAD, no ref; dirty tree → ref contains the dirt; non-git → `off` with a note | `git checkout <ref>` recovers a pre-step tree |
| R2 | Docs: "recover from an autonomous run" next to the `trash/` row | docs | — | Recovery documented beside the existing backup story |

### Sequencing

- **S1 is worth landing alone** — it removes `subprocess` from `tools.py` and creates the
  seam, with no user-visible change.
- **R1 next.** The default execution mode is the host, so the snapshot net protects the
  people who never type `--docker`.
- **V1–V5 are independent of Phase S** and improve honesty on the on-device path today.
- **S6 gates calling the sandbox done**; S3 without S6 reproduces hazard #8 exactly.

---

## Part 5 — Risk register

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| Agent destroys the workspace (host mode, the default) | P0 | R-SNAP git refs, existing gates and `trash/` | Ignored/untracked files (`.venv`) are not snapshotted by design |
| Docker absent/broken mid-run under `--docker` | P0 | R-FAIL abort + preflight before first model call | Run stops; `/continue` resumes after the human fixes it |
| Cross-project container reuse | P0 | R-ID hash + label verification; pid in ephemeral names | Same path reused later: `sandbox reset` |
| Persist container drifts from the pinned image | P1 | R-DRIFT age/size warnings + digest label check + `sandbox reset` | Warnings can be ignored; drift is hygiene, not enforcement |
| Zombie accumulation in a month-old container | P1 | R-INIT | None known |
| Leaked ephemeral container after `kill -9` | P1 | R-REAP sweep on next start | A container lingers until the next `--docker` run |
| Runaway process survives timeout | P1 | R-TMO in-container `timeout` + client guard | SIGTERM-ignoring process is SIGKILLed after 5s |
| Host resource exhaustion | P1 | R-LIM cgroup flags | Disk is not limited; `sandbox status` reports layer size |
| Acceptance commands become a rubber stamp | P1 | `require_negative_baseline` | Off by default; recommended for unattended runs |
| Probe emits destructive commands | P1 | readonly + GatePolicy + cap 2 + downgrade-only | Wastes a cycle on a weak model; context-gated |
| Bind-mount slowness on macOS | P2 | Named volumes, `:delegated` | Measure per project |

---

## Part 6 — Test matrix (no network, no real Docker)

Every Docker interaction is asserted at the **argv** level against a fake `subprocess`,
in the style of `test_tools.py`'s existing `Popen` patching. No test requires a daemon; a
documented manual smoke check covers the real thing.

- **Default path first:** with no flag, `build_backend` returns `LocalBackend`, `docker`
  is never invoked, and the entire existing suite passes unmodified. That is the
  acceptance criterion for "no behavior delta".
- Argv: `run` includes `--init`, `--user`, `--cap-drop`, `--pids-limit`, `--memory`,
  `--cpus`, `--security-opt`; persist adds the restart policy; `exec` wraps in
  `timeout --signal=TERM --kill-after=5s`.
- Names/labels: same path → same name; different path → different; ephemeral carries
  pid; label or digest mismatch → refuse.
- Lifecycle: ephemeral removed on normal exit and on exception; orphan with a dead pid
  swept; persist survives and reattaches.
- Exit mapping: 0/1/124, and a docker-CLI failure that must be distinguishable from a
  command failure (a daemon error must not read as "your tests failed").
- Gates: destructive commands inside the sandbox still hit `GatePolicy`; `DENIED` string
  unchanged.
- Verification: acceptance fail after eval pass → maker; baseline all-pass → blocked;
  probe unparseable → skipped; probe nonzero → fail.
