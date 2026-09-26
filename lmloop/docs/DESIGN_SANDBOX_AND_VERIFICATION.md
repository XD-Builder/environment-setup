# Design: Execution sandbox and verification hardening

**Status:** proposed (nothing here is implemented)
**Date:** 2026-09-19 · **Revised:** 2026-09-26 (review round 3)
**Depends on:** `tools.run_shell`, `tools.GatePolicy`, `loop.run_until`, `graph.run_graph`
**Companion:** [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md)

Two questions drive this file:

1. Should autonomous shell execution leave the host? **Only when the user asks for it
   on the command line.** The default stays exactly what ships today: local execution,
   local model, no container runtime anywhere in the path.
2. How does the loop stop believing the model? **Deterministic check commands with a
   recorded baseline.** An adversarial LLM auditor is a supplement, not the mechanism.

## Review log (round 3)

Defects found by re-auditing revision 2 against the code and against real `git` /
Docker behavior. Each is fixed in the body below; this table exists so a reviewer can
diff intent, not just text.

| # | Sev | Defect in revision 2 | Evidence | Fix (section) |
|---|---|---|---|---|
| 1 | P0 | Snapshot command `git stash create --include-untracked` **silently drops untracked files**: it exits 0 and prints nothing when only untracked files changed, so the "safety net" recorded `HEAD` | Reproduced on git 2.43 | Temp-index plumbing, verified to capture untracked files without touching the index or tree (§1.6) |
| 2 | P0 | `-p 3000-3010:3000-3010` binds **0.0.0.0** — every agent test server exposed to the LAN | Docker default publish address | `-p 127.0.0.1:…` (§1.4) |
| 3 | P0 | `sh -lc 'timeout … <quoted command>'` breaks shell-syntax commands (`timeout` tries to exec a program literally named `a \| b`) and `-l` sources login profiles nondeterministically | Quoting analysis | Two argv shapes, no login shell (§1.2) |
| 4 | P0 | Host-built `.venv` / `node_modules` in the bind mount are **wrong-platform** binaries inside a Linux container (Mach-O on macOS); container-built ones then break the host. Revision 2 filed this under "performance" | Platform ABI | Per-workspace named volumes shadow dependency dirs (§1.5) |
| 5 | P1 | `bridge` described as host isolation. It is not: a bridged container reaches host services bound to 0.0.0.0 through the gateway, Docker Desktop always resolves `host.docker.internal`, revision 2 *added* `--add-host` on purpose, and outbound internet (exfiltration) is unrestricted | Docker networking | R-NET rewritten honestly; no `--add-host`; `none` recommended for untrusted input (§1.4) |
| 6 | P1 | Probe called "read-only" but the readonly tool set still includes `run_shell`, shell redirects and `pip install` mutate, and a `max_rounds=1` act may call tools itself | `tools.READONLY_OMIT` keeps `run_shell` | Probe act is `no_tools`; probe commands are simple argv only; under `--docker` they run in a sibling container with the workspace mounted `:ro`, which is actually enforceable (§2.5) |
| 7 | P1 | Negative baseline blocks legitimate refactor goals, where tests are **supposed** to pass before and after | Rule analysis | `--keep` invariants vs `--accept` transitions; baseline also covers `--check` (§2.3) |
| 8 | P1 | "Unverified: last shell exited N" is biased: a trailing `grep` no-match (exit 1) is framed as a failure, contradicting R-SCOPE | Rule analysis | Neutral "Shell evidence" table of the last three commands (§2.4) |
| 9 | P1 | "Downgrade is safe" was false for graphs: `GraphRun.next_step` returns `("run", defn.start)` for an unknown role, so an old binary would **restart the whole graph** | `graph.py:377` | Graph logs get no new roles; verification folds into the `node` row, as eval already does (§3.3) |
| 10 | P1 | Persist container keeps old limits/network after config changes | Lifecycle analysis | `lmloop.config_hash` label; mismatch refuses with the reset hint (§1.2) |
| 11 | P1 | Two containers on one workspace (persist + ephemeral, or two processes) and host port collisions were possible | Lifecycle analysis | Preflight refuses a second live container per workspace and checks port availability (§1.3) |
| 12 | P2 | Preflight check "inside a git repo when snapshots are on" was tautological (snapshot default already depends on git); "no docker.sock in configured mounts" guarded mounts the design never defined | Self-contradiction | v1 has exactly one bind mount plus shadow volumes; no user mount config exists to check (§1.3) |
| 13 | P2 | `--user uid:gid` with no passwd entry: `HOME` does not exist, and tools calling `getpwuid` (git identity, ssh) fail | Container UID behavior | `HOME` created at start; git identity passed via env; limitation documented (§1.2) |
| 14 | P2 | `--docker-image` override did not require a digest, bypassing R-PIN for one run | Flag spec | Same `@sha256:` rule as config (§3.2) |

