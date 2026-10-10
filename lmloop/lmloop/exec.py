"""Execution backends for shell commands.

``LocalBackend`` is the default host path. ``DockerBackend`` is opt-in via
``--docker`` / ``--docker-persist`` and talks to a Docker-compatible CLI
(``LMLOOP_DOCKER``, default ``docker``). This module is a leaf: it does not
import tools, agent, loop, or graph.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import cfg_get, project_slug

SANDBOX_PORTS = ("3000-3010", "8000-8010")
SANDBOX_MEMORY = "4g"
SANDBOX_CPUS = "2"
SANDBOX_PIDS = "512"
SANDBOX_SHADOW_DIRS = (".venv", "node_modules")
PERSIST_RESTART = "unless-stopped"
PERSIST_MAX_AGE_D = 7
PERSIST_DISK_WARN_GB = 5
CLIENT_GUARD_EXTRA_S = 15
PREFLIGHT_TIMEOUT_S = 10
DOCKER_BIN_ENV = "LMLOOP_DOCKER"
WORKSPACE_MOUNT = "/workspace"
HOME_IN_CONTAINER = "/tmp/lmloop-home"

_HOST_RE_SRC = r"^[A-Za-z0-9._:-]+$"
_ACTIVE: "ExecBackend | None" = None

PS_FORMAT = (
    "{{.Names}}\t{{.Label \"lmloop.mode\"}}\t{{.Label \"lmloop.pid\"}}\t"
    "{{.Label \"lmloop.workspace\"}}\t{{.Label \"lmloop.image_digest\"}}\t"
    "{{.Label \"lmloop.config_hash\"}}\t{{.Label \"lmloop.policy_bundle\"}}\t"
    "{{.Status}}"
)

RESET_HINT = "config changed — run `lmloop sandbox reset`"
NETWORK_HONEST = (
    "bridge limits inbound exposure; it is not host isolation and it allows egress"
)


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    backend: str
    timed_out: bool = False
    timeout_s: int = 0
    spawn_error: bool = False


@dataclass(frozen=True)
class PreflightReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ContainerRow:
    name: str
    mode: str
    pid: int
    workspace: str
    image_digest: str
    config_hash: str
    policy_bundle: str
    status: str

    @property
    def running(self) -> bool:
        text = (self.status or "").lower()
        return text.startswith("up") or "running" in text


def subprocess_runner(argv: list[str], timeout: "float | None" = None) -> CmdResult:
    """Default Docker CLI runner. Tests inject their own callable."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return CmdResult(124, out, err)
    except OSError as exc:
        return CmdResult(127, "", str(exc))
    return CmdResult(int(proc.returncode or 0), proc.stdout or "", proc.stderr or "")


def docker_binary() -> str:
    return os.environ.get(DOCKER_BIN_ENV, "docker").strip() or "docker"


def image_has_digest(ref: str) -> bool:
    return "@sha256:" in (ref or "")


def workspace_token(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]


def shadow_label(dirname: str) -> str:
    return dirname.lstrip(".") or dirname


def volume_name(token: str, dirname: str) -> str:
    return f"lmloop-dep-{token}-{shadow_label(dirname)}"


