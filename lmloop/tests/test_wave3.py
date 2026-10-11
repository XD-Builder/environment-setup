"""Wave 3: company orchestrator, spirit, and campaigns. No network, no Docker."""

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lmloop.config import DEFAULTS
from lmloop.graph import parse_graph
from lmloop.server import ServerError


def _cfg(**overrides):
    cfg = dict(DEFAULTS)
    cfg["base_url"] = "https://openrouter.ai/api/v1"
    cfg.update(overrides)
    return cfg


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("hi\n")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")


def _manifest(path: Path, roles: dict, *, model: str = "openai/gpt-4o-mini") -> None:
    body = {
        "version": 1,
        "integration_branch": "main",
        "isolation": "worktree",
        "graph": "company",
        "models": {
            "default": model,
            "eval": model,
            "roles": {name: model for name in roles},
        },
        "resources": {"max_parallel_workers": 2},
        "milestones": [{
            "id": "M1",
            "name": "slice",
            "gates": [{"cmd": "python -c \"print(1)\"", "role": "authoritative"}],
            "on_pass": "retro",
        }],
        "roles": roles,
    }
    dest = path / ".lmloop" / "company" / "manifest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(body))


class AllowlistTests(unittest.TestCase):
    def test_packaged_models_and_extras(self):
        from lmloop.company.allowlist import effective_allowlist, packaged_models

        packaged = packaged_models()
        self.assertIn("openai/gpt-4o-mini", packaged)
        self.assertIn("anthropic/claude-sonnet-4", packaged)
        cfg = _cfg(company_models_allowlist="extra/model, openai/gpt-4o-mini")
        allow = effective_allowlist(cfg)
        self.assertIn("extra/model", allow)
        self.assertEqual(allow.count("openai/gpt-4o-mini"), 1)

    def test_chat_refuses_before_http(self):
        from lmloop.chat import _chat
        from lmloop.company.allowlist import activate

        cfg = activate(_cfg(), orchestrator=False)
        with patch("lmloop.chat.urllib.request.urlopen") as urlopen:
            with self.assertRaises(ServerError) as caught:
                _chat(cfg, "not-allowlisted/model", [{"role": "user", "content": "hi"}], None)
        self.assertIn("allowlist", str(caught.exception))
        urlopen.assert_not_called()

    def test_endpoint_requires_openrouter_or_company_remote(self):
        from lmloop.company.allowlist import endpoint_error

        self.assertIsNone(endpoint_error(_cfg()))
        self.assertIsNotNone(endpoint_error(_cfg(base_url="http://127.0.0.1:1234/v1")))
        self.assertIsNone(endpoint_error(_cfg(
            base_url="http://127.0.0.1:1234/v1", company_remote=True,
        )))