## Activation model (read this first)

| Invocation | Shell execution | Container |
|---|---|---|
| `lmloop …` (default, unchanged) | Host `subprocess`, exactly as today | None. `docker` is never invoked, never probed |
| `lmloop --docker …` | Inside an **ephemeral** container removed when the process exits | Created at start, removed at exit, orphans reaped on next start |
| `lmloop --docker-persist …` | Inside a **long-lived** container that survives process exit | Named, reattached each run, for 24/7 supervisors |

No config key can turn the sandbox on; config only parameterizes a sandbox the flag
requested. `--docker-persist` implies `--docker`. Once the flag is present there is no
fallback to the host. Git snapshots (§1.6) default on in **both** modes, because the
default mode has no container at all.

---

## Part 0 — Requirements from the adversarial review

| ID | Requirement | Why |
|---|---|---|
| R-FLAG | Activation is a CLI flag only | A config key would silently containerize a user who typed `lmloop` |
| R-SNAP | Snapshot before every autonomous maker step, both modes, including untracked files | A bind-mounted workspace is the blast radius; `backup_file` covers only file tools |
| R-NET | Default `bridge`, published ports bound to loopback, no `--add-host`; `none` for untrusted input; `host` opt-in with a warning | `bridge` limits *inbound* exposure, not host reachability or egress (see §1.4) |
| R-ID | Container identity from the workspace realpath hash; ephemeral names add the pid; labels verified before reuse | A fixed name lets project A's agent run in project B's mount |
| R-TMO | `timeout` inside the container plus a client-side guard | Killing the `docker` CLI does not kill the process in the container |
| R-LIM | `--pids-limit`, `--memory`, `--cpus`, `--cap-drop ALL`, `no-new-privileges` | A runaway in a container still exhausts the host and can OOM-kill LM Studio |
| R-PIN | Images by `@sha256:` only, everywhere a reference is accepted | Floating tags are non-deterministic builds |
| R-UID | Host uid/gid, created `HOME`, host-side ownership probe | Root-owned files in the user's repo |
| R-SAME | Check, acceptance, and probe commands use the maker's backend | A host-side gate certifies work done somewhere else |
| R-FAIL | Preflight failure aborts before the first model call | Silent fallback to the host is the failure this file prevents |
| R-SECRET | No credential mounts, no docker socket, empty env passthrough by default | Prompt injection via `fetch_url` can exfiltrate anything mounted |
| R-ABI | Dependency directories are shadowed by per-workspace named volumes | Host and container binaries are not interchangeable |
| R-DET | Authority lives in deterministic commands; the probe can only downgrade | Same-model auditing has correlated errors |
| R-SCOPE | Exit-code authority applies only to designated commands | A blanket rule teaches `\|\| true` |
| R-DRIFT | Persist mode verifies image digest and config hash, warns on age and layer size | A long-lived container stops resembling what was pinned |
| R-INIT | `--init` in every container | `sleep infinity` as PID 1 never reaps zombies |
| R-REAP | Ephemeral teardown in `finally` + `atexit`, dead-pid orphan sweep on next start | `kill -9` leaks containers |
| R-COMPAT | New log rows must be safe under an old binary's resume logic | `GraphRun.next_step` restarts from the start on unknown roles |