def config_fingerprint(
    *,
    image: str,
    network: str,
    memory: str,
    policy_hash: str,
    egress_hash: str,
) -> str:
    blob = "|".join([
        image,
        network,
        memory,
        SANDBOX_CPUS,
        SANDBOX_PIDS,
        ",".join(SANDBOX_PORTS),
        ",".join(SANDBOX_SHADOW_DIRS),
        PERSIST_RESTART,
        policy_hash,
        egress_hash,
    ])
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def bundle_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def load_policy_text(raw: str) -> "tuple[str, str | None]":
    """Return ``(hash, error)``. Empty raw is not a bundle."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return "", f"policy bundle: {exc}"
    if not isinstance(data, dict):
        return "", "policy bundle must be a JSON object"
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return bundle_hash(canonical), None


def parse_allowlist(text: str) -> "tuple[list[str], list[str]]":
    import re
    host_re = re.compile(_HOST_RE_SRC)
    hosts: list[str] = []
    errors: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not host_re.fullmatch(line):
            errors.append(f"bad egress host: {line}")
            continue
        hosts.append(line)
    return hosts, errors


def egress_script(hosts: list[str]) -> str:
    lines = [
        "iptables -P OUTPUT DROP",
        "iptables -A OUTPUT -o lo -j ACCEPT",
        "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
    ]
    for host in hosts:
        lines.append(f"iptables -A OUTPUT -d {host} -j ACCEPT")
    return " && ".join(lines)


def persist_warnings(age_days: "float | None", layer_gb: "float | None") -> list[str]:
    notes: list[str] = []
    if age_days is not None and age_days > PERSIST_MAX_AGE_D:
        notes.append(
            f"persist container is {age_days:.0f}d old "
            f"(warn after {PERSIST_MAX_AGE_D}d); continuing"
        )
    if layer_gb is not None and layer_gb > PERSIST_DISK_WARN_GB:
        notes.append(
            f"persist writable layer is {layer_gb:.1f}GB "
            f"(warn after {PERSIST_DISK_WARN_GB}GB); continuing"
        )
    return notes


def parse_ps(text: str) -> list[ContainerRow]:
    rows: list[ContainerRow] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 8:
            parts.append("")
        try:
            pid = int(parts[2] or "0")
        except ValueError:
            pid = 0
        rows.append(ContainerRow(
            name=parts[0],
            mode=parts[1],
            pid=pid,
            workspace=parts[3],
            image_digest=parts[4],
            config_hash=parts[5],
            policy_bundle=parts[6],
            status=parts[7],
        ))
    return rows


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def port_is_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def ports_in_ranges() -> list[int]:
    found: list[int] = []
    for spec in SANDBOX_PORTS:
        start_s, end_s = spec.split("-", 1)
        found.extend(range(int(start_s), int(end_s) + 1))
    return found


def workspace_rejection(root: Path, home: "Path | None" = None) -> "str | None":
    resolved = root.resolve()
    home_path = (home or Path.home()).resolve()
    if resolved == Path("/"):
        return "workspace is /"
    if resolved == home_path:
        return "workspace is $HOME"
    for part in resolved.parts:
        if "docker.sock" in part:
            return "workspace path contains a Docker socket"
    return None


def git_identity() -> list[tuple[str, str]]:
    mapping = (
        ("user.name", "GIT_AUTHOR_NAME"),
        ("user.email", "GIT_AUTHOR_EMAIL"),
        ("user.name", "GIT_COMMITTER_NAME"),
        ("user.email", "GIT_COMMITTER_EMAIL"),
    )
    # Read each git key once.
    cache: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for git_key, env_name in mapping:
        if git_key not in cache:
            cache[git_key] = _git_config(git_key)
        value = cache[git_key]
        if not value or env_name in seen:
            continue
        seen.add(env_name)
        pairs.append((env_name, value))
    return pairs


def _git_config(key: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def network_args(network: str) -> list[str]:
    if network == "none":
        return ["--network", "none"]
    if network == "host":
        return ["--network", "host"]
    args = ["--network", "bridge"]
    for spec in SANDBOX_PORTS:
        args.extend(["-p", f"127.0.0.1:{spec}:{spec}"])
    return args


def describe_exec() -> str:
    backend = active_backend()
    return backend.describe()


def backend_label() -> str:
    backend = active_backend()
    if isinstance(backend, DockerBackend):
        return "docker:persist" if backend.persist else "docker:ephemeral"
    return "local"


def install_backend(backend: "ExecBackend") -> None:
    global _ACTIVE
    _ACTIVE = backend


def reset_active_backend() -> None:
    global _ACTIVE
    _ACTIVE = None


def active_backend() -> "ExecBackend":
    if _ACTIVE is not None:
        return _ACTIVE
    return LocalBackend()


class LocalBackend:
    """Host subprocess backend. Rejects ``readonly`` — the host cannot enforce it."""

    name = "local"

    def run(
        self,
        command: "str | list[str]",
        *,
        timeout_s: int,
        cwd: Path,
        readonly: bool = False,
        shell_syntax: bool = False,
    ) -> ExecResult:
        if readonly:
            raise ValueError(
                "LocalBackend cannot enforce readonly=True; the host has no such mode"
            )
        try:
            if isinstance(command, list):
                argv: "list[str] | str" = command
                use_shell = False
            elif shell_syntax:
                argv = command
                use_shell = True
            else:
                argv = shlex.split(command)
                use_shell = False
        except ValueError as exc:
            return ExecResult(
                stdout=f"ERROR: {exc}", stderr="", exit_code=127,
                backend=self.name, spawn_error=True,
            )
        try:
            proc = subprocess.Popen(
                argv,
                shell=use_shell,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            return ExecResult(
                stdout=f"ERROR: {exc}", stderr="", exit_code=127,
                backend=self.name, spawn_error=True,
            )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return ExecResult(
                stdout="", stderr="", exit_code=124, backend=self.name,
                timed_out=True, timeout_s=timeout_s,
            )
        except KeyboardInterrupt:
            proc.kill()
            try:
                proc.communicate()
            except KeyboardInterrupt:
                pass
            raise
        return ExecResult(
            stdout=stdout or "",
            stderr=stderr or "",
            exit_code=int(proc.returncode or 0),
            backend=self.name,
        )

    def preflight(self) -> PreflightReport:
        return PreflightReport()

    def describe(self) -> str:
        return "local (host)"

    def close(self) -> None:
        return None


@dataclass
class DockerBackend:
    """Docker CLI backend. ``runner`` is the only subprocess seam (fake in tests)."""

    cfg: dict
    root: Path
    persist: bool = False
    image: str = ""
    runner: object = field(default=subprocess_runner)
    memory: str = SANDBOX_MEMORY
    identity: "list[tuple[str, str]] | None" = None
    started: bool = False
    _closed: bool = False
    _containers: "list[ContainerRow] | None" = None
    _pid_alive: object = field(default=pid_alive)
    _port_free: object = field(default=port_is_free)
    _write_probe: "object | None" = None
    _persist_stats: "object | None" = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.image = (self.image or str(cfg_get(self.cfg, "sandbox_image") or "")).strip()
        self.network = str(cfg_get(self.cfg, "sandbox_network") or "bridge").strip().lower()
        if self.network not in ("bridge", "none", "host"):
            self.network = "bridge"
        self.token = workspace_token(self.root)
        slug = project_slug(self.root)
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in slug)[:40] or "repo"
        self.persist_name = f"lmloop-sbx-{safe}-{self.token}"
        self.ephemeral_name = f"{self.persist_name}-{os.getpid()}"
        self.name = self.persist_name if self.persist else self.ephemeral_name
        self.bin = docker_binary()
        self.policy_hash, self.policy_error = self._read_policy()
        self.egress_hosts, self.egress_errors, self.egress_hash = self._read_egress()
        self.untrusted = (self.root / ".lmloop" / "untrusted").is_file()
        self.config_hash = config_fingerprint(
            image=self.image,
            network=self.network,
            memory=self.memory,
            policy_hash=self.policy_hash,
            egress_hash=self.egress_hash,
        )

    @property
    def mode_name(self) -> str:
        return "persist" if self.persist else "ephemeral"

    def _read_policy(self) -> "tuple[str, str | None]":
        path = policy_bundle_path(self.root)
        if path is None:
            return "", None
        try:
            raw = path.read_text()
        except OSError as exc:
            return "", f"policy bundle: {exc}"
        return load_policy_text(raw)

    def _read_egress(self) -> "tuple[list[str], list[str], str]":
        path = egress_allowlist_path(self.root)
        if path is None:
            return [], [], ""
        try:
            text = path.read_text()
        except OSError as exc:
            return [], [f"egress allowlist: {exc}"], ""
        hosts, errors = parse_allowlist(text)
        digest = bundle_hash("\n".join(hosts)) if hosts else ""
        return hosts, errors, digest

    def volume_names(self) -> list[str]:
        return [volume_name(self.token, name) for name in SANDBOX_SHADOW_DIRS]

    def secret_files(self) -> list[Path]:
        directory = self.root / ".lmloop" / "sandbox-secrets"
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.iterdir() if path.is_file())

    def create_argv(self) -> list[str]:
        argv = [
            self.bin, "run", "-d", "--init", "--name", self.name,
        ]
        labels = {
            "lmloop.mode": self.mode_name,
            "lmloop.workspace": str(self.root),
            "lmloop.image_digest": self.image,
            "lmloop.config_hash": self.config_hash,
        }
        if not self.persist:
            labels["lmloop.pid"] = str(os.getpid())
        if self.policy_hash:
            labels["lmloop.policy_bundle"] = self.policy_hash
        for key, value in labels.items():
            argv.extend(["--label", f"{key}={value}"])
        argv.extend([
            "-v", f"{self.root}:{WORKSPACE_MOUNT}",
        ])
        for dirname, vol in zip(SANDBOX_SHADOW_DIRS, self.volume_names()):
            argv.extend(["-v", f"{vol}:{WORKSPACE_MOUNT}/{dirname}"])
        for path in self.secret_files():
            argv.extend([
                "-v", f"{path}:/run/lmloop-secrets/{path.name}:ro",
            ])
        argv.extend([
            "-w", WORKSPACE_MOUNT,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", f"HOME={HOME_IN_CONTAINER}",
        ])
        identity = self.identity if self.identity is not None else git_identity()
        for key, value in identity:
            argv.extend(["-e", f"{key}={value}"])
        argv.extend([
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", SANDBOX_PIDS,
            "--memory", self.memory,
            "--cpus", SANDBOX_CPUS,
        ])
        argv.extend(network_args(self.network))
        if self.persist:
            argv.extend(["--restart", PERSIST_RESTART])
        argv.extend([self.image, "sleep", "infinity"])
        return argv

    def exec_argv(self, command: str, *, shell_syntax: bool, timeout_s: int) -> list[str]:
        base = [
            self.bin, "exec", "-w", WORKSPACE_MOUNT, self.name,
            "timeout", "--signal=TERM", "--kill-after=5s", f"{timeout_s}s",
        ]
        if shell_syntax:
            return base + ["/bin/sh", "-c", command]
        return base + ["--", *shlex.split(command)]

    def egress_exec_argv(self, script: str) -> list[str]:
        return [
            self.bin, "exec", "-w", WORKSPACE_MOUNT, self.name,
            "/bin/sh", "-c", script,
        ]

    def shell_argv(self) -> list[str]:
        return [self.bin, "exec", "-it", "-w", WORKSPACE_MOUNT, self.name, "/bin/sh"]

    def run(
        self,
        command: "str | list[str]",
        *,
        timeout_s: int,
        cwd: Path,
        readonly: bool = False,
        shell_syntax: bool = False,
    ) -> ExecResult:
        _ = (cwd, readonly)
        if isinstance(command, list):
            script = " ".join(shlex.quote(part) for part in command)
            shell_syntax = True
            command = script
        if not self.started:
            return ExecResult(
                stdout="ERROR: sandbox container is not running",
                stderr="", exit_code=127, backend="docker", spawn_error=True,
            )
        try:
            argv = self.exec_argv(command, shell_syntax=shell_syntax, timeout_s=timeout_s)
        except ValueError as exc:
            return ExecResult(
                stdout=f"ERROR: {exc}", stderr="", exit_code=127,
                backend="docker", spawn_error=True,
            )
        guard = timeout_s + CLIENT_GUARD_EXTRA_S
        result = self.runner(argv, timeout=guard)  # type: ignore[operator]
        timed = int(result.returncode) == 124
        return ExecResult(
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            exit_code=int(result.returncode),
            backend="docker",
            timed_out=timed,
            timeout_s=timeout_s,
        )

    def list_containers(self) -> list[ContainerRow]:
        if self._containers is not None:
            return list(self._containers)
        result = self.runner(  # type: ignore[operator]
            [self.bin, "ps", "-a", "--filter", "label=lmloop.workspace",
             "--format", PS_FORMAT],
            timeout=PREFLIGHT_TIMEOUT_S,
        )
        if result.returncode != 0:
            return []
        return parse_ps(result.stdout)

    def check_binary(self) -> "str | None":
        if shutil.which(self.bin) is None and "/" not in self.bin:
            return f"{DOCKER_BIN_ENV} binary {self.bin!r} is not on PATH"
        if "/" in self.bin and not Path(self.bin).exists():
            return f"{DOCKER_BIN_ENV} binary {self.bin!r} is not on PATH"
        return None

    def check_daemon(self) -> "str | None":
        result = self.runner(  # type: ignore[operator]
            [self.bin, "version", "--format", "{{.Server.Version}}"],
            timeout=PREFLIGHT_TIMEOUT_S,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return "docker daemon is not reachable (version check failed)"
        return None

    def check_image(self) -> "str | None":
        if not self.image:
            return "sandbox image is empty; pass --docker-image or run `lmloop sandbox build`"
        if not image_has_digest(self.image):
            return "image reference must contain @sha256: (tags are not accepted)"
        result = self.runner(  # type: ignore[operator]
            [self.bin, "image", "inspect", self.image, "--format", "{{.Id}}"],
            timeout=PREFLIGHT_TIMEOUT_S,
        )
        if result.returncode != 0:
            return f"image is not present locally: {self.image}"
        return None

    def check_workspace(self) -> "str | None":
        return workspace_rejection(self.root)

    def check_write_probe(self) -> "str | None":
        if self._write_probe is not None:
            return self._write_probe()  # type: ignore[operator]
        probe = self.root / ".lmloop-probe"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            return f"workspace is not writable: {exc}"
        if probe.exists():
            return "write probe left a file behind (root-owned artifact?)"
        return None

    def check_single_container(self) -> "str | None":
        alive = self._pid_alive
        for row in self.list_containers():
            if row.workspace != str(self.root):
                continue
            if row.name == self.name:
                continue
            if not row.running:
                continue
            if row.mode == "ephemeral" and not alive(row.pid):  # type: ignore[operator]
                continue
            return (
                f"another sandbox is already running for this workspace ({row.name})"
            )
        return None

    def check_persist_match(self) -> "str | None":
        if not self.persist:
            return None
        for row in self.list_containers():
            if row.workspace != str(self.root) and row.name != self.persist_name:
                continue
            if row.name != self.persist_name and row.workspace != str(self.root):
                continue
            if not row.running and row.name != self.persist_name:
                continue
            if row.image_digest and row.image_digest != self.image:
                return f"image digest changed — {RESET_HINT}"
            if row.config_hash and row.config_hash != self.config_hash:
                return RESET_HINT
            if row.policy_bundle and row.policy_bundle != self.policy_hash:
                return f"policy bundle hash mismatch — {RESET_HINT}"
        return None

    def check_ports(self) -> "str | None":
        if self.network != "bridge":
            return None
        free = self._port_free
        for port in ports_in_ranges():
            if not free(port):  # type: ignore[operator]
                return f"port 127.0.0.1:{port} is busy ({SANDBOX_PORTS[0]} / {SANDBOX_PORTS[1]})"
        return None

    def preflight(self) -> PreflightReport:
        errors: list[str] = []
        warnings: list[str] = []
        for check in (
            self.check_binary,
            self.check_daemon,
            self.check_image,
            self.check_workspace,
            self.check_write_probe,
            self.check_single_container,
            self.check_persist_match,
            self.check_ports,
        ):
            message = check()
            if message:
                errors.append(message)
        errors.extend(self.egress_errors)
        if self.policy_error:
            errors.append(self.policy_error)
        if self.network == "host":
            warnings.append(
                "sandbox_network=host shares the host network namespace (Linux/WSL2 opt-in)"
            )
        if self.network == "none" and self.egress_hosts:
            warnings.append("egress allowlist is ignored when sandbox_network is none")
        if self.persist:
            age, layer = self.persist_stats()
            warnings.extend(persist_warnings(age, layer))
        if self.network == "bridge":
            warnings.append(NETWORK_HONEST)
        report = PreflightReport(tuple(errors), tuple(warnings))
        self._record_preflight(report)
        return report

    def persist_stats(self) -> "tuple[float | None, float | None]":
        if self._persist_stats is not None:
            return self._persist_stats()  # type: ignore[operator]
        return (None, None)

    def _record_preflight(self, report: PreflightReport) -> None:
        from . import usage
        usage.record(
            "sandbox.preflight",
            network=self.network,
            image_digest=self.image,
            policy_bundle_hash=self.policy_hash,
            untrusted=self.untrusted,
            ok=report.ok,
        )

    def sweep_orphans(self) -> list[str]:
        removed: list[str] = []
        alive = self._pid_alive
        for row in self.list_containers():
            if row.mode != "ephemeral":
                continue
            if alive(row.pid):  # type: ignore[operator]
                continue
            self.runner([self.bin, "rm", "-f", row.name], timeout=30)  # type: ignore[operator]
            removed.append(row.name)
        return removed

    def start(self) -> PreflightReport:
        report = self.preflight()
        if not report.ok:
            return report
        self.sweep_orphans()
        if self.persist and self._persist_attachable():
            self.started = True
            self._ensure_home()
            self._maybe_egress(report)
            return report
        created = self.runner(self.create_argv(), timeout=120)  # type: ignore[operator]
        if created.returncode != 0:
            detail = (created.stderr or created.stdout or "docker run failed").strip()
            return PreflightReport(report.errors + (detail,), report.warnings)
        self.started = True
        self._ensure_home()
        self._maybe_egress(report)
        if not self.persist:
            atexit.register(self.close)
        return report

    def _persist_attachable(self) -> bool:
        for row in self.list_containers():
            if row.name == self.persist_name and row.running:
                return (
                    row.image_digest == self.image
                    and row.config_hash == self.config_hash
                    and (not row.policy_bundle or row.policy_bundle == self.policy_hash)
                )
        return False

    def _ensure_home(self) -> None:
        self.runner(  # type: ignore[operator]
            [self.bin, "exec", self.name, "mkdir", "-p", HOME_IN_CONTAINER],
            timeout=30,
        )

    def _maybe_egress(self, report: PreflightReport) -> None:
        if self.network != "bridge" or not self.egress_hosts:
            return
        script = egress_script(self.egress_hosts)
        applied = self.runner(self.egress_exec_argv(script), timeout=30)  # type: ignore[operator]
        from . import usage
        decision = "allowlist" if applied.returncode == 0 else "unenforceable"
        usage.record("sandbox.policy", rule="egress", decision=decision)
        if applied.returncode != 0:
            # Warnings were already frozen; status text still tells the truth.
            _ = report

    def close(self) -> None:
        if self.persist or self._closed:
            return
        self._closed = True
        self.started = False
        self.runner([self.bin, "rm", "-f", self.name], timeout=30)  # type: ignore[operator]

    def reset(self, *, deps: bool = False) -> list[list[str]]:
        commands = [[self.bin, "rm", "-f", self.persist_name]]
        if deps:
            for vol in self.volume_names():
                commands.append([self.bin, "volume", "rm", "-f", vol])
        for argv in commands:
            self.runner(argv, timeout=30)  # type: ignore[operator]
        self.started = False
        return commands

    def describe(self) -> str:
        if not self.started:
            return f"docker · requested · {self.mode_name} · {self.network}"
        ports = ""
        if self.network == "bridge":
            ports = " · bridge 127.0.0.1:" + ",".join(SANDBOX_PORTS)
        elif self.network == "none":
            ports = " · none"
        else:
            ports = " · host"
        return f"docker · {self.mode_name} · {self.name}{ports}"

    def status_text(self) -> str:
        lines = [
            f"exec: {self.describe()}",
            NETWORK_HONEST,
            "network: bridge = inbound loopback publish only; host services on "
            "0.0.0.0 stay reachable; egress is open unless an allowlist is applied "
            "or sandbox_network is none.",
            "network: none = no ingress, no host services, no egress.",
            "network: host = opt-in, same network namespace as the host (Linux/WSL2).",
            f"image: {self.image or '(unset)'}",
            f"config_hash: {self.config_hash}",
        ]
        if self.policy_hash:
            lines.append(f"policy_bundle: {self.policy_hash}")
        if self.egress_hosts:
            lines.append("egress allowlist: " + ", ".join(self.egress_hosts))
        lines.append(
            "shadow volumes: " + ", ".join(
                f"{vol}:{WORKSPACE_MOUNT}/{name}"
                for name, vol in zip(SANDBOX_SHADOW_DIRS, self.volume_names())
            )
        )
        return "\n".join(lines)


def policy_bundle_path(root: Path) -> "Path | None":
    env = os.environ.get("LMLOOP_SANDBOX_POLICY", "").strip()
    if env:
        return Path(env)
    candidate = root / ".lmloop" / "sandbox-policy.json"
    if candidate.is_file():
        return candidate
    return None


def egress_allowlist_path(root: Path) -> "Path | None":
    env = os.environ.get("LMLOOP_SANDBOX_EGRESS", "").strip()
    if env:
        return Path(env)
    candidate = root / ".lmloop" / "sandbox-egress.txt"
    if candidate.is_file():
        return candidate
    return None


def prepare_scoped_secret(root: Path, name: str, text: str) -> Path:
    """Write one secret file for a read-only mount. No environment passthrough."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        raise ValueError("secret name must be a single token")
    dest = Path(root) / ".lmloop" / "sandbox-secrets" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return dest


