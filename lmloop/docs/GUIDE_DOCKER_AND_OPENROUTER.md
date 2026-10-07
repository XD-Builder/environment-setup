# Guide: OpenRouter and Docker sandbox

This guide covers **using lmloop with remote models (OpenRouter)** today, and **opt-in
Docker execution** as specified in the design docs (sandbox CLI flags are not shipped yet;
see [Status](#docker-sandbox-status) below).

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

---

## Docker sandbox

### Docker sandbox status

**Execution sandbox flags (`--docker`, `lmloop sandbox …`) are proposed, not implemented** in
this repository revision. Default behavior is unchanged: `run_shell` and checks run on the
**host** via `subprocess`.

When shipped, the UX will match the design:

| Invocation | Behavior |
|------------|----------|
| `lmloop …` (default) | Host shell; Docker never invoked |
| `lmloop --docker …` | Ephemeral container; removed on exit |
| `lmloop --docker-persist …` | Long-lived container for supervisors |

No config key will enable Docker; only CLI flags will. Config will tune an **already flagged**
run (`sandbox_image`, `sandbox_network`, … — see design §3.1).

### Why use Docker (when available)

- **ABI correctness:** Linux tools inside the container match CI/Linux deploy targets; host
  `.venv` / `node_modules` can be shadowed so macOS binaries are not bind-mounted into Linux.
- **Blast radius:** Agent shell runs inside a labeled container with caps dropped, memory/CPU
  limits, and optional `sandbox_network: none`.
- **Honest limits:** `bridge` is **not** full host isolation — see the network table below.

### Prepare the image (today)

You can build the reference image before the CLI exists:

```bash
cd lmloop
docker build -f sandbox/Dockerfile -t lmloop-sandbox:local sandbox/
docker inspect lmloop-sandbox:local --format='{{index .RepoDigests 0}}'
```

When `lmloop sandbox build` ships, it will digest-pin (`@sha256:…`) and write `sandbox_image`
to config.

### Network modes (when `--docker` ships)

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
| Preflight fails before first token | Daemon down, image digest missing locally, or port range busy on 127.0.0.1 |
| Checks pass locally but fail in container | Check ran on host while maker was in container (design R-SAME — same backend enforced when shipped) |
| Wrong Python/node binaries | Host `.venv` visible — use shadow volumes / `sandbox reset --deps` when shipped |
| `--mirror` / registry pull issues | Document your registry mirror in Docker daemon config; lmloop does not wrap `docker pull` |

---

## Combined example (future)

When both features ship:

```bash
export OPENROUTER_API_KEY='sk-or-…'
lmloop config set base_url 'https://openrouter.ai/api/v1'
lmloop config set auto_start_server false
lmloop --docker until --check 'python -m unittest discover -s tests -q' 'fix the failing tests'
```

Until then, use OpenRouter **without** `--docker`, or run Docker manually and keep using host
lmloop for the agent loop.