### Rejected outright

- **Docker SDK / `docker-py`** — adds `requests`/`urllib3`/`websocket-client` to a
  project whose loop is deliberately `urllib`-only. The CLI over `subprocess` suffices.
- **A `signal_task_complete` tool** — `until`/`graph` already ignore prose; the REPL has
  a human reading every reply.
- **Podman/nerdctl backend classes** — `sandbox_docker_bin` covers CLI-compatible runtimes.
- **An in-process daemon** — 24/7 means an OS supervisor invoking
  `lmloop --docker-persist until …`.
- **Mounting `~/.lmloop`** — memory stays host-side because the agent process does.
- **User-configurable extra mounts in v1** — every extra mount is a new exfiltration and
  corruption surface; the one bind mount plus shadow volumes is the whole contract.

---

## Part 1 — Execution backend

### 1.1 New module: `lmloop/exec.py`

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
    def run(self, argv_or_script: "list[str] | str", *, timeout_s: int,
            cwd: Path, readonly: bool = False) -> ExecResult: ...
    def preflight(self) -> "str | None": ...
    def describe(self) -> str: ...
```

- `LocalBackend` is today's `subprocess.Popen` path moved verbatim, including
  `KeyboardInterrupt` kill semantics. It rejects `readonly=True` with a clear error — the
  host cannot enforce it, and pretending otherwise is how revision 2 went wrong (§2.5).
- `exec.build_backend(cfg, workspace_root, *, docker: bool, persist: bool)` is the only
  factory. With `docker=False` it returns `LocalBackend` without a PATH lookup.
- `run` takes a **list** for simple commands and a **str** for shell syntax, mirroring
  the existing `run_shell` split (`argv = command if shell_syntax else shlex.split(command)`),
  so the classification stays in `ShellCommand` where it already lives.

**Import graph:** `exec.py` is a leaf (`subprocess`, `shlex`, `hashlib`, `pathlib`,
`config`); it must not import `tools`, `agent`, `loop`, or `graph`.

### 1.2 Container lifecycle

| | `--docker` (ephemeral) | `--docker-persist` |
|---|---|---|
| Name | `lmloop-sbx-<slug>-<sha8>-<pid>` | `lmloop-sbx-<slug>-<sha8>` |
| Labels | `lmloop.mode`, `lmloop.pid`, `lmloop.workspace`, `lmloop.image_digest`, `lmloop.config_hash` | same |
| Create | At start, after preflight | First use; reattached while running |
| Restart policy | none | `sandbox_persist_restart` (default `unless-stopped`) |
| Teardown | `docker rm -f` in `finally` + `atexit` | Only via `lmloop sandbox rm/reset` |
| Orphans | Next start sweeps `lmloop.mode=ephemeral` containers whose pid is dead | Digest/config-hash mismatch refuses; age/size warns |

`lmloop.config_hash` is `sha8` of the normalized values that are baked in at creation
(image, network, ports, memory, cpus, pids, env allowlist, shadow dirs). Changing any of
them makes an existing persist container refuse with "config changed — run
`lmloop sandbox reset`" rather than silently running with stale limits.

```
docker run -d --init --name <n> <labels…> \
  -v <ws>:/workspace -v lmloop-dep-<sha8>-venv:/workspace/.venv … \
  -w /workspace --user <uid>:<gid> -e HOME=/tmp/lmloop-home \
  -e GIT_AUTHOR_NAME -e GIT_AUTHOR_EMAIL -e GIT_COMMITTER_NAME -e GIT_COMMITTER_EMAIL \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit <n> --memory <m> --cpus <c> [network flags] [restart flags] \
  <image@digest> sleep infinity