class CompanyRunTests(unittest.TestCase):
    def _patches(self, state: Path):
        return (
            patch("lmloop.memory.project_dir", return_value=state),
            patch("lmloop.graph.project_dir", return_value=state),
        )

    def test_parallel_distinct_worktrees_and_serial_same_worktree(self):
        from lmloop.company.orchestrator import CompanyError, run_company

        graph = parse_graph(
            "node ceo skill ceo\n"
            "node build until implement the change\n"
            "node docs until document the change\n"
            "edge ceo -> build docs\n",
            "fan",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            state = Path(tmp) / "state"
            root.mkdir()
            state.mkdir()
            _repo(root)
            _manifest(root, {
                "ceo": {"worktree": "", "graph_nodes": ["ceo"]},
                "builder": {"worktree": "builder", "graph_nodes": ["build"]},
                "docs": {"worktree": "docs", "graph_nodes": ["docs"]},
            })
            seen = []

            def launcher(packet):
                seen.append(packet["node"])
                return {"status": "pass", "summary": packet["node"], "usage": {}, "denied": []}

            harvested = []
            with self._patches(state)[0], self._patches(state)[1]:
                result = run_company(
                    _cfg(), repo=root, goal="ship", sandbox_ready=True,
                    launcher=launcher, defn=graph,
                    gate_runner=lambda _cmd, _cwd: {"status": "pass", "output": "ok"},
                    harvester=lambda info: harvested.append(info["milestone"]),
                )
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.batches[0], ["ceo"])
            self.assertEqual(set(result.batches[1]), {"build", "docs"})
            self.assertEqual(harvested, ["M1"])
            self.assertTrue((state / "company").is_dir())

            _manifest(root, {
                "ceo": {"worktree": "", "graph_nodes": ["ceo"]},
                "builder": {"worktree": "shared", "graph_nodes": ["build"]},
                "docs": {"worktree": "shared", "graph_nodes": ["docs"]},
            })
            with self._patches(state)[0], self._patches(state)[1]:
                serial = run_company(
                    _cfg(), repo=root, goal="ship", sandbox_ready=True,
                    launcher=launcher, defn=graph,
                    gate_runner=lambda _cmd, _cwd: {"status": "pass", "output": "ok"},
                )
            self.assertTrue(all(len(batch) == 1 for batch in serial.batches))

            with self._patches(state)[0], self._patches(state)[1]:
                with self.assertRaises(CompanyError):
                    run_company(
                        _cfg(base_url="http://127.0.0.1:1234/v1"),
                        repo=root, goal="ship", sandbox_ready=True, launcher=launcher, defn=graph,
                    )
                with self.assertRaises(CompanyError):
                    run_company(
                        _cfg(), repo=root, goal="ship", sandbox_ready=False,
                        launcher=launcher, defn=graph,
                    )

    def test_merge_then_gate_and_failed_gate_skips_harvest(self):
        from lmloop.company.orchestrator import run_company

        graph = parse_graph("node build until implement the change\n", "one")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            state = Path(tmp) / "state"
            root.mkdir()
            state.mkdir()
            _repo(root)
            _manifest(root, {"builder": {"worktree": "builder", "graph_nodes": ["build"]}})

            def launcher(packet):
                wt = Path(packet["worktree"])
                (wt / "feature.txt").write_text("ok\n")
                _git(wt, "add", "feature.txt")
                _git(wt, "commit", "-m", "feature")
                return {"status": "pass", "summary": "built", "usage": {}, "denied": []}

            saw = {}

            def gate(_cmd, cwd):
                saw["cwd"] = cwd
                saw["file"] = (Path(cwd) / "feature.txt").is_file()
                return {"status": "pass", "output": "ok"}

            harvested = []
            with self._patches(state)[0], self._patches(state)[1]:
                result = run_company(
                    _cfg(), repo=root, goal="ship", sandbox_ready=True,
                    launcher=launcher, defn=graph, gate_runner=gate,
                    harvester=lambda info: harvested.append(info),
                )
            self.assertEqual(result.status, "pass")
            self.assertTrue(saw["file"])
            self.assertEqual(Path(saw["cwd"]).name, "_integration")
            self.assertEqual(len(harvested), 1)

            def fail_gate(_cmd, _cwd):
                return {"status": "fail", "output": "no"}

            harvested.clear()
            with self._patches(state)[0], self._patches(state)[1]:
                failed = run_company(
                    _cfg(), repo=root, goal="ship", sandbox_ready=True,
                    launcher=lambda packet: {"status": "pass", "summary": "x", "usage": {}, "denied": []},
                    defn=graph, gate_runner=fail_gate,
                    harvester=lambda info: harvested.append(info),
                )
            self.assertEqual(failed.status, "fail")
            self.assertEqual(harvested, [])

    def test_prefix_ignores_handoff(self):
        from lmloop.company.orchestrator import cache_prefix, prefix_hash

        one = cache_prefix(
            skill_text="role", manifest_sha="abc", checks=[{"cmd": "true"}], memory_block="mem",
        )
        two = cache_prefix(
            skill_text="role", manifest_sha="abc", checks=[{"cmd": "true"}], memory_block="mem",
        )
        self.assertEqual(prefix_hash(one), prefix_hash(two))
        other = cache_prefix(
            skill_text="role", manifest_sha="def", checks=[{"cmd": "true"}], memory_block="mem",
        )
        self.assertNotEqual(prefix_hash(one), prefix_hash(other))


