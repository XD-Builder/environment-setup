"""Wave 2: Docker sandbox seam, canvas TUI helpers, abstention metrics.

Docker is never contacted. Argv is asserted against a fake runner.
"""

import inspect
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from lmloop.canvas_tui import (
    CanvasSession,
    Viewport,
    bucket_collisions,
    cull_viewport,
    open_memory_canvas,
    try_fullscreen,
)
from lmloop.checks import apply_baseline
from lmloop.config import DEFAULTS
from lmloop.exec import (
    CLIENT_GUARD_EXTRA_S,
    CmdResult,
    ContainerRow,
    DockerBackend,
    LocalBackend,
    PreflightReport,
    RESET_HINT,
    active_backend,
    backend_label,
    build_backend,
    describe_exec,
    egress_script,
    image_has_digest,
    install_backend,
    parse_allowlist,
    persist_warnings,
    prepare_scoped_secret,
    reset_active_backend,
    sandbox_build_digest,
    workspace_rejection,
)
from lmloop.loop import UntilRun, _until_outcome_label, abstain_reason, run_check, run_until
from lmloop.tools import format_exec_result, run_shell
from lmloop import evals


IMAGE = "lmloop-sandbox@sha256:" + ("ab" * 32)


class Rec:
    def __init__(self, result=None):
        self.calls = []
        self.timeouts = []
        self.result = result or CmdResult(0, "", "")

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return self.result


def _node(ident, x, y, typ="learning", label=""):
    return type("N", (), {
        "id": ident, "x": x, "y": y, "type": typ, "label": label,
        "detail": label, "confidence": 3,
    })()


def _backend(root: Path, **kwargs) -> DockerBackend:
    cfg = dict(DEFAULTS)
    params = {
        "cfg": cfg,
        "root": root,
        "image": IMAGE,
        "runner": Rec(),
        "identity": [],
        "persist": False,
    }
    params.update(kwargs)
    backend = DockerBackend(**params)
    backend._containers = []
    backend._port_free = lambda _port: True
    backend._write_probe = lambda: None
    return backend


class LocalBackendTests(unittest.TestCase):
    def test_echo_nonzero_and_timeout(self):
        backend = LocalBackend()
        root = Path(".").resolve()
        ok = backend.run("echo wave2-ok", timeout_s=5, cwd=root)
        self.assertEqual(ok.exit_code, 0)
        self.assertIn("wave2-ok", ok.stdout)
        self.assertEqual(ok.backend, "local")
        bad = backend.run("false", timeout_s=5, cwd=root)
        self.assertNotEqual(bad.exit_code, 0)
        timed = backend.run("sleep 30", timeout_s=1, cwd=root)
        self.assertTrue(timed.timed_out)
        self.assertTrue(format_exec_result(timed).startswith("ERROR: command timed out after 1s"))

    def test_readonly_rejected(self):
        with self.assertRaises(ValueError):
            LocalBackend().run("true", timeout_s=5, cwd=Path("."), readonly=True)

    def test_no_flag_skips_path_lookup(self):
        with patch("lmloop.exec.shutil.which") as which:
            backend = build_backend({}, docker=False)
        which.assert_not_called()
        self.assertIsInstance(backend, LocalBackend)
        self.assertNotIn("docker", DEFAULTS)
        self.assertNotIn("use_docker", DEFAULTS)

    def test_run_shell_does_not_call_subprocess(self):
        source = inspect.getsource(run_shell)
        self.assertNotIn("subprocess", source)