def build_backend(
    cfg: dict,
    workspace_root: "Path | None" = None,
    *,
    docker: bool = False,
    persist: bool = False,
    image: str = "",
    runner=None,
) -> "LocalBackend | DockerBackend":
    """Factory. ``docker=False`` returns ``LocalBackend`` without a PATH lookup."""
    if not docker and not persist:
        return LocalBackend()
    root = Path(workspace_root or Path.cwd()).resolve()
    kwargs = {
        "cfg": cfg,
        "root": root,
        "persist": persist or False,
        "image": image,
    }
    if runner is not None:
        kwargs["runner"] = runner
    return DockerBackend(**kwargs)


def local_status_text() -> str:
    return "\n".join([
        "exec: local (host)",
        "Docker is not active. Pass --docker to run shell commands in a container.",
        NETWORK_HONEST + " when you do opt in.",
        "Published ports (bridge only): "
        + ", ".join(f"127.0.0.1:{spec}" for spec in SANDBOX_PORTS),
    ])


def sandbox_build_digest(stdout: str) -> "str | None":
    """Pick an ``image@sha256:`` reference out of inspect output. Never a tag."""
    for token in (stdout or "").replace(",", " ").replace('"', " ").split():
        if "@sha256:" in token:
            return token.strip("[]")
        if token.startswith("sha256:") and len(token) > 12:
            return "lmloop-sandbox@" + token
    return None