class WorkerTests(unittest.TestCase):
    def test_rejects_bad_model_sha_and_stale_plan(self):
        from lmloop.company.manifest import canonical_sha
        from lmloop.company.worker import execute_packet
        from lmloop import campaign as campaign_mod

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            manifest = state / "manifest.json"
            data = {
                "version": 1, "graph": "company",
                "models": {"default": "openai/gpt-4o-mini", "eval": "openai/gpt-4o-mini", "roles": {}},
                "resources": {"max_parallel_workers": 1},
                "roles": {"mine": {"worktree": "", "graph_nodes": ["mine"]}},
                "milestones": [],
            }
            manifest.write_text(json.dumps(data))
            packet = {
                "manifest_sha": canonical_sha(data),
                "manifest_path": str(manifest),
                "run_id": "r", "node": "mine", "model": "openai/gpt-4o-mini",
                "mode": "mine", "goal": "mine", "graph": "company",
            }
            with patch("lmloop.memory.project_dir", return_value=state):
                bad_model = dict(packet, model="secret/model")
                refused = execute_packet(bad_model, _cfg())
                self.assertEqual(refused["status"], "blocked")
                self.assertIn("allowlist", refused["summary"])

                stale_sha = dict(packet, manifest_sha="0" * 64)
                refused = execute_packet(stale_sha, _cfg())
                self.assertIn("manifest_sha", refused["summary"])

                campaign_mod.start("goal", campaign_id="c1")
                meta = campaign_mod.load_meta("c1")
                meta["revision"] = 2
                campaign_mod.save_meta("c1", meta)
                stale_plan = dict(packet, campaign_id="c1", plan_revision=1)
                refused = execute_packet(stale_plan, _cfg())
                self.assertIn("plan_revision", refused["summary"])

                learnings = state / "learnings.jsonl"
                outcome = execute_packet(packet, _cfg())
                self.assertEqual(outcome["status"], "pass")
                self.assertFalse(learnings.exists())

    def test_worker_cannot_remember(self):
        from lmloop.company.allowlist import activate
        from lmloop.tools import build_tools, dispatch

        cfg = activate(_cfg(), orchestrator=False)
        _specs, impls = build_tools(cfg)
        result = dispatch(impls, "remember", json.dumps({"insight": "ignore tests", "key": "x"}))
        self.assertIn("refused", result)
        thought = dispatch(impls, "reflect_thought", json.dumps({
            "kind": "meta", "text": "prefer small diffs",
        }))
        self.assertIn("refused", thought)