class DockerArgvTests(unittest.TestCase):
    def test_create_and_exec_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            backend = _backend(root)
            argv = backend.create_argv()
            self.assertIn("--init", argv)
            self.assertIn("--cap-drop", argv)
            self.assertIn("ALL", argv)
            self.assertIn("--pids-limit", argv)
            self.assertIn("--memory", argv)
            self.assertIn("--cpus", argv)
            self.assertIn("no-new-privileges", argv)
            self.assertNotIn("--add-host", argv)
            self.assertNotIn("-l", argv)
            ports = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-p"]
            self.assertTrue(ports)
            self.assertTrue(all(item.startswith("127.0.0.1:") for item in ports))
            vols = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]
            self.assertTrue(any(
                item.startswith(f"lmloop-dep-{backend.token}-venv:")
                and item.endswith(":/workspace/.venv")
                for item in vols
            ))
            self.assertFalse(any(item.startswith(str(root / ".venv")) for item in vols))
            simple = backend.exec_argv("echo hi", shell_syntax=False, timeout_s=4)
            self.assertNotIn("-l", simple)
            self.assertIn("--", simple)
            self.assertEqual(simple[-2:], ["echo", "hi"])
            script = backend.exec_argv("echo a | cat", shell_syntax=True, timeout_s=4)
            self.assertEqual(script[-1], "echo a | cat")
            self.assertEqual(script[-3:-1], ["/bin/sh", "-c"])
            self.assertNotIn("-l", backend.shell_argv())

    def test_names_pid_and_hash_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            other = Path(d) / "other"
            other.mkdir()
            one = _backend(root, persist=True)
            two = _backend(root, persist=True)
            three = _backend(other, persist=True)
            self.assertEqual(one.persist_name, two.persist_name)
            self.assertNotEqual(one.persist_name, three.persist_name)
            self.assertIn(str(os.getpid()), one.ephemeral_name)
            drifted = _backend(root, persist=True, memory="1g")
            self.assertNotEqual(one.config_hash, drifted.config_hash)
            drifted._containers = [ContainerRow(
                name=drifted.persist_name, mode="persist", pid=0,
                workspace=str(drifted.root), image_digest=IMAGE,
                config_hash=one.config_hash, policy_bundle="", status="Up",
            )]
            message = drifted.check_persist_match()
            self.assertIn("sandbox reset", message or "")
            self.assertIn(RESET_HINT, message or "")

    def test_timeout_maps_124_and_client_guard(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Rec(CmdResult(124, "", ""))
            backend = _backend(Path(d), runner=rec)
            backend.started = True
            result = backend.run("sleep 9", timeout_s=3, cwd=Path(d))
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            self.assertEqual(rec.timeouts[0], 3 + CLIENT_GUARD_EXTRA_S)

    def test_orphan_sweep_and_shadow_reset(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rec = Rec()
            backend = _backend(root, runner=rec, persist=True)
            backend._containers = [ContainerRow(
                name="leaked", mode="ephemeral", pid=2 ** 22,
                workspace=str(root), image_digest=IMAGE, config_hash="x",
                policy_bundle="", status="Exited",
            )]
            backend._pid_alive = lambda _pid: False
            removed = backend.sweep_orphans()
            self.assertEqual(removed, ["leaked"])
            self.assertTrue(any(call[1:4] == ["rm", "-f", "leaked"] for call in rec.calls))
            other = _backend(root, persist=True)
            self.assertEqual(backend.volume_names(), other.volume_names())
            with_deps = backend.reset(deps=True)
            self.assertTrue(any("volume" in cmd for cmd in with_deps))
            plain = other.reset(deps=False)
            self.assertFalse(any("volume" in cmd for cmd in plain))

    def test_same_backend_for_checks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rec = Rec(CmdResult(0, "ok\n", ""))
            backend = _backend(root, runner=rec)
            backend.started = True
            install_backend(backend)
            self.addCleanup(reset_active_backend)
            text = run_check(dict(DEFAULTS), "true", None, root)
            self.assertIn("[exit code: 0]", text)
            self.assertTrue(any("exec" in call for call in rec.calls))
            self.assertEqual(backend_label(), "docker:ephemeral")
            self.assertIn("docker", describe_exec())

    def test_secrets_are_read_only_mounts_not_env(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prepare_scoped_secret(root, "token", "sekrit-value")
            with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "nope"}):
                argv = _backend(root).create_argv()
            blob = " ".join(argv)
            self.assertIn(":ro", blob)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", blob)
            self.assertNotIn("sekrit-value", blob)
            self.assertNotIn("docker.sock", blob)

    def test_policy_bundle_mismatch_and_egress_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            policy = root / ".lmloop" / "sandbox-policy.json"
            policy.parent.mkdir(parents=True)
            policy.write_text('{"network": "bridge"}\n')
            allow = root / ".lmloop" / "sandbox-egress.txt"
            allow.write_text("# comment\nexample.com\nbad host\n")
            hosts, errors = parse_allowlist(allow.read_text())
            self.assertEqual(hosts, ["example.com"])
            self.assertTrue(errors)
            backend = _backend(root, persist=True)
            self.assertTrue(backend.policy_hash)
            self.assertEqual(backend.egress_hosts, ["example.com"])
            script = egress_script(backend.egress_hosts)
            self.assertIn("iptables -P OUTPUT DROP", script)
            self.assertNotIn("-l", backend.egress_exec_argv(script))
            backend.egress_errors = []
            backend._containers = [ContainerRow(
                name=backend.persist_name, mode="persist", pid=0,
                workspace=str(root), image_digest=IMAGE,
                config_hash=backend.config_hash, policy_bundle="ffff",
                status="Up",
            )]
            message = backend.check_persist_match()
            self.assertIn("policy bundle", message or "")

    def test_preflight_checks_fail_independently(self):
        with tempfile.TemporaryDirectory() as d:
            backend = _backend(Path(d))
            backend.bin = "lmloop-docker-not-installed"
            self.assertIn("PATH", backend.check_binary() or "")
            backend.runner = lambda argv, timeout=None: CmdResult(1, "", "down")
            self.assertIn("daemon", backend.check_daemon() or "")
            backend.image = "python:latest"
            self.assertIn("@sha256:", backend.check_image() or "")
            backend.image = IMAGE
            self.assertIn("not present", backend.check_image() or "")
            self.assertIsNotNone(workspace_rejection(Path("/")))
            self.assertIn("HOME", workspace_rejection(Path.home()) or "")
            backend._write_probe = lambda: "read-only"
            self.assertEqual(backend.check_write_probe(), "read-only")
            backend._containers = [ContainerRow(
                name="other", mode="ephemeral", pid=os.getpid(),
                workspace=str(backend.root), image_digest=IMAGE,
                config_hash="x", policy_bundle="", status="Up 2 seconds",
            )]
            self.assertIn("another sandbox", backend.check_single_container() or "")
            backend._port_free = lambda port: port != 3000
            self.assertIn("3000", backend.check_ports() or "")
            notes = persist_warnings(8, 6)
            self.assertEqual(len(notes), 2)
            self.assertTrue(image_has_digest(IMAGE))
            self.assertIsNone(sandbox_build_digest("lmloop-sandbox:local"))
            self.assertTrue(image_has_digest(
                sandbox_build_digest('["ghcr.io/example/lmloop@sha256:abc"]') or ""
            ))


class SandboxCliTests(unittest.TestCase):
    def test_bad_image_rejected_and_docker_aborts_before_handler(self):
        from lmloop.cli import main

        with patch("lmloop.cli.cmd_eval_cli") as handler:
            code = main(["--docker-image", "python:latest", "eval"])
        self.assertEqual(code, 2)
        handler.assert_not_called()

        with tempfile.TemporaryDirectory() as d:
            previous = Path.cwd()
            os.chdir(d)
            try:
                with patch("lmloop.cli.cmd_eval_cli") as handler:
                    code = main(["--docker", "eval"])
                handler.assert_not_called()
                self.assertEqual(code, 1)
            finally:
                os.chdir(previous)

    def test_persist_implies_docker_and_status_is_local(self):
        from lmloop.cli import main

        with tempfile.TemporaryDirectory() as d:
            previous = Path.cwd()
            os.chdir(d)
            try:
                with patch.object(DockerBackend, "start", return_value=PreflightReport(("no daemon",))):
                    with patch("lmloop.cli.cmd_eval_cli") as handler:
                        code = main(["--docker-persist", "eval"])
                handler.assert_not_called()
                self.assertEqual(code, 1)
                out = StringIO()
                with patch("sys.stdout", out):
                    status = main(["sandbox", "status"])
                self.assertEqual(status, 0)
                self.assertIn("local (host)", out.getvalue())
                self.assertIn("not host isolation", out.getvalue())
            finally:
                os.chdir(previous)
                reset_active_backend()

    def test_bridge_appends_port_steering(self):
        from lmloop import skills

        with tempfile.TemporaryDirectory() as d:
            backend = _backend(Path(d))
            install_backend(backend)
            self.addCleanup(reset_active_backend)
            with patch("lmloop.steer.clock_block", return_value="## Clock\n\nNOW"), \
                 patch("lmloop.steer.steering_block", return_value=""), \
                 patch("lmloop.memory.context_block", return_value=""):
                text = skills.system_prompt({})
        self.assertIn("3000-3010", text)
        self.assertIn("127.0.0.1", text)
        reset_active_backend()
        with patch("lmloop.steer.clock_block", return_value="## Clock\n\nNOW"), \
             patch("lmloop.steer.steering_block", return_value=""), \
             patch("lmloop.memory.context_block", return_value=""):
            host = skills.system_prompt({})
        self.assertNotIn("3000-3010", host)

    def test_help_lists_sandbox_once(self):
        from lmloop.commands import COMMANDS
        self.assertEqual([c.name for c in COMMANDS].count("sandbox"), 1)


class CanvasTuiTests(unittest.TestCase):
    def test_collisions_and_culling(self):
        view = Viewport(origin_x=0, origin_y=0, width=10, height=6, zoom=1)
        on = [_node("a", 1.2, 1.2), _node("b", 1.4, 1.1)]
        off = [_node("c", 40, 1)]
        buckets = bucket_collisions(on + off, view)
        self.assertEqual(len(buckets), 1)
        members = next(iter(buckets.values()))
        self.assertEqual(len(members), 2)
        self.assertEqual({node.id for node in members}, {"a", "b"})
        visible = cull_viewport(on + off, view)
        self.assertEqual({node.id for node in visible}, {"a", "b"})

    def test_bindings_dispatch(self):
        session = CanvasSession(
            nodes=(_node("a", 0, 0, label="alpha"), _node("b", 3, 0, label="beta")),
            edges=(("a", "b", "contradicts"),),
            viewport=Viewport(width=20, height=8, zoom=1),
        )
        session.handle("tab")
        self.assertEqual(session.selected, 1)
        origin = session.viewport.origin_x
        session.handle("l")
        self.assertGreater(session.viewport.origin_x, origin)
        session.handle("g")
        self.assertTrue(session.show_clusters)
        session.handle("/")
        session.handle("a")
        session.handle("enter")
        self.assertEqual(session.query, "a")
        self.assertFalse(session.search_mode)
        session.handle("r")
        self.assertTrue(session.reload_requested)
        session.handle("q")
        self.assertTrue(session.quit)
        rendered = session.render()
        self.assertIn("clusters", rendered)
        self.assertIn("a ≠ b", rendered)

    def test_module_does_not_import_agent_loop_or_graph(self):
        path = Path(__file__).resolve().parents[1] / "lmloop" / "canvas_tui.py"
        text = path.read_text(encoding="utf-8")
        for banned in ("from .agent", "from .loop", "from .graph", "import agent", "import loop"):
            self.assertNotIn(banned, text)
        with patch("lmloop.canvas_tui.sys.stdin.isatty", return_value=False):
            self.assertFalse(try_fullscreen(lambda _q: None))

    def test_graph_off_is_text(self):
        text = open_memory_canvas({"use_graph": False}, query="")
        self.assertIn("use_graph", text)


class AbstentionTests(unittest.TestCase):
    def test_fixture_metrics_and_selection_rule(self):
        train = evals.load_abstention_pairs(split="train")
        validation = evals.load_abstention_pairs(split="validation")
        self.assertTrue(train)
        self.assertTrue(validation)
        self.assertTrue({pair.name for pair in train}.isdisjoint({pair.name for pair in validation}))
        known = {item[0] for item in evals.MUTATION_CATALOG}
        for pair in train + validation:
            self.assertIn(pair.mutation, known)
            self.assertTrue(pair.goal)
        metrics = evals.score_abstention(train + validation)
        self.assertEqual(metrics["act"], 1.0)
        self.assertEqual(metrics["abstain"], 1.0)
        self.assertEqual(metrics["pair"], 1.0)
        self.assertFalse(evals.harness_selection_ok(
            {"act": 0.8, "abstain": 0.8},
            {"act": 0.9, "abstain": 0.7},
        ))
        self.assertTrue(evals.harness_selection_ok(
            {"act": 0.8, "abstain": 0.8},
            {"act": 0.8, "abstain": 0.9},
        ))
        self.assertFalse(evals.harness_selection_ok(
            {"act": 0.8, "abstain": 0.8},
            {"act": 0.8, "abstain": 0.8},
        ))
        self.assertTrue(evals.admission_ready(0.4))
        self.assertFalse(evals.admission_ready(0.2))
        destructive = evals.AbstentionPair(
            "x", "goal", "M-gate-deny-rm", "done", "abstain", True, "train",
        )
        self.assertEqual(evals.score_abstention([destructive])["abstain"], 0.0)

    def test_eval_cli_abstention_json(self):
        from lmloop.cli import main

        out = StringIO()
        with patch("sys.stdout", out):
            code = main(["eval", "--abstention", "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"act"', out.getvalue())
        self.assertIn("missing-runner", out.getvalue())

    def test_broken_keep_abstains_before_maker(self):
        calls = []

        def fake_act(*_a, **_k):
            calls.append("act")
            raise AssertionError("maker should not run")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch("lmloop.loop.project_dir", return_value=root), \
                 patch("lmloop.memory.project_dir", return_value=root), \
                 patch("lmloop.loop.agent.act", side_effect=fake_act), \
                 patch("lmloop.loop.skills.system_prompt", return_value="sys"), \
                 patch("lmloop.loop._model_propose_checks", return_value=()):
                run = UntilRun.create("keep lint", keeps=("false",))
                run = run_until(
                    {
                        "until_max_steps": 4,
                        "until_mine": False,
                        "confirm_shell": False,
                        "shell_timeout_s": 5,
                        "context_reserve": 2048,
                        "until_baseline": "auto",
                        "check_inference": "auto",
                    },
                    "m",
                    run=run,
                    echo=lambda *_a, **_k: None,
                    echo_status=lambda *_a, **_k: None,
                    workspace_root=root,
                    interactive=False,
                )
        self.assertEqual(calls, [])
        roles = [event.get("role") for event in run.events]
        self.assertIn("abstain", roles)
        self.assertNotIn("maker", roles)
        self.assertTrue(run.is_done())
        self.assertEqual(_until_outcome_label(run.events), "abstain")

    def test_abstain_reason_ignores_typed_missing_binary(self):
        from lmloop.checks import PlannedCheck

        blocked = apply_baseline(
            (PlannedCheck("pytest", "check", "flag", "authoritative", "--check", True),),
            [{"cmd": "pytest", "status": "blocked"}],
        )
        self.assertIsNone(abstain_reason(blocked))
        broken = apply_baseline(
            (PlannedCheck("make lint", "keep", "flag", "authoritative", "--keep", True),),
            [{"cmd": "make lint", "status": "fail"}],
        )
        self.assertEqual(abstain_reason(broken), "keep-prebroken")

    def test_gap_rules(self):
        events = [ {
            "ts": "2026-01-01T00:00:00Z",
            "feature": "check.blocked",
            "detail": {"cmd": "pytest", "exit_class": "spawn"},
        } for _ in range(3)]
        events.append({
            "ts": "2026-01-01T00:00:00Z",
            "feature": "until.finish",
            "detail": {"outcome": "paused"},
        })
        events.append({
            "ts": "2026-01-01T00:00:00Z",
            "feature": "sandbox.preflight",
            "detail": {"network": "bridge", "untrusted": True, "ok": True},
        })
        events.append({
            "ts": "2026-01-01T00:00:00Z",
            "feature": "tool",
            "detail": {"name": "fetch_url"},
        })
        gaps = {gap.id for gap in evals.find_gaps(evals.aggregate(events), min_samples=1)}
        self.assertIn("blocked-as-fail", gaps)
        self.assertIn("docker-bridge-exfil-risk", gaps)


if __name__ == "__main__":
    unittest.main()
