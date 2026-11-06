"""Docker sandbox subcommand and process startup.

The shell backend stays in ``capabilities/exec.py``. This module is the CLI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..core.config import Config, save_config
from ..ui import Console


def sandbox_ready() -> bool:
    from ..capabilities.exec import DockerBackend, active_backend
    return isinstance(active_backend(), DockerBackend)


def sandbox_build(cfg: Config, console: Console) -> int:
    from ..capabilities import exec as exec_mod

    dockerfile = Path(__file__).resolve().parent.parent / "sandbox" / "Dockerfile"
    if not dockerfile.is_file():
        console.error(f"missing {dockerfile}")
        return 1
    tag = "lmloop-sandbox:local"
    bin_name = exec_mod.docker_binary()
    runner = exec_mod.subprocess_runner
    built = runner(
        [bin_name, "build", "-f", str(dockerfile), "-t", tag, str(dockerfile.parent)],
        timeout=600,
    )
    if built.returncode != 0:
        console.error((built.stderr or built.stdout or "docker build failed").strip())
        return 1
    inspected = runner(
        [bin_name, "image", "inspect", tag, "--format", "{{json .RepoDigests}}"],
        timeout=30,
    )
    digest = exec_mod.sandbox_build_digest(inspected.stdout)
    if not digest or not exec_mod.image_has_digest(digest):
        console.error("refusing to persist a tag; image inspect did not yield @sha256:")
        return 1
    cfg["sandbox_image"] = digest
    save_config(cfg)
    console.info(f"sandbox_image = {digest}")
    return 0


def cmd_sandbox(cfg: Config, words: list, console: Console) -> int:
    """``lmloop sandbox`` — status by default. Build persists a digest, never a tag."""
    from ..capabilities import exec as exec_mod

    verb = words[0] if words else "status"
    if verb not in ("", "status", "build", "shell", "reset", "rm"):
        console.error("usage: lmloop sandbox [status | build | shell | reset [--deps] | rm]")
        return 1
    if verb in ("", "status"):
        backend = exec_mod.active_backend()
        if isinstance(backend, exec_mod.DockerBackend):
            console.info(backend.status_text())
        else:
            console.info(exec_mod.local_status_text())
        return 0
    if verb == "build":
        return sandbox_build(cfg, console)
    root = Path.cwd().resolve()
    backend = exec_mod.active_backend()
    if not isinstance(backend, exec_mod.DockerBackend):
        backend = exec_mod.DockerBackend(cfg, root, persist=True)
    if verb == "reset":
        deps = "--deps" in words[1:]
        backend.reset(deps=deps)
        console.info("sandbox reset" + (" --deps" if deps else ""))
        return 0
    if verb == "rm":
        backend.runner(  # type: ignore[operator]
            [backend.bin, "rm", "-f", backend.persist_name], timeout=30,
        )
        console.info(f"removed {backend.persist_name}")
        return 0
    if verb == "shell":
        if not isinstance(exec_mod.active_backend(), exec_mod.DockerBackend):
            console.error("sandbox shell needs --docker or --docker-persist on this process")
            return 1
        argv = backend.shell_argv()
        if not sys.stdin.isatty():
            console.error("sandbox shell needs a TTY")
            console.info(" ".join(argv))
            return 1
        proc = subprocess.run(argv)
        return int(proc.returncode or 0)
    return 1


def activate_sandbox(cfg: Config, args, console: Console) -> int:
    """Start a sandbox when the flag asked for one. No flag: do not probe Docker.

    Returns 0 to continue, or an exit code that stops the process before any
    model call. A config key cannot turn this on.
    """
    from ..capabilities import exec as exec_mod

    image = (getattr(args, "docker_image", "") or "").strip()
    if image and not exec_mod.image_has_digest(image):
        console.error("image reference must contain @sha256:")
        return 2
    enabled = bool(getattr(args, "docker", False) or getattr(args, "docker_persist", False))
    if not enabled:
        return 0
    backend = exec_mod.build_backend(
        cfg,
        Path.cwd(),
        docker=True,
        persist=bool(args.docker_persist),
        image=image,
    )
    report = backend.start()
    if not report.ok:
        for line in report.errors:
            console.error(line)
        return 1
    for line in report.warnings:
        console.hint(line)
    exec_mod.install_backend(backend)
    return 0