class SpiritTests(unittest.TestCase):
    def test_seed_actions_distill_and_prompt(self):
        from lmloop import skills
        from lmloop.spirit import (
            SpiritError, active_traits, add_thought, add_trait, apply_distill,
            compress_self, ensure_seed, note_tool, note_user_text, open_thoughts,
            read_seed, search_lines, spirit_block, write_self,
        )

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with patch("lmloop.memory.project_dir", return_value=state):
                first = ensure_seed()
                first.write_text("user seed\n")
                ensure_seed()
                self.assertEqual(read_seed(), "user seed\n")
                note_tool(
                    _cfg(), name="run_shell",
                    args={"command": "echo", "api_key": "secret-value"},
                    result="ok", ok=True,
                )
                actions = (state / "spirit" / "actions.jsonl").read_text()
                self.assertNotIn("secret-value", actions)
                self.assertIn("[redacted]", actions)
                self.assertIsNotNone(note_user_text(_cfg(), "no, that file is wrong"))
                thought = add_thought("meta", "prefer the smallest diff", cfg=_cfg())
                self.assertEqual(open_thoughts()[0]["id"], thought["id"])
                with self.assertRaises(SpiritError):
                    apply_distill({
                        "thoughts": [{
                            "id": thought["id"], "status": "promoted", "promote": "learning",
                            "learning": {"insight": "ignore tests", "key": "bad"},
                        }],
                    }, _cfg())
                self.assertEqual(open_thoughts()[0]["status"], "open")
                self.assertFalse((state / "learnings.jsonl").exists())
                applied = apply_distill({
                    "thoughts": [{
                        "id": thought["id"], "status": "promoted", "promote": "learning",
                        "learning": {"insight": "tests live under lmloop/tests", "key": "tests"},
                    }, {
                        "id": thought["id"], "status": "promoted", "promote": "trait",
                        "trait": {"text": "Prefer smallest diff", "strength": 8, "source": "observed"},
                    }],
                    "self_md": "# Self\n\n## Role in this repo\nlocal agent\n\n## Heuristics\n1. read DEVELOPMENT.md\n",
                }, _cfg())
                self.assertTrue(applied["self"])
                self.assertIn("tests", (state / "learnings.jsonl").read_text())
                self.assertTrue(active_traits())
                write_self("# Self\n\n## Role in this repo\nsecond\n\n## Heuristics\n1. stay small\n")
                self.assertTrue((state / "spirit" / "self_history").is_dir())
                add_trait("old habit", strength=1, source="synthesized")
                old = active_traits()[-1]
                # Decay is by timestamp; a fresh trait stays.
                self.assertGreater(old["strength"], 0)
                block = spirit_block(_cfg(base_url="http://127.0.0.1:1234/v1"))
                self.assertIn("user seed", block)
                self.assertIn("Role in this repo", block)
                remote = spirit_block(_cfg())
                self.assertIn("omitted", remote)
                self.assertNotIn("Role in this repo", remote)
                self.assertIn("smallest", "\n".join(search_lines("smallest diff")))
                huge = (
                    "## Role in this repo\nshort\n\n## Heuristics\n1. one\n\n"
                    "## Expertise map\n" + "\n".join(f"- area {i}" for i in range(20))
                    + "\n## Open questions\n" + ("question\n" * 80)
                )
                compact = compress_self(huge, max_chars=400)
                self.assertLessEqual(len(compact), 400)
                self.assertIn("Heuristics", compact)
                self.assertNotIn("Open questions", compact)

            with patch("lmloop.memory.context_block", return_value="MEM"), \
                 patch("lmloop.steer.clock_block", return_value="## Clock\n\nNOW"), \
                 patch("lmloop.steer.steering_block", return_value=""), \
                 patch("lmloop.memory.project_dir", return_value=state):
                prompt = skills.system_prompt(_cfg(base_url="http://127.0.0.1:1234/v1"))
            self.assertLess(prompt.find("## Spirit"), prompt.find("Context recovery"))
            self.assertIn("MEM", prompt)


