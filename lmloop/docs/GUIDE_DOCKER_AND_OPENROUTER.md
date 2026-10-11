# Guide: OpenRouter and Docker sandbox

This guide covers **using lmloop with remote models (OpenRouter)** today, and **opt-in
Docker execution** on the host (see [Status](#docker-sandbox-status) below).

Design references:

- [DESIGN_SANDBOX_AND_VERIFICATION.md](DESIGN_SANDBOX_AND_VERIFICATION.md) — `--docker`, checks, snapshots
- [DESIGN_MULTI_AGENT_COMPANY.md](DESIGN_MULTI_AGENT_COMPANY.md) — multi-worker company on Docker + OpenRouter
- [DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md](DESIGN_DAG_AND_KNOWLEDGE_CANVAS.md) — `model_concurrency`, remote spend

---

## OpenRouter (remote models)

lmloop talks to any **OpenAI-compatible** `/v1/chat/completions` endpoint. OpenRouter is a
common choice when you want stronger models, tool calling, or more parallel capacity than a
single local GPU slot.

### 1. API key

OpenRouter requires a bearer token. Use **one** of:

| Method | Example |
|--------|---------|
| Environment (recommended) | `export OPENROUTER_API_KEY='sk-or-…'` |
| Alternate env | `export LMLOOP_API_KEY='…'` (used when config `api_key` is empty) |
| Config file | `lmloop config set api_key 'sk-or-…'` (stored in `~/.lmloop/config.json`; treat like a password) |

The key is sent only in the `Authorization: Bearer …` header on chat and model-list requests.
It is **not** injected into prompts or tool output.

### 2. Point lmloop at OpenRouter

```bash
lmloop config set base_url 'https://openrouter.ai/api/v1'
lmloop config set model 'anthropic/claude-sonnet-4'
lmloop config set auto_start_server false
```

`auto_start_server false` skips `lms server start` / `lms load` (LM Studio-only). Remote
runs do not use LM Studio’s native context or vision APIs; set `context_length` manually if
`/stats` shows `limit unknown`, and set `vision false` unless you know the remote model
supports images.

Optional OpenRouter ranking headers (not required for billing):

```bash
export OPENROUTER_HTTP_REFERER='https://your-site.example'
export OPENROUTER_X_TITLE='my-lmloop-project'
```

lmloop forwards these when set (see `config.openrouter_extra_headers()`).

### 3. Verify

```bash
lmloop models          # should list OpenRouter models when the key is valid
lmloop "say hello in one sentence"
```

### 4. Tool calling and `/until`

Pick models OpenRouter marks as supporting **tools** if you rely on `run_shell`, file tools,
or `/until` maker/check loops. The packaged `company` graph and skills behave like local runs;
only the HTTP endpoint changes.

**Privacy:** Remote `base_url` sends prompts, tool output, and (by default) injected memory
to a third party. Trim `context_learnings` / `context_decisions`, avoid `@`-attaching
secrets, and keep `confirm_destructive` on for shell gates.

### 5. Planned remote tuning keys

These appear in the [roadmap config budget](DESIGN_ROADMAP.md#3-config-budget) and are **not**
in lmloop yet; they document intent for OpenRouter-heavy workflows:

| Key (planned) | Purpose |
|---------------|---------|
| `model_concurrency` | Host-wide cap on in-flight requests per `base_url` (`auto` → 4 on remote hosts) |
| `run_token_budget` | Spend ceiling per autonomous run |
| `eval_model` | Cheaper checker model for until/graph eval |
| `recall_sessions` | Default off on remote endpoints (session text in prompts) |

### 6. Multi-agent company (planned)

[DESIGN_MULTI_AGENT_COMPANY.md](DESIGN_MULTI_AGENT_COMPANY.md) describes `lmloop company run
--docker` with an OpenRouter **model allowlist** (`company_models_allowlist`). The default
tier list lives in [`../company/openrouter_autonomous.yaml`](../company/openrouter_autonomous.yaml)
(illustrative IDs — validate against OpenRouter before autonomous use).

**Multi-day runs:** [DESIGN_LONG_HORIZON_PLANNING.md](DESIGN_LONG_HORIZON_PLANNING.md)
defines **campaigns** (durable plan + board + daily reflection) resumed by an OS supervisor
with `lmloop campaign resume … --docker-persist`, optionally bound to `company run --campaign`.

---

## Docker sandbox

### Docker sandbox status

The default is still the host. Docker is used only when you pass a flag. No config key
enables it. `run_shell` and check-plan commands share the active backend for that process
(`LocalBackend` by default, `DockerBackend` after `--docker` / `--docker-persist`).

**GitHub Actions (`docker-local`):** builds `lmloop/sandbox/Dockerfile`, verifies the image
toolchain (Python 3.14, git, ripgrep, curl), and reruns the scripted agent baselines inside
a container with the repo bind-mounted. That job does **not** pass `--docker`; it exercises
`LocalBackend` on the sandbox Linux userland. End-to-end **`--docker`** behavior (preflight,
digest pin, shadow volumes) is covered by **`tests/test_wave2.py`** on runners with Docker.

| Invocation | Behavior |
|------------|----------|
| `lmloop …` (default) | Host shell; Docker is not probed |
| `lmloop --docker …` | Ephemeral container; removed on exit |
| `lmloop --docker-persist …` | Long-lived container for supervisors (implies `--docker`) |
| `lmloop sandbox status` | `exec: local (host)` unless this process passed `--docker` |
| `lmloop sandbox build` | Builds `sandbox/Dockerfile` and stores `image@sha256:…` (never a tag) |
| `lmloop sandbox reset [--deps]` | Removes the persist container; `--deps` also removes shadow volumes |

`sandbox_image` and `sandbox_network` only apply after the flag. `--docker-image` must
contain `@sha256:`. Preflight failure exits before the first model call.

### Why use Docker (when available)

- **ABI correctness:** Linux tools inside the container match CI/Linux deploy targets; host
  `.venv` / `node_modules` can be shadowed so macOS binaries are not bind-mounted into Linux.
- **Blast radius:** Agent shell runs inside a labeled container with caps dropped, memory/CPU
  limits, and optional `sandbox_network: none`.
- **Honest limits:** `bridge` is **not** full host isolation — see the network table below.

### Prepare the image

```bash
cd lmloop
lmloop sandbox build
# writes sandbox_image = <name>@sha256:… to ~/.lmloop/config.json
lmloop --docker-image '<that digest>' until make the tests pass
```

A floating tag is rejected. If `docker image inspect` does not report a repo digest,
`sandbox build` refuses to save one.

### Network modes

| `sandbox_network` | Inbound from LAN | Reach host services | Internet egress | Typical use |
|-------------------|------------------|---------------------|-----------------|-------------|
| `bridge` (default) | Only published ports on `127.0.0.1` | **Yes** for host services on 0.0.0.0 | Yes | Normal dev |
| `none` | No | No | No | Untrusted fetch / unattended |
| `host` | Process binds | Yes, including loopback | Yes | Linux/WSL2 opt-in |

Published port ranges (loopback on the host): **3000–3010** and **8000–8010**. Test servers
inside the container should bind `0.0.0.0` on those ports; you reach them at `127.0.0.1` on
the host.

### Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `LMLOOP_DOCKER` | `docker` | Container CLI (`podman` if Docker-compatible) |

Credentials and the Docker socket are **not** mounted into the sandbox in v1 (design R-SECRET).

### Troubleshooting (Docker)

| Symptom | Likely cause |
|---------|----------------|
| Preflight fails before first token | Daemon down, image digest missing locally, workspace is `/` or `$HOME`, another sandbox holds this workspace, or a port in 3000–3010 / 8000–8010 is busy on 127.0.0.1 |
| Checks pass locally but fail in container | They share the maker's backend. A host `.venv` is not visible inside the container (shadow volume) |
| Wrong Python/node binaries | `lmloop sandbox reset --deps` drops the named volumes so the next run reinstalls them |
| Persist container refuses to attach | Image digest, config hash, or policy bundle changed. `lmloop sandbox reset` (add `--deps` only when you also want new dependency volumes) |
| `--mirror` / registry pull issues | Put the registry mirror in the Docker daemon config. lmloop does not wrap `docker pull`, and `git push --mirror` would publish `refs/lmloop/*` snapshot refs |

---

## Combined example

```bash
export OPENROUTER_API_KEY='sk-or-…'
lmloop config set base_url 'https://openrouter.ai/api/v1'
lmloop config set auto_start_server false
lmloop --docker until --check 'python -m unittest discover -s tests -q' 'fix the failing tests'
```

Company mode is a further opt-in. It refuses loopback unless `company_remote` is true,
and it refuses any model that is not in `company/openrouter_autonomous.yaml` or
`company_models_allowlist`:

```bash
lmloop campaign start --goal "Ship the auth slice"
lmloop --docker company run --goal "Ship the auth slice" --campaign <id>
```

A supervisor can resume the same campaign on later days. State stays on the host;
`--docker-persist` only reattaches the container:

```bash
lmloop --docker-persist campaign resume <id>
```

`campaign_end_of_day_utc` pauses after that UTC hour. `campaign_max_days` (default 30)
requires `lmloop campaign extend` before work continues.
