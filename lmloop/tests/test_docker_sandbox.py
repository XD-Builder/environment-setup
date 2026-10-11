"""Reference sandbox image contract (docker local).

Builds ``lmloop/sandbox/Dockerfile`` and checks Python 3.14, git, ripgrep,
and curl, plus ``rg`` on a bind-mounted workspace. Opt in with
``LMLOOP_DOCKER_E2E=1``. ``--docker`` / ``DockerBackend`` is covered by
``tests/test_wave2.py``. ``LMLOOP_SANDBOX_IMAGE`` skips rebuild when CI
already tagged the image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from e2e_harness import REPO_ROOT, baseline

IMAGE = os.environ.get("LMLOOP_SANDBOX_IMAGE", "lmloop-sandbox:ci")
DOCKER_E2E = os.environ.get("LMLOOP_DOCKER_E2E") == "1"


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@unittest.skipUnless(DOCKER_E2E, "set LMLOOP_DOCKER_E2E=1 to run the sandbox image contract")
class DockerSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not on PATH")
        info = _docker("info")
        if info.returncode != 0:
            raise unittest.SkipTest("docker daemon is not reachable")
        inspect = _docker("image", "inspect", IMAGE)
        if inspect.returncode == 0 and os.environ.get("LMLOOP_SANDBOX_REBUILD") != "1":
            return
        dockerfile = REPO_ROOT / "lmloop" / "sandbox" / "Dockerfile"
        build = _docker(
            "build", "-f", str(dockerfile), "-t", IMAGE, str(dockerfile.parent),
            timeout=600,
        )
        if build.returncode != 0:
            raise AssertionError(build.stderr or build.stdout)

    @baseline("docker.sandbox", "image provides python 3.14, git, rg, curl", kind="holistic")
    def test_image_tools(self):
        checks = {
            "python": ["python", "-c", "import sys; assert sys.version_info[:2] >= (3, 14)"],
            "git": ["git", "--version"],
            "rg": ["rg", "--version"],
            "curl": ["curl", "--version"],
        }
        for name, argv in checks.items():
            with self.subTest(tool=name):
                proc = _docker("run", "--rm", IMAGE, *argv)
                self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    @baseline("docker.sandbox", "rg reads a mounted workspace", kind="holistic")
    def test_rg_in_mounted_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "spec.txt").write_text("TOKEN_DOCKER local sandbox\n")
            proc = _docker(
                "run", "--rm",
                "-v", f"{root}:/work",
                "-w", "/work",
                IMAGE,
                "rg", "-n", "TOKEN_DOCKER", "spec.txt",
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TOKEN_DOCKER", proc.stdout)


if __name__ == "__main__":
    unittest.main()