docker exec <n> mkdir -p /tmp/lmloop-home          # once, after create
```

Exec has two shapes, and neither uses a login shell:

```
simple:  docker exec -w /workspace <n> timeout --signal=TERM --kill-after=5s <N>s -- <argv…>
shell:   docker exec -w /workspace <n> timeout --signal=TERM --kill-after=5s <N>s /bin/sh -c <script>
```

Each element is a separate argv entry to `subprocess`, so there is no second layer of
quoting to get wrong. GNU `timeout` (without `--foreground`) signals its whole process
group, so a pipeline's children die with it. Exit 124 maps to `timed_out=True` and the
existing `ERROR: command timed out after Ns` string; the client-side guard is `N + 15s`
for a hung daemon.

Git identity values are read from the host's `git config` once at start and passed as
env, because the container uid has no passwd entry. Tools that call `getpwuid` for
other reasons (ssh, some package managers) may still fail; that is documented, not
papered over with `nss_wrapper` in v1.

### 1.3 Preflight (only with `--docker`; fail fast before any model call)

1. `sandbox_docker_bin` on PATH.
2. `docker version --format '{{.Server.Version}}'` succeeds within 10s.
3. The image reference contains `@sha256:` and is present locally.
4. Workspace is not `/`, not `$HOME`, and no path component is a Docker socket.
5. Write probe `touch .lmloop-probe && rm .lmloop-probe`; confirm on the host that no
   root-owned artifact remains.
6. No **other** running container carries this `lmloop.workspace` label (one sandbox per
   workspace, whatever its mode).
7. Our own container, if present, matches `lmloop.image_digest` and `lmloop.config_hash`.
8. Every host port in `sandbox_ports` is free on `127.0.0.1` (bind-and-close test).
9. Persist only: age ≤ `sandbox_persist_max_age_d`, writable layer ≤
   `sandbox_persist_disk_warn_gb` — **warn and continue**.

### 1.4 Networking, stated honestly

| `sandbox_network` | Inbound from LAN | Reach host services | Internet egress | Use for |
|---|---|---|---|---|
| `bridge` (default) | Only published ports, bound to `127.0.0.1` | **Yes** for anything the host binds on 0.0.0.0 (via the bridge gateway; always on Docker Desktop) | Yes | Normal development |
| `none` | No | No | No | Untrusted input: tasks that `fetch_url` arbitrary pages, or any unattended persist run that doesn't need the network |
| `host` | Whatever the process binds | Yes, including loopback | Yes | Opt-in, Linux/WSL2 only, printed warning |

`bridge` is an inbound-exposure control and a namespace boundary, **not** host isolation
and not exfiltration protection. Revision 2 oversold it. No `--add-host` is added: the
agent process talks to the model server from the host, so the container has no reason to.

Ports publish as `-p 127.0.0.1:3000-3010:3000-3010 -p 127.0.0.1:8000-8010:8000-8010`.
When ranges are published, `steer.py` appends: *"Test servers must bind 0.0.0.0 inside
the sandbox on a port in 3000-3010 or 8000-8010; the host reaches them at 127.0.0.1."*
Binding 0.0.0.0 *inside* the container is correct and does not expose the host, because
the host side of the publish is loopback.

### 1.5 Image and dependency directories (R-PIN, R-ABI)

`lmloop/sandbox/Dockerfile`, digest-pinned and small:

```dockerfile
FROM debian:bookworm-slim@sha256:<pinned>
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git ripgrep curl python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
```

`lmloop sandbox build` records the repo digest in config; a tag is never persisted.

Dependency directories are a **correctness** problem, not a performance one. A `.venv`
built on macOS contains Mach-O binaries that cannot run in a Linux container, and a
`.venv` built in the container breaks the host. `sandbox_shadow_dirs` (default
`.venv,node_modules`) mounts a named volume `lmloop-dep-<sha8>-<dir>` over each listed
path inside the container:

- The host's copy is invisible to the container and untouched by it.
- The volume persists across ephemeral runs, so dependencies install once per workspace,
  not once per run. Under `sandbox_network: none` the first install must happen in a
  networked run; preflight notes an empty shadow volume.
- `lmloop sandbox reset --deps` removes the volumes; plain `reset` keeps them.

### 1.6 Snapshots (R-SNAP) — on by default, both modes

Before each maker step in `until` / `graph`, when the workspace is a git repo and
`autonomous_snapshot: git` (default):

```bash
idx=$(mktemp -d)/index                       # path must not pre-exist: git rejects an empty file
GIT_INDEX_FILE=$idx git read-tree HEAD
GIT_INDEX_FILE=$idx git add -A               # tracked edits + untracked, honoring .gitignore
tree=$(GIT_INDEX_FILE=$idx git write-tree)
sha=$(git commit-tree "$tree" -p HEAD -m "lmloop snapshot <run> <step>")
git update-ref "refs/lmloop/<run-ts>/<step>" "$sha"
```

Verified on git 2.43: the snapshot commit contains modified tracked files and untracked
files, and the user's real index and working tree are unchanged afterwards. The ref
name goes into the run row; recovery is `git checkout refs/lmloop/…` or
`git restore --source=refs/lmloop/… -- <path>`.

Guards:

- **Size:** untracked files larger than `snapshot_max_file_mb` (default 20) are excluded
  via a pathspec, and the row lists what was skipped. An agent that writes a 2 GB
  artifact must not bloat `.git` by 2 GB per cycle.
- **Clean tree:** when `write-tree` equals `HEAD^{tree}`, no ref is written and the row
  records `HEAD`.
- **Retention:** refs older than 14 days are deleted (same horizon as `trash/`);
  objects are reclaimed by the user's normal `git gc`.
- **Push safety:** `refs/lmloop/*` is outside every default refspec, so `git push` never
  sends it; `git push --mirror` would, and the README says so.

---

## Part 2 — Verification hardening

### 2.1 What is authoritative

| Signal | Authority |
|---|---|
| `--check <cmd>` exit code | **Authoritative** (shipped) |
| `--accept <cmd>` exit codes (new) | **Authoritative** — must pass at the end |
| `--keep <cmd>` exit codes (new) | **Authoritative** — invariants that must pass at the end |
| Baseline transition (new, opt-in) | **Authoritative when enabled** |
| Eval `STATUS:` | Necessary, not sufficient |
| Probe (new, opt-in) | Downgrade-only |
| Maker prose | Zero |
| Other shell exits | Evidence for the checker, never a verdict |

### 2.2 Acceptance and invariant commands

```
lmloop until --check 'pytest -q' --accept 'pytest -q tests/test_new_feature.py' \
             --keep 'ruff check .' <goal>
node build until --check 'pytest -q' --keep 'ruff check .' refactor the parser
```

1. Run **after** `--check` passes (or eval says `pass` when there is no check), through
   the maker's backend (R-SAME), under the `GatePolicy` with denials dropped.
2. All `--accept` and `--keep` commands must exit 0; the first nonzero flips the cycle to
   `fail` with `Acceptance failed: <cmd> exited N` and clipped output in the handoff.
3. **Never LLM-authored.** `graph propose` may suggest one in a draft a human approves.
4. They should tolerate repeated execution (build caches, `.pytest_cache`) — they run at
   baseline and at every passing cycle. The README states this.

### 2.3 Baseline (`require_negative_baseline`, default false)

Baseline covers `--check` and `--accept`, but **not** `--keep`:

1. Before the first maker step, run `--check`, every `--accept`, and every `--keep`; record
   exits once in a `baseline` row. Resume never re-runs it.
2. With the flag on, reaching `done/pass` requires at least one of `--check` / `--accept`
   to have been nonzero at baseline and zero at the end.
3. If they all already pass at baseline, stop with `blocked`: *"check/acceptance commands
   already pass — nothing to prove; tighten them, or use --keep for invariants"*.
4. A `--keep` command that **fails at baseline** stops with `blocked` too: *"invariant
   already broken before any work: <cmd>"* — otherwise the maker is blamed for a
   pre-existing failure and burns cycles on it.

A refactor goal is expressed with `--keep` only and the flag off; a feature goal uses
`--accept` with the flag on. Revision 2 had no way to say the former and would have
blocked every legitimate refactor.

### 2.4 Shell evidence for the checker (deterministic, always on)

Appended to the eval prompt, computed from the maker's tool transcript:

```
Shell evidence (last 3 maker commands, most recent last):
  exit 0  pytest -q tests/test_loop.py
  exit 1  grep -n TODO lmloop/loop.py
  exit 0  ruff check .
```

Neutral wording, no verdict. The checker sees exit codes it would otherwise have to ask
for, without being told that a `grep` no-match is a failure.

### 2.5 Adversarial probe (`eval_probe`, default false)

Only after eval says `pass` **and** acceptance passes:

- One isolated `act()` with **`no_tools=True`** and a frozen prompt: output ≤
  `eval_probe_max_cmds` (default 2) commands in one fenced block. It proposes, it does
  not execute.
- Parse: each line through `ShellCommand`; any line with shell syntax (`needs_shell()`)
  or a destructive classification is dropped. No lines left → `probe/skipped`, never a
  block.
- Execution:
  - `--docker`: in a sibling `docker run --rm` of the same image with the workspace
    mounted **`:ro`**, the same shadow volumes `:ro`, `--network none`, and the same
    limits. Read-only is enforced by the kernel, not by hope.
  - Host: a snapshot ref is taken immediately before the probe, and the row records it.
    The host cannot enforce read-only; the design says so instead of claiming it.
- Any nonzero exit → cycle `fail`, output to the maker. A probe can never produce `pass`.
- Skipped below `probe_min_context` (default 8192), and counted against the capacity
  model in the companion doc — on a single-slot local server it is a full serialized
  model call per passing cycle.

---

## Part 3 — Config, commands, compatibility

### 3.1 Config keys

Sandbox keys are **inert unless `--docker` was passed**.

| Key | Default | Meaning |
|---|---|---|
| `sandbox_image` | `""` | Must contain `@sha256:`; written by `lmloop sandbox build` |
| `sandbox_docker_bin` | `docker` | `podman` works here |
| `sandbox_network` | `bridge` | `bridge` \| `none` \| `host` (§1.4) |
| `sandbox_ports` | `3000-3010,8000-8010` | Published on `127.0.0.1` when `bridge` |
| `sandbox_memory` / `sandbox_cpus` / `sandbox_pids` | `4g` / `2` / `512` | cgroup limits |
| `sandbox_env_passthrough` | `""` | Comma-separated allowlist |
| `sandbox_shadow_dirs` | `.venv,node_modules` | Named-volume shadows (R-ABI) |
| `sandbox_persist_restart` | `unless-stopped` | Persist mode only |
| `sandbox_persist_max_age_d` | `7` | Warn past this age |
| `sandbox_persist_disk_warn_gb` | `5` | Warn past this writable-layer size |
| `autonomous_snapshot` | `git` | **Host runs too**; `git` \| `off` |
| `snapshot_max_file_mb` | `20` | Untracked files above this are excluded and listed |
| `require_negative_baseline` | `false` | §2.3 |
| `eval_probe` / `eval_probe_max_cmds` / `probe_min_context` | `false` / `2` / `8192` | §2.5 |

### 3.2 Flags and the one new stem

Root-parser flags, accepted by every subcommand: `--docker`, `--docker-persist`
(implies `--docker`), `--docker-image <ref@sha256:…>` (one-run override; rejected
without a digest, same rule as config).

`sandbox` is one `CommandMeta` with `arg_choices = ("build","status","shell","reset","rm")`;
`reset --deps` also removes shadow volumes. `/sandbox` mirrors `status`/`reset`.
`/stats` gains one honest line:

```
exec: local (host)
exec: docker · persist · lmloop-sbx-repo-1a2b3c4d · age 2d · layer 1.4G · bridge 127.0.0.1:3000-3010
```

### 3.3 Run-log compatibility (R-COMPAT)

Verified against the resume code, not assumed:

- **Until logs** may gain roles `baseline` and `accept`, because `UntilRun.next_role`
  falls back to `"maker"` for unknown `(role, status)` pairs: an old binary re-runs a
  maker step, which is harmless.
- **Graph logs gain no new roles.** `GraphRun.next_step` returns `("run", defn.start)`
  for an unknown role, so a new role would make an old binary restart the entire graph.
  Acceptance and probe outcomes fold into the existing `node` row's `status` (exactly how
  skill eval is already folded), with details in an optional `verify` field.
- New optional fields everywhere: `snapshot_ref`, `backend`
  (`local` / `docker:ephemeral` / `docker:persist`), `verify`. Readers use `.get`.

---

## Part 4 — Task breakdown

### Phase S — sandbox (opt-in)

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| S1 | `exec.py`: `ExecResult`, `ExecBackend`, `LocalBackend` extracted verbatim; `build_backend(docker=False)` short-circuit; `LocalBackend` rejects `readonly=True` | `exec.py`, `tools.py` | Local run, nonzero, timeout, interrupt; no PATH lookup without the flag; existing shell tests unchanged | `run_shell` has no `subprocess` import, zero behavior delta |
| S2 | `--docker` / `--docker-persist` / `--docker-image` on the root parser | `cli.py`, `repl.py` | No flag → local; persist implies docker; image without digest rejected; no config key enables docker | R-FLAG is a test |
| S3 | `DockerBackend` argv: names, labels, `--init`, both exec shapes, 124 mapping, env identity | `exec.py` | Exact argv asserted against a fake `subprocess`; shell script passed as one argv element; no `-l` anywhere | Quoting is structural, not escaped |
| S4 | Lifecycle: ephemeral teardown + orphan sweep; persist attach, restart policy, digest and config-hash labels | `exec.py` | Simulated `kill -9` orphan reaped; changed memory limit → refuse with reset hint | R-REAP / R-DRIFT are tests |
| S5 | Preflight (nine checks) + abort wiring | `exec.py`, `cli.py`, `status.py` | Each check fails independently; second container per workspace refused; busy port refused | A dead daemon aborts before the first token |
| S6 | Same-backend enforcement for check / accept / keep / probe | `loop.py`, `graph.py`, `tools.py` | `run_check` cannot run locally while the maker is containerized | R-SAME is a unit test |
| S7 | Loopback-only port publishing + steering line; network table in `/sandbox status` | `exec.py`, `steer.py` | Every `-p` argument starts with `127.0.0.1:`; no `--add-host` in any argv | R-NET is a test |
| S8 | Shadow volumes + `reset --deps` | `exec.py` | Each listed dir gets a volume mount; volume name stable per workspace | Host `.venv` never visible in the container |
| S9 | `sandbox` stem, `/sandbox`, `/stats` exec line; image build + digest persistence | `commands.py`, `cli.py`, `repl.py`, `ui.py`, `sandbox/Dockerfile`, `config.py` | Routing; `/stats` says `local (host)` by default; tag never persisted | `/help` lists `sandbox` once |
| S10 | Docs: README (flags, network table, Safety, config, troubleshooting incl. `--mirror` caveat), ARCHITECTURE, DEVELOPMENT | docs | — | Docs say plainly that the default is host execution and that `bridge` is not host isolation |

### Phase R — recovery (protects the default host path; land first)

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| R1 | Temp-index snapshot, size guard, clean-tree shortcut, 14-day prune, `snapshot_ref` on rows | `snapshot.py` (leaf), `loop.py`, `graph.py` | Untracked file present in the snapshot; real index/tree unchanged; oversize file excluded and listed; clean tree writes no ref; non-git → `off` with a note | The regression that sank revision 2 (untracked files lost) is a named test |
| R2 | README: "recover from an autonomous run" beside the `trash/` row | docs | — | Recovery documented where users already look |

### Phase V — verification (no sandbox required)

| ID | Task | Files | Tests | Done when |
|---|---|---|---|---|
| V1 | `--accept` / `--keep` parsing on CLI, `/until`, graph nodes | `loop.py`, `graph.py`, `cli.py`, `repl.py` | Quoted commands survive `shlex`; repeats accumulate; fail-closed parse errors | Round-trips through `parse_graph` |
| V2 | Runner; until `accept` row; graph folds into `node` status + `verify` field | `loop.py`, `graph.py`, `status.py` | Eval pass + acceptance fail → maker; graph log has no new role | An eval `pass` cannot finish a run alone |
| V3 | Baseline with `--keep` semantics | `loop.py`, `status.py` | All pass → blocked; broken `--keep` → blocked; fail→pass → done; resume never re-runs baseline | Refactor and feature goals both expressible |
| V4 | Shell-evidence table | `loop.py`, `agent.py` (transcript accessor) | Last three commands with exits; no verdict words | Checker sees exits, unbiased |
| V5 | Probe: `no_tools` act, argv-only parse, `:ro` sibling container under docker, pre-probe snapshot on host | `loop.py`, `exec.py`, `status.py` | Shell-syntax line dropped; destructive dropped; `:ro` + `--network none` in argv; nonzero → fail; nothing parsable → skipped | Probe can only downgrade, and cannot write under docker |
| V6 | Docs + flip this file to `Status: shipped` | docs | — | Until flow shows baseline/accept/keep/probe |

### Sequencing

R1 → S1 → V1–V4 → S2–S9 → V5. R1 first because the default mode is the host and it has
no other net; S1 creates the seam with no user-visible change; V1–V4 need no sandbox;
V5 last because its enforceable half depends on S3.

---

## Part 5 — Risk register

| Risk | Sev | Mitigation | Residual |
|---|---|---|---|
| Workspace destroyed (host mode, the default) | P0 | R-SNAP with untracked capture | Ignored files and files over the size guard are not snapshotted |
| Docker broken under `--docker` | P0 | R-FAIL | Run stops; `/continue` after the human fixes it |
| Cross-project or duplicate containers | P0 | R-ID + one-container-per-workspace preflight | Same path reused later: `sandbox reset` |
| Agent test servers exposed to the LAN | P0 | Loopback-only publish | None known |
| Wrong-platform dependency binaries | P0 | R-ABI shadow volumes | First install under `network: none` needs a networked run |
| Exfiltration via egress in `bridge` | P1 | Documented; `none` for untrusted input | `bridge` allows egress by design |
| Container reaches host services | P1 | Documented honestly; no `--add-host` | Host services bound to 0.0.0.0 remain reachable in `bridge` |
| Persist drift (image, config, layer) | P1 | R-DRIFT | Age/size are warnings, not enforcement |
| Probe mutates the host workspace | P1 | argv-only, destructive-dropped, pre-probe snapshot | Host read-only is not enforceable; recoverable instead |
| Old binary resumes a new log | P1 | R-COMPAT: no new graph roles | Old until binary re-runs one maker step |
| Runaway process / host exhaustion | P1 | R-TMO, R-LIM, R-INIT | Disk is not limited; layer size is reported |
| Missing passwd entry breaks a tool | P2 | Git identity via env; documented | ssh-style tools may fail in the container |

---

## Part 6 — Test matrix (no network, no real Docker)

Docker interactions are asserted at the **argv** level against a fake `subprocess`, in
the style of `test_tools.py`'s existing `Popen` patching. No test needs a daemon.

- **Default path first:** no flag → `LocalBackend`, `docker` never invoked, the whole
  existing suite passes unmodified.
- Argv: `--init`, `--user`, `--cap-drop`, `--pids-limit`, `--memory`, `--cpus`,
  `--security-opt`, shadow volume mounts, every `-p` prefixed `127.0.0.1:`, no
  `--add-host`, no `-l`; exec wraps `timeout … --` for argv and `/bin/sh -c` for scripts.
- Identity: same path → same name; different path → different; ephemeral carries pid;
  label, digest, or config-hash mismatch → refuse.
- Snapshot (real `git` in a temp repo — fast, no network): untracked captured; index and
  tree untouched; oversize excluded; clean tree → no ref.
- Compat: an until log with `baseline`/`accept` rows resumes to `maker` under the old
  `next_role` table; a graph log written by the new code contains only existing roles.
- Verification: acceptance fail after eval pass → maker; baseline all-pass → blocked;
  broken `--keep` → blocked; probe shell-syntax line dropped; probe nonzero → fail.