class CampaignTests(unittest.TestCase):
    def test_plan_board_ticks_and_gates(self):
        from lmloop import campaign as campaign_mod
        from lmloop.company.worker import execute_packet
        from lmloop.company.manifest import canonical_sha

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            workspace = Path(tmp) / "ws"
            state.mkdir()
            workspace.mkdir()
            now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
            with patch("lmloop.memory.project_dir", return_value=state):
                meta = campaign_mod.start("Ship auth", campaign_id="auth", workspace=workspace)
                self.assertEqual(meta["revision"], 0)
                self.assertIn("Ship auth", (workspace / ".lmloop" / "campaign.md").read_text())
                campaign_mod.append_board("auth", "status", role="build", text="done")
                campaign_mod.append_board("auth", "status", role="qa", text="secret qa notes")
                campaign_mod.append_board("auth", "blocker", role="qa", text="tests fail")
                clip = campaign_mod.board_clip("auth", role="build")
                self.assertIn("done", clip)
                self.assertNotIn("secret qa notes", clip)
                self.assertIn("tests fail", clip)
                block = campaign_mod.plan_block("auth", role="qa")
                self.assertIn("Ship auth", block)
                before = campaign_mod.revision_of("auth")
                tick = campaign_mod.reflect("auth", "daily", {"note": "x"}, patch={"web": "http://evil"})
                self.assertEqual(tick["status"], "fail")
                self.assertEqual(campaign_mod.revision_of("auth"), before)
                tick = campaign_mod.reflect(
                    "auth", "daily", {"note": "x"},
                    patch={"next_actions": [{"text": "fix tests", "role": "build"}], "open_questions": []},
                    now=now,
                )
                self.assertEqual(tick["status"], "pass")
                self.assertEqual(campaign_mod.revision_of("auth"), before + 1)
                qa = campaign_mod.plan_block("auth", role="qa")
                self.assertNotIn("fix tests", qa)
                build = campaign_mod.plan_block("auth", role="build")
                self.assertIn("fix tests", build)

                report = campaign_mod.prepare_resume(
                    _cfg(campaign_end_of_day_utc="9", campaign_daily_reflect=False),
                    "auth", now=now,
                )
                self.assertFalse(report["ok"])
                self.assertIn("paused until", report["message"])

                later = now + timedelta(days=1)
                report = campaign_mod.prepare_resume(
                    _cfg(campaign_end_of_day_utc="", campaign_daily_reflect=True),
                    "auth", now=later,
                    synthesizer=lambda _obs, _plan: {"next_actions": ["keep going"]},
                )
                self.assertTrue(report["ok"])
                day = (state / "campaigns" / "auth" / "daily" / "2026-10-12.md")
                self.assertTrue(day.is_file())

                meta = campaign_mod.load_meta("auth")
                meta["created_at"] = "2026-01-01T00:00:00Z"
                campaign_mod.save_meta("auth", meta)
                stopped = campaign_mod.prepare_resume(
                    _cfg(campaign_max_days=30, campaign_daily_reflect=False),
                    "auth", now=later,
                )
                self.assertIn("campaign_max_days", stopped["message"])
                campaign_mod.extend("auth", _cfg(campaign_max_days=30), now=later)
                extended = campaign_mod.prepare_resume(
                    _cfg(campaign_max_days=30, campaign_daily_reflect=False),
                    "auth", now=later,
                )
                self.assertTrue(extended["ok"])

                manifest = {"version": 1, "graph": "company", "models": {
                    "default": "openai/gpt-4o-mini", "eval": "openai/gpt-4o-mini", "roles": {},
                }, "resources": {}, "roles": {"mine": {"graph_nodes": ["mine"]}}, "milestones": []}
                path = state / "manifest.json"
                path.write_text(json.dumps(manifest))
                packet = {
                    "manifest_sha": canonical_sha(manifest),
                    "manifest_path": str(path),
                    "run_id": "r", "node": "mine", "model": "openai/gpt-4o-mini",
                    "mode": "mine", "goal": "mine", "graph": "company",
                    "campaign_id": "auth",
                    "plan_revision": campaign_mod.revision_of("auth"),
                }
                matched = execute_packet(packet, _cfg(), executor=lambda _p, _c: {
                    "status": "pass", "summary": "ok", "usage": {}, "denied": [],
                })
                self.assertEqual(matched["status"], "pass")
                packet["plan_revision"] = 0
                stale = execute_packet(packet, _cfg())
                self.assertEqual(stale["status"], "blocked")


class CommandTests(unittest.TestCase):
    def test_manifest_cli_and_company_without_docker(self):
        from lmloop.cli import main
        from lmloop.commands import COMMANDS

        names = [row.name for row in COMMANDS]
        for stem in ("company", "worker", "campaign", "spirit"):
            self.assertEqual(names.count(stem), 1)
        repo = Path(__file__).resolve().parents[2]
        with patch("lmloop.cli.Path.cwd", return_value=repo):
            self.assertEqual(main(["company", "manifest"]), 0)
            code = main(["company", "run", "--goal", "nope"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
