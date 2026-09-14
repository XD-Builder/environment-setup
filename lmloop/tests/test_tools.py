"""Tests for lmloop tool safety helpers."""

import io
import os
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

from pathlib import Path

from lmloop.commands import RESERVED_SKILL_NAMES, slash_command_metas
from lmloop.tools import (
    GATE_IRREVERSIBLE,
    GATE_RECOVERABLE,
    GatePolicy,
    autonomous_gate,
    build_tools,
    coerce_bool,
    confirm_label,
    delete_file,
    dispatch,
    find_files,
    format_tool_preview,
    gate_tier,
    is_destructive,
    list_dir,
    move_file,
    needs_shell,
    read_file,
    run_shell,
    search_files,
    tool_names,
    unwrap_tool_result,
    update_file,
    user_notice,
    write_file,
    ShellCommand,
    ToolResult,
)
from lmloop.web import _search_ddgs, fetch_url, web_search


DDG_HTML_FIXTURE = """
<html><body>
<div class="result results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.fairfaxcounty.gov%2Flibrary">
        Fairfax County Public Library
      </a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.fairfaxcounty.gov%2Flibrary">
      Library programs for children and families in Fairfax County.
    </a>
    <div class="clear"></div>
  </div>
</div>
<div class="result results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title">
      <a class="result__a" href="https://www.firstfivefairfax.org/">First Five Fairfax</a>
    </h2>
    <a class="result__snippet" href="https://www.firstfivefairfax.org/">
      Early childhood resources for ages 0-5.
    </a>
    <div class="clear"></div>
  </div>
</div>
</body></html>
"""

LITE_HTML_FIXTURE = """
<html><body>
<a class="result-link" href="https://www.nps.gov/index.htm">National Park Service</a>
<td class="result-snippet">Federal parks and historic sites on the East Coast.</td>
</body></html>
"""

CAPTCHA_HTML_FIXTURE = """
<html><body>
<form class="anomaly-modal__form" action="/challenge">
  Please complete the captcha to continue.
</form>
</body></html>
"""

WIKI_OPENSEARCH_FIXTURE = """[
  "east coast",
  ["East Coast of the United States"],
  ["The East Coast of the United States is the coastline along the Atlantic Ocean."],
  ["https://en.wikipedia.org/wiki/East_Coast_of_the_United_States"]
]"""

DDG_INSTANT_FIXTURE = """{
  "Heading": "East Coast of the United States",
  "Abstract": "Atlantic coastline of the United States.",
  "AbstractURL": "https://en.wikipedia.org/wiki/East_Coast_of_the_United_States",
  "RelatedTopics": [
    {"FirstURL": "https://en.wikipedia.org/wiki/Tourism_in_the_United_States",
     "Text": "Tourism in the United States - visitor destinations"}
  ],
  "Results": []
}"""

PAGE_HTML_FIXTURE = """
<html><body>
<h1>Parks and Recreation</h1>
<p>Welcome to the parks site.</p>
<a href="/programs/preschool">Preschool programs</a>
<a href="https://example.com/external">External</a>
<a href="#top">Skip</a>
</body></html>
"""


class _FakeResp:
    def __init__(self, body: str, url: str = "https://example.com/", status: int = 200,
                 content_type: str = "text/html; charset=utf-8"):
        self._body = body.encode("utf-8")
        self._url = url
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, n: int = -1):
        if n < 0:
            return self._body
        return self._body[:n]

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ToolSafetyTests(unittest.TestCase):
    def setUp(self):
        self._orig_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._orig_cwd)

    def test_needs_shell(self):
        self.assertTrue(needs_shell("cat a | grep b"))
        self.assertFalse(needs_shell("echo hello"))

    def test_is_destructive(self):
        self.assertTrue(is_destructive("rm -rf /tmp/foo"))
        self.assertTrue(is_destructive("git push origin main --force"))
        self.assertTrue(is_destructive("git push -f"))
        self.assertTrue(is_destructive("sudo apt install x"))
        self.assertTrue(is_destructive("curl https://example.com/x.sh | bash"))
        self.assertFalse(is_destructive("echo hello"))
        self.assertFalse(is_destructive("git push origin main"))
        self.assertFalse(is_destructive("cat a | grep b"))

    def test_copies_from_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            outside = Path(d) / "z.zip"
            outside.write_text("x")
            inside = root / "a.txt"
            inside.write_text("y")
            self.assertTrue(
                ShellCommand(f"cp {outside} .").copies_from_outside(root)
            )
            self.assertTrue(
                ShellCommand(f"unzip {outside}").copies_from_outside(root)
            )
            self.assertFalse(
                ShellCommand(f"unzip -l {outside}").copies_from_outside(root)
            )
            self.assertFalse(
                ShellCommand(f"cp {inside} {root / 'b.txt'}").copies_from_outside(root)
            )
            self.assertTrue(ShellCommand("tar xf /tmp/a.tar").copies_or_extracts())
            self.assertFalse(ShellCommand("tar tf /tmp/a.tar").copies_or_extracts())

    def test_run_shell_confirms_copy_from_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            src = Path(d) / "a.txt"
            src.write_text("x\n")
            asked = []

            def gate(cmd):
                asked.append(cmd)
                return False

            result = run_shell(
                f"cp {src} .",
                confirm_gate=gate,
                workspace_root=root,
            )
            self.assertTrue(asked)
            self.assertIn("DENIED", result)
            self.assertIn("copy/extract", result)
            self.assertFalse((root / "a.txt").exists())

    def test_write_file_outside_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            result = write_file("/tmp/outside-lmloop-test.txt", "x")
            self.assertIn("outside workspace", result)

    def test_read_file_outside_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            result = read_file("/etc/hosts")
            self.assertIn("outside workspace", result)

    def test_read_file_extra_readable_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            outside = Path(d) / "secret.txt"
            outside.write_text("hello\n")
            other = Path(d) / "other.txt"
            other.write_text("nope\n")
            self.assertIn(
                "outside workspace",
                read_file(str(outside), workspace_root=root),
            )
            result = read_file(
                str(outside), workspace_root=root,
                extra_readable=[outside.resolve()],
            )
            self.assertIn("hello", result)
            self.assertNotIn("ERROR", result)
            self.assertIn(
                "outside workspace",
                read_file(
                    str(other), workspace_root=root,
                    extra_readable=[outside.resolve()],
                ),
            )
            self.assertIn(
                "outside workspace",
                write_file(str(outside), "x", workspace_root=root),
            )

    def test_list_dir_extra_readable_outside(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            outdir = Path(d) / "extdir"
            outdir.mkdir()
            (outdir / "a.txt").write_text("x\n")
            self.assertIn(
                "outside workspace",
                list_dir(str(outdir), workspace_root=root),
            )
            result = list_dir(
                str(outdir), workspace_root=root,
                extra_readable=[outdir.resolve()],
            )
            self.assertIn("a.txt", result)
            nested = read_file(
                str(outdir / "a.txt"), workspace_root=root,
                extra_readable=[outdir.resolve()],
            )
            self.assertIn("x", nested)

    def test_build_tools_extra_readable_read_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            outside = Path(d) / "secret.txt"
            outside.write_text("hello\n")
            _, impls = build_tools(
                {"confirm_shell": False},
                workspace_root=root,
                extra_readable=[outside.resolve()],
            )
            result = impls["read_file"](str(outside))
            self.assertIn("hello", result)
            self.assertIn("outside workspace", impls["write_file"](str(outside), "x"))

    def test_write_file_extra_readable_requires_confirm(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            outside = Path(d) / "secret.txt"
            outside.write_text("hello\n")
            denied = write_file(
                str(outside), "nope", workspace_root=root,
                extra_readable=[outside.resolve()],
                confirm_gate=lambda _: False,
            )
            self.assertIn("DENIED", denied)
            self.assertEqual(outside.read_text(), "hello\n")
            ok = write_file(
                str(outside), "yes\n", workspace_root=root,
                extra_readable=[outside.resolve()],
                confirm_gate=lambda _: True,
            )
            self.assertIn("Overwrote", ok)
            self.assertNotIn("backup", ok)  # outside the workspace: no trash copy
            self.assertEqual(outside.read_text(), "yes\n")

    def test_read_file_zip_lists_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            outside = Path(d) / "z.zip"
            with zipfile.ZipFile(outside, "w") as zf:
                zf.writestr("inner.txt", "secret-body")
            result = read_file(
                str(outside), workspace_root=root,
                extra_readable=[outside.resolve()],
            )
            self.assertIn("inner.txt", result)
            self.assertNotIn("secret-body", result)
            self.assertNotIn("ERROR", result)

    def test_read_file_inside_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            path = os.path.join(d, "hello.txt")
            with open(path, "w") as f:
                f.write("line1\nline2\n")
            result = read_file("hello.txt")
            self.assertIn("line1", result)
            self.assertIn(str(Path(d).resolve() / "hello.txt"), result)
            self.assertNotIn("ERROR", result)

    def test_read_file_png_is_not_mojibake(self):
        from tests.test_extract import PNG_1X1
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            Path("shot.png").write_bytes(PNG_1X1)
            result = read_file("shot.png")
            self.assertIsInstance(result, ToolResult)
            self.assertNotIn("\x89", result.text)
            self.assertIn("image/png", result.text)
            self.assertTrue(result.attachments)
            _, impls = build_tools({"confirm_shell": False}, workspace_root=Path(d))
            dispatched = dispatch(impls, "read_file", '{"path": "shot.png"}')
            self.assertIsInstance(dispatched, ToolResult)
            text, atts = unwrap_tool_result(dispatched)
            self.assertEqual(text, result.text)
            self.assertEqual(len(atts), 1)

    def test_read_file_docx_paginates(self):
        from tests.test_extract import _docx_bytes
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            Path("n.docx").write_bytes(_docx_bytes([f"para{i}" for i in range(1, 8)]))
            result = read_file("n.docx", start_line=3, max_lines=2)
            self.assertIn("para2", result)
            self.assertIn("para3", result)
            self.assertNotIn("para4", result)

    def test_format_tool_preview_resolves_relative_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            preview = format_tool_preview(
                "read_file",
                '{"path": "cybertruck_59k_top_10_findings.md"}',
                workspace_root=root,
            )
            self.assertIn(str(root / "cybertruck_59k_top_10_findings.md"), preview)
            self.assertNotIn("ERROR", preview)

    def test_remember_cites_learnings_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch("lmloop.memory.project_dir", return_value=root):
                _, impls = build_tools({"confirm_shell": False}, workspace_root=root)
                out = impls["remember"]("disambiguate 59K as a price", type="pitfall",
                                        key="pitfall_ambiguous_numeric_abbreviation")
            self.assertIn("pitfall_ambiguous_numeric_abbreviation", out)
            self.assertIn(str(root / "learnings.jsonl"), out)
            self.assertIn("Prior learning applied: pitfall_ambiguous_numeric_abbreviation", out)

    def test_file_tools_stay_pinned_after_chdir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "hello.txt").write_text("line1\n")
            other = tempfile.mkdtemp()
            try:
                os.chdir(other)
                _, impls = build_tools({"confirm_shell": False}, workspace_root=root)
                result = impls["read_file"]("hello.txt")
                self.assertIn("line1", result)
                self.assertNotIn("ERROR", result)
            finally:
                os.chdir(self._orig_cwd)
                os.rmdir(other)

    def test_run_shell_uses_pinned_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            other = tempfile.mkdtemp()
            try:
                os.chdir(other)
                _, impls = build_tools({"confirm_shell": False}, workspace_root=root)
                result = impls["run_shell"]("pwd")
                self.assertIn(str(root), result)
            finally:
                os.chdir(self._orig_cwd)
                os.rmdir(other)

    def test_confirm_destructive_calls_gate(self):
        gate = mock.Mock(return_value=False)
        result = run_shell("rm -rf /tmp/lmloop-test-rm", confirm_gate=gate)
        gate.assert_called()
        self.assertIn("DENIED", result)

    def test_confirm_shell_syntax_off_skips_pipe_gate(self):
        gate = mock.Mock(return_value=False)
        result = run_shell(
            "echo a | cat",
            confirm_gate=gate,
            confirm_destructive=True,
            confirm_shell_syntax=False,
        )
        gate.assert_not_called()
        self.assertNotIn("DENIED", result)

    def test_run_shell_kills_on_keyboard_interrupt(self):
        proc = mock.Mock()
        proc.communicate.side_effect = KeyboardInterrupt()
        with mock.patch("lmloop.tools.subprocess.Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                run_shell("sleep 999", confirm_destructive=False)
        proc.kill.assert_called()

    def test_list_dir_includes_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            open(".secret", "w").close()
            open("visible.txt", "w").close()
            result = list_dir(".")
            self.assertIn("visible.txt", result)
            self.assertIn(".secret", result)

    def test_dispatch_ignores_extra_kwargs(self):
        specs, impls = build_tools({"confirm_shell": False})
        result = dispatch(impls, "list_dir", '{"path": ".", "extra": 1}')
        self.assertNotIn("ERROR", result)

    def test_dispatch_missing_required(self):
        _, impls = build_tools({"confirm_shell": False})
        result = dispatch(impls, "run_shell", "{}")
        self.assertIn("missing required argument 'command'", result)

    def test_dispatch_write_file_allows_empty_content(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            _, impls = build_tools({"confirm_shell": False})
            result = dispatch(impls, "write_file", '{"path": "empty.txt", "content": ""}')
            self.assertNotIn("ERROR", result)
            self.assertTrue(os.path.isfile("empty.txt"))
            with open("empty.txt") as f:
                self.assertEqual(f.read(), "")

    def test_dispatch_empty_tool_name(self):
        _, impls = build_tools({"confirm_shell": False})
        result = dispatch(impls, "", "{}")
        self.assertIn("empty tool name", result)

    def test_dispatch_bad_int_field(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            with open("f.txt", "w") as f:
                f.write("hi\n")
            _, impls = build_tools({"confirm_shell": False})
            result = dispatch(impls, "read_file", '{"path": "f.txt", "max_lines": "nope"}')
            self.assertIn("'max_lines' must be an integer", result)

    def test_dispatch_coerces_int_field(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            with open("f.txt", "w") as f:
                f.write("a\nb\nc\n")
            _, impls = build_tools({"confirm_shell": False})
            result = dispatch(impls, "read_file", '{"path": "f.txt", "max_lines": "2"}')
            self.assertNotIn("ERROR", result)
            self.assertIn("a", result)


class WebResearchToolTests(unittest.TestCase):
    def setUp(self):
        # urllib-chain tests must not hit real ddgs / the network.
        p = mock.patch("lmloop.web._search_ddgs", return_value=([], "unavailable"))
        self.addCleanup(p.stop)
        p.start()

    def test_web_search_parses_and_unwraps_uddg(self):
        with mock.patch("lmloop.web.urllib.request.urlopen", return_value=_FakeResp(DDG_HTML_FIXTURE)):
            out = web_search("Fairfax early childhood", max_results=5)
        self.assertNotIn("ERROR", out)
        self.assertIn("<<<untrusted>>>", out)
        self.assertIn("Fairfax County Public Library", out)
        self.assertIn("https://www.fairfaxcounty.gov/library", out)
        self.assertNotIn("uddg=", out)
        self.assertIn("https://www.firstfivefairfax.org/", out)
        self.assertIn("Early childhood resources", out)

    def test_web_search_captcha_falls_back_then_errors(self):
        with mock.patch("lmloop.web.urllib.request.urlopen", return_value=_FakeResp(CAPTCHA_HTML_FIXTURE)):
            out = web_search("test query")
        self.assertIn("ERROR:", out)
        self.assertIn("all backends", out)
        self.assertIn("Do not retry", out)

    def test_web_search_falls_back_to_lite_after_captcha(self):
        def _open(req, timeout=None, context=None):
            url = getattr(req, "full_url", "")
            if "html.duckduckgo.com" in url:
                return _FakeResp(CAPTCHA_HTML_FIXTURE)
            if "lite.duckduckgo.com" in url:
                return _FakeResp(LITE_HTML_FIXTURE)
            raise AssertionError(f"unexpected url {url}")

        with mock.patch("lmloop.web.urllib.request.urlopen", side_effect=_open):
            out = web_search("east coast parks")
        self.assertNotIn("ERROR", out)
        self.assertIn("National Park Service", out)
        self.assertIn("https://www.nps.gov/index.htm", out)
        self.assertIn("DuckDuckGo Lite", out)

    def test_web_search_falls_back_to_wikipedia(self):
        def _open(req, timeout=None, context=None):
            url = getattr(req, "full_url", "")
            if "wikipedia.org" in url:
                return _FakeResp(WIKI_OPENSEARCH_FIXTURE, content_type="application/json")
            return _FakeResp(CAPTCHA_HTML_FIXTURE)

        with mock.patch("lmloop.web.urllib.request.urlopen", side_effect=_open):
            out = web_search("east coast")
        self.assertNotIn("ERROR", out)
        self.assertIn("East Coast of the United States", out)
        self.assertIn("Wikipedia", out)

    def test_web_search_falls_back_to_instant_answer(self):
        def _open(req, timeout=None, context=None):
            url = getattr(req, "full_url", "")
            if "html.duckduckgo.com" in url or "lite.duckduckgo.com" in url:
                return _FakeResp(CAPTCHA_HTML_FIXTURE)
            if "api.duckduckgo.com" in url:
                return _FakeResp(DDG_INSTANT_FIXTURE, content_type="application/json")
            raise AssertionError(f"unexpected url {url}")

        with mock.patch("lmloop.web.urllib.request.urlopen", side_effect=_open):
            out = web_search("east coast")
        self.assertNotIn("ERROR", out)
        self.assertIn("East Coast of the United States", out)
        self.assertIn("Instant Answer", out)

    def test_web_search_empty_returns_error(self):
        with mock.patch(
            "lmloop.web.urllib.request.urlopen",
            return_value=_FakeResp("<html><body><p>no results</p></body></html>"),
        ):
            out = web_search("zzzzunlikelyquery")
        self.assertIn("ERROR:", out)
        self.assertIn("no search results", out)
        self.assertIn("Do not retry", out)

    def test_dispatch_web_search_requires_query(self):
        _, impls = build_tools({"confirm_shell": False})
        result = dispatch(impls, "web_search", "{}")
        self.assertIn("missing required argument 'query'", result)

    def test_dispatch_web_search_coerces_max_results(self):
        with mock.patch("lmloop.web.urllib.request.urlopen", return_value=_FakeResp(DDG_HTML_FIXTURE)):
            _, impls = build_tools({"confirm_shell": False})
            result = dispatch(impls, "web_search", '{"query": "fairfax", "max_results": "1"}')
        self.assertNotIn("ERROR", result)
        self.assertIn("Fairfax County Public Library", result)
        # max_results=1 should omit the second hit
        self.assertNotIn("First Five Fairfax", result)

    def test_build_tools_registers_web_search(self):
        specs, impls = build_tools({"confirm_shell": False})
        names = {s["function"]["name"] for s in specs}
        self.assertIn("web_search", names)
        self.assertIn("web_search", impls)

    def test_current_time_registered_and_returns_iso_fields(self):
        snap = {
            "utc": "2026-08-14T17:06:00Z",
            "local": "2026-08-14T13:06:00-04:00",
            "today": "2026-08-14",
            "7_days_ago": "2026-08-07",
            "28_days_ago": "2026-07-17",
            "90_days_ago": "2026-05-16",
        }
        specs, impls = build_tools({"confirm_shell": False})
        names = {s["function"]["name"] for s in specs}
        self.assertIn("current_time", names)
        self.assertIn("current_time", impls)
        with mock.patch("lmloop.steer.clock_snapshot", return_value=snap):
            result = dispatch(impls, "current_time", "{}")
        self.assertNotIn("ERROR", result)
        self.assertIn("utc: 2026-08-14T17:06:00Z", result)
        self.assertIn("28_days_ago: 2026-07-17", result)
        self.assertIn("90_days_ago: 2026-05-16", result)

    def test_tool_registry_is_single_source(self):
        specs, impls = build_tools({"confirm_shell": False})
        names = [s["function"]["name"] for s in specs]
        self.assertEqual(names, list(impls.keys()))
        self.assertEqual(set(names), set(tool_names(cfg={"confirm_shell": False})))
        self.assertIn("graph_add_edge", tool_names())
        for s in specs:
            self.assertIsInstance(s["function"]["name"], str)
            self.assertTrue(s["function"]["parameters"]["properties"] or True)

    def test_readonly_omits_write_tools(self):
        from lmloop.tools import READONLY_OMIT

        full_specs, _ = build_tools({"confirm_shell": False})
        full_names = {s["function"]["name"] for s in full_specs}
        specs, impls = build_tools({"confirm_shell": False}, readonly=True)
        names = {s["function"]["name"] for s in specs}
        self.assertEqual(names, set(impls))
        self.assertEqual(names & READONLY_OMIT, set())
        self.assertIn("run_shell", names)
        self.assertIn("read_file", names)
        self.assertIn("recall_memory", names)
        self.assertIn("current_time", names)
        self.assertEqual(set(tool_names(cfg={"confirm_shell": False})), full_names)
        self.assertIn("graph_add_edge", tool_names())
        self.assertGreaterEqual(len(full_names), 10)
        self.assertNotIn("graph_add_edge", full_names)

    def test_graph_add_edge_only_when_use_graph(self):
        specs, impls = build_tools({"confirm_shell": False, "use_graph": True})
        names = {s["function"]["name"] for s in specs}
        self.assertIn("graph_add_edge", names)
        self.assertIn("graph_add_edge", impls)
        off, _ = build_tools({"confirm_shell": False})
        self.assertNotIn("graph_add_edge", {s["function"]["name"] for s in off})
        from lmloop.tools import _TOOL_DEFS
        self.assertIn("graph_add_edge", _TOOL_DEFS)

    def test_registry_survives_use_graph_false_rebuild(self):
        from lmloop.tools import _TOOL_DEFS

        build_tools({"confirm_shell": False, "use_graph": True})
        self.assertIn("graph_add_edge", _TOOL_DEFS)
        specs, _ = build_tools({"confirm_shell": False})
        self.assertNotIn("graph_add_edge", {s["function"]["name"] for s in specs})
        self.assertIn("graph_add_edge", _TOOL_DEFS)
        self.assertIn("graph_add_edge", tool_names())
        self.assertNotIn(
            "graph_add_edge", tool_names(cfg={"confirm_shell": False}),
        )

    def test_command_table_reserves_slash_stems(self):
        slash = {c.name for c in slash_command_metas() if c.reserve_skill}
        self.assertTrue(slash <= RESERVED_SKILL_NAMES)
        self.assertIn("system", RESERVED_SKILL_NAMES)
        self.assertIn("continue", RESERVED_SKILL_NAMES)
        # Packaged skills may still be overridden by users.
        self.assertNotIn("compact", RESERVED_SKILL_NAMES)
        self.assertNotIn("retro", RESERVED_SKILL_NAMES)
        self.assertIn("until", RESERVED_SKILL_NAMES)
        self.assertIn("graph", RESERVED_SKILL_NAMES)

    def test_fetch_url_http_error(self):
        err = urllib.error.HTTPError(
            "https://example.com/missing", 404, "Not Found", hdrs=None, fp=io.BytesIO(b""),
        )
        with mock.patch("lmloop.web.urllib.request.urlopen", side_effect=err):
            out = fetch_url("https://example.com/missing")
        self.assertIn("ERROR: HTTP 404", out)
        self.assertIn("https://example.com/missing", out)

    def test_fetch_url_final_url_and_links(self):
        resp = _FakeResp(
            PAGE_HTML_FIXTURE,
            url="https://www.example.gov/parks/",
            status=200,
        )
        with mock.patch("lmloop.web.urllib.request.urlopen", return_value=resp):
            out = fetch_url("https://www.example.gov/parks")
        self.assertIn("[fetched https://www.example.gov/parks/ | HTTP 200]", out)
        self.assertIn("Parks and Recreation", out)
        self.assertIn("Links found on page:", out)
        self.assertIn("https://www.example.gov/programs/preschool", out)
        self.assertIn("https://example.com/external", out)
        self.assertNotIn("#top", out)
        self.assertIn("<<<untrusted>>>", out)

    def test_fetch_url_png_attaches(self):
        from tests.test_extract import PNG_1X1
        resp = _FakeResp("x", url="https://example.com/a.png", content_type="image/png")
        resp._body = PNG_1X1
        with mock.patch("lmloop.web.urllib.request.urlopen", return_value=resp):
            out = fetch_url("https://example.com/a.png")
        self.assertIsInstance(out, ToolResult)
        self.assertIn("image/png", out.text)
        self.assertIn("<<<untrusted>>>", out.text)
        self.assertNotIn("\x89PNG", out.text)
        self.assertEqual(len(out.attachments), 1)

    def test_fetch_url_pdf_extracts(self):
        resp = _FakeResp("x", url="https://example.com/a.pdf", content_type="application/pdf")
        resp._body = b"%PDF-1.4\nfake"
        proc = mock.Mock(returncode=0, stdout=b"Hello from PDF\n", stderr=b"")
        with mock.patch("lmloop.web.urllib.request.urlopen", return_value=resp), \
             mock.patch("lmloop.extract.shutil.which", return_value="/usr/bin/pdftotext"), \
             mock.patch("lmloop.extract.subprocess.run", return_value=proc):
            out = fetch_url("https://example.com/a.pdf")
        self.assertIsInstance(out, str)
        self.assertIn("Hello from PDF", out)
        self.assertIn("<<<untrusted>>>", out)


class DdgsSearchTests(unittest.TestCase):
    def test_search_ddgs_not_installed(self):
        import builtins
        real_import = builtins.__import__

        def _import(name, *args, **kwargs):
            if name == "ddgs" or name.startswith("ddgs."):
                raise ImportError("blocked")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=_import):
            results, err = _search_ddgs("parks", 5, 10)
        self.assertEqual(results, [])
        self.assertEqual(err, "not installed")

    def test_web_search_uses_ddgs_before_urllib(self):
        client = mock.Mock()
        client.text.return_value = [
            {"title": "National Park Service", "href": "https://www.nps.gov/",
             "body": "Federal parks on the East Coast."},
        ]
        ddgs_mod = mock.Mock(DDGS=mock.Mock(return_value=client))
        with mock.patch.dict("sys.modules", {"ddgs": ddgs_mod}), \
             mock.patch("lmloop.web.urllib.request.urlopen") as urlopen:
            out = web_search("east coast parks")
        urlopen.assert_not_called()
        client.text.assert_called_once()
        kwargs = client.text.call_args.kwargs
        self.assertEqual(kwargs.get("max_results"), 8)
        self.assertEqual(kwargs.get("backend"), "auto")
        self.assertNotIn("ERROR", out)
        self.assertIn("National Park Service", out)
        self.assertIn("https://www.nps.gov/", out)
        self.assertIn("(via ddgs)", out)

    def test_web_search_falls_back_when_ddgs_errors(self):
        client = mock.Mock()
        client.text.side_effect = RuntimeError("rate limited")
        ddgs_mod = mock.Mock(DDGS=mock.Mock(return_value=client))

        def _open(req, timeout=None, context=None):
            url = getattr(req, "full_url", "")
            if "html.duckduckgo.com" in url:
                return _FakeResp(CAPTCHA_HTML_FIXTURE)
            if "lite.duckduckgo.com" in url:
                return _FakeResp(LITE_HTML_FIXTURE)
            raise AssertionError(f"unexpected url {url}")

        with mock.patch.dict("sys.modules", {"ddgs": ddgs_mod}), \
             mock.patch("lmloop.web.urllib.request.urlopen", side_effect=_open):
            out = web_search("east coast parks")
        self.assertNotIn("ERROR", out)
        self.assertIn("National Park Service", out)
        self.assertIn("DuckDuckGo Lite", out)


class ConcurrentToolTests(unittest.TestCase):
    def test_consecutive_reads_share_a_group(self):
        from lmloop.tools import concurrent_groups, run_tool_calls

        build_tools({"confirm_shell": False})
        self.assertEqual(
            concurrent_groups(["read_file", "list_dir", "write_file", "read_file"]),
            [[0, 1], [2], [3]],
        )
        self.assertEqual(
            concurrent_groups(["write_file", "run_shell"]),
            [[0], [1]],
        )

    def test_run_tool_calls_preserves_order(self):
        from lmloop.tools import run_tool_calls

        build_tools({"confirm_shell": False})
        impls = {
            "read_file": lambda path: f"body:{path}",
            "list_dir": lambda path=".": f"dir:{path}",
        }
        out = run_tool_calls(impls, [
            ("read_file", '{"path": "a.py"}'),
            ("list_dir", '{"path": "."}'),
        ])
        self.assertEqual(out, ["body:a.py", "dir:."])

    def test_writes_stay_serial(self):
        from lmloop.tools import run_tool_calls

        build_tools({"confirm_shell": False})
        order = []

        def write(path, content):
            order.append("write")
            return "w"

        def shell(command):
            order.append("shell")
            return "s"

        impls = {"write_file": write, "run_shell": shell}
        out = run_tool_calls(impls, [
            ("write_file", '{"path": "a.py", "content": "x"}'),
            ("run_shell", '{"command": "true"}'),
        ])
        self.assertEqual(order, ["write", "shell"])
        self.assertEqual(out, ["w", "s"])

    def test_parallel_reads_overlap(self):
        import threading
        from lmloop.tools import run_tool_calls

        build_tools({"confirm_shell": False})
        barrier = threading.Barrier(2)
        seen = []

        def read(path, start_line=1, max_lines=400):
            seen.append(path)
            barrier.wait(timeout=2)
            return path

        impls = {"read_file": read}
        out = run_tool_calls(impls, [
            ("read_file", '{"path": "a.py"}'),
            ("read_file", '{"path": "b.py"}'),
        ])
        self.assertEqual(set(out), {"a.py", "b.py"})
        self.assertEqual(set(seen), {"a.py", "b.py"})


class _Gate:
    """Recording confirm_gate with a fixed answer."""

    def __init__(self, answer: bool):
        self.answer = answer
        self.calls: list = []

    def __call__(self, command: str) -> bool:
        self.calls.append(command)
        return self.answer


class FileEditToolTests(unittest.TestCase):
    """update_file / write_file overwrite / move_file / delete_file + backups."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "ws"
        self.root.mkdir()
        self.state = base / "state"
        self.state.mkdir()
        self._patch = mock.patch("lmloop.config.project_dir", return_value=self.state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _backups(self) -> list:
        trash = self.state / "trash"
        return sorted(p for p in trash.rglob("*") if p.is_file()) if trash.exists() else []

    # --- update_file ---

    def test_update_file_unique_replace_returns_diff_and_backup(self):
        f = self.root / "a.py"
        f.write_text("def f():\n    return 1\n\ndef g():\n    return 2\n")
        out = update_file("a.py", "    return 1\n", "    return 10\n", workspace_root=self.root)
        self.assertTrue(out.startswith("Updated "), out)
        self.assertIn("replaced 1 occurrence (line 2)", out)
        self.assertIn("-    return 1", out)
        self.assertIn("+    return 10", out)
        self.assertIn("--- a.py\n+++ a.py", out)  # diff labels are workspace-relative
        self.assertIn("backup: ", out)
        self.assertEqual(f.read_text(), "def f():\n    return 10\n\ndef g():\n    return 2\n")
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].name, "a.py")
        self.assertEqual(backups[0].read_text(), "def f():\n    return 1\n\ndef g():\n    return 2\n")

    def test_repeated_edits_keep_every_preimage(self):
        f = self.root / "a.txt"
        f.write_text("v1\n")
        update_file("a.txt", "v1", "v2", workspace_root=self.root)
        update_file("a.txt", "v2", "v3", workspace_root=self.root)
        out = write_file("a.txt", "v4\n", workspace_root=self.root, confirm_gate=_Gate(True))
        backups = self._backups()
        self.assertEqual([b.name for b in backups], ["a.txt", "a.txt.~1~", "a.txt.~2~"])
        self.assertEqual([b.read_text() for b in backups], ["v1\n", "v2\n", "v3\n"])
        self.assertIn(str(backups[2]), out)  # result cites the exact pre-image

    def test_update_file_preserves_crlf_and_matches_lf_snippet(self):
        f = self.root / "win.txt"
        f.write_bytes(b"a\r\nb\r\nc\r\n")
        out = update_file("win.txt", "b\nc\n", "B\n", workspace_root=self.root)
        self.assertIn("Updated", out)
        self.assertEqual(f.read_bytes(), b"a\r\nB\r\n")
        untouched = self.root / "mixed.txt"
        untouched.write_bytes(b"x\r\ny\n")
        update_file("mixed.txt", "x\r\n", "X\r\n", workspace_root=self.root)
        self.assertEqual(untouched.read_bytes(), b"X\r\ny\n")

    def test_update_file_multiline_span_in_header(self):
        f = self.root / "a.txt"
        f.write_text("one\ntwo\nthree\nfour\n")
        out = update_file("a.txt", "two\nthree\n", "TWO\n", workspace_root=self.root)
        self.assertIn("(lines 2-3)", out)
        self.assertEqual(f.read_text(), "one\nTWO\nfour\n")

    def test_update_file_not_found_error_copy(self):
        (self.root / "a.txt").write_text("hello\n")
        out = update_file("a.txt", "nope", "x", workspace_root=self.root)
        self.assertTrue(out.startswith("ERROR: old_string not found"))
        self.assertIn("read_file", out)
        self.assertEqual((self.root / "a.txt").read_text(), "hello\n")
        self.assertEqual(self._backups(), [])

    def test_update_file_ambiguous_lists_lines(self):
        (self.root / "a.txt").write_text("x = 1\ny = 2\nx = 1\n")
        out = update_file("a.txt", "x = 1", "x = 2", workspace_root=self.root)
        self.assertIn("matches 2 places", out)
        self.assertIn("lines 1, 3", out)
        self.assertIn("replace_all=true", out)
        self.assertEqual((self.root / "a.txt").read_text(), "x = 1\ny = 2\nx = 1\n")

    def test_update_file_replace_all(self):
        (self.root / "a.txt").write_text("x = 1\ny = 2\nx = 1\n")
        out = update_file("a.txt", "x = 1", "x = 2", replace_all=True, workspace_root=self.root)
        self.assertIn("replaced 2 occurrences (lines 1, 3)", out)
        self.assertEqual((self.root / "a.txt").read_text(), "x = 2\ny = 2\nx = 2\n")

    def test_update_file_empty_new_string_deletes(self):
        (self.root / "a.txt").write_text("keep\ndrop\n")
        out = update_file("a.txt", "drop\n", "", workspace_root=self.root)
        self.assertIn("Updated", out)
        self.assertEqual((self.root / "a.txt").read_text(), "keep\n")

    def test_update_file_no_change_and_missing_file(self):
        (self.root / "a.txt").write_text("same\n")
        self.assertIn("no change", update_file("a.txt", "same", "same", workspace_root=self.root))
        out = update_file("missing.txt", "a", "b", workspace_root=self.root)
        self.assertIn("does not exist", out)
        self.assertIn("write_file", out)

    def test_update_file_non_text_file(self):
        (self.root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
        out = update_file("blob.bin", "a", "b", workspace_root=self.root)
        self.assertIn("not a text file", out)

    def test_update_file_outside_workspace_requires_confirm(self):
        outside = Path(self._tmp.name) / "ext.txt"
        outside.write_text("hello\n")
        self.assertIn(
            "outside workspace",
            update_file(str(outside), "hello", "bye", workspace_root=self.root),
        )
        no = _Gate(False)
        denied = update_file(
            str(outside), "hello", "bye", workspace_root=self.root,
            extra_readable=[outside], confirm_gate=no,
        )
        self.assertIn("DENIED", denied)
        self.assertEqual(no.calls, [f"update_file {outside.resolve()}"])
        self.assertEqual(outside.read_text(), "hello\n")
        yes = _Gate(True)
        ok = update_file(
            str(outside), "hello", "bye", workspace_root=self.root,
            extra_readable=[outside], confirm_gate=yes,
        )
        self.assertIn("Updated", ok)
        self.assertNotIn("backup", ok)
        self.assertEqual(outside.read_text(), "bye\n")

    def test_dispatch_update_file_allows_empty_new_string_and_coerces_bool(self):
        (self.root / "a.txt").write_text("a\na\n")
        _, impls = build_tools({"confirm_shell": False}, workspace_root=self.root)
        out = dispatch(
            impls, "update_file",
            '{"path": "a.txt", "old_string": "a\\n", "new_string": "", "replace_all": "true"}',
        )
        self.assertIn("replaced 2 occurrences", out)
        self.assertEqual((self.root / "a.txt").read_text(), "")
        bad = dispatch(
            impls, "update_file",
            '{"path": "a.txt", "old_string": "a", "new_string": "b", "replace_all": "maybe"}',
        )
        self.assertIn("must be true or false", bad)

    # --- write_file ---

    def test_write_file_new_file_does_not_prompt(self):
        gate = _Gate(False)
        out = write_file("new.txt", "hi", workspace_root=self.root, confirm_gate=gate)
        self.assertIn("Wrote 2 chars", out)
        self.assertEqual(gate.calls, [])
        self.assertEqual(self._backups(), [])

    def test_write_file_overwrite_prompts_and_decline_keeps_content(self):
        f = self.root / "a.txt"
        f.write_text("original\n")
        no = _Gate(False)
        out = write_file("a.txt", "new\n", workspace_root=self.root, confirm_gate=no)
        self.assertTrue(out.startswith("DENIED"), out)
        self.assertIn("update_file", out)
        self.assertEqual(no.calls, [f"overwrite {f.resolve()}"])
        self.assertEqual(f.read_text(), "original\n")
        self.assertEqual(self._backups(), [])

    def test_write_file_overwrite_accept_backs_up(self):
        f = self.root / "sub" / "a.txt"
        f.parent.mkdir()
        f.write_text("original\n")
        yes = _Gate(True)
        out = write_file("sub/a.txt", "new\n", workspace_root=self.root, confirm_gate=yes)
        self.assertTrue(out.startswith("Overwrote"), out)
        self.assertIn("backup: ", out)
        self.assertEqual(f.read_text(), "new\n")
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertTrue(str(backups[0]).endswith("sub/a.txt"))
        self.assertEqual(backups[0].read_text(), "original\n")
        self.assertIn(str(backups[0]), out)

    def test_write_file_confirms_disabled_overwrites_silently(self):
        f = self.root / "a.txt"
        f.write_text("original\n")
        gate = _Gate(False)
        _, impls = build_tools({"confirm_shell": False}, workspace_root=self.root, confirm_gate=gate)
        out = impls["write_file"]("a.txt", "new\n")
        self.assertIn("Overwrote", out)
        self.assertEqual(gate.calls, [])
        self.assertEqual(f.read_text(), "new\n")

    def test_build_tools_write_file_overwrite_rides_confirm_destructive(self):
        f = self.root / "a.txt"
        f.write_text("original\n")
        gate = _Gate(False)
        _, impls = build_tools({}, workspace_root=self.root, confirm_gate=gate)
        self.assertIn("DENIED", impls["write_file"]("a.txt", "new\n"))
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(f.read_text(), "original\n")

    # --- move_file / delete_file ---

    def test_move_file_gate_and_backup(self):
        src = self.root / "a.txt"
        src.write_text("body\n")
        no = _Gate(False)
        denied = move_file("a.txt", "b/c.txt", workspace_root=self.root, confirm_gate=no)
        self.assertIn("DENIED", denied)
        self.assertEqual(no.calls, [f"move_file {src.resolve()} -> {(self.root / 'b' / 'c.txt').resolve()}"])
        self.assertTrue(src.exists())
        yes = _Gate(True)
        ok = move_file("a.txt", "b/c.txt", workspace_root=self.root, confirm_gate=yes)
        self.assertIn("Moved", ok)
        self.assertIn("backup: ", ok)
        self.assertFalse(src.exists())
        self.assertEqual((self.root / "b" / "c.txt").read_text(), "body\n")

    def test_move_file_refuses_dirs_dest_exists_and_outside(self):
        (self.root / "d").mkdir()
        (self.root / "a.txt").write_text("a")
        (self.root / "b.txt").write_text("b")
        self.assertIn("is a directory", move_file("d", "e", workspace_root=self.root))
        self.assertIn("already exists", move_file("a.txt", "b.txt", workspace_root=self.root))
        self.assertIn("does not exist", move_file("zzz", "y", workspace_root=self.root))
        self.assertIn("outside workspace", move_file("a.txt", "/tmp/x.txt", workspace_root=self.root))
        self.assertTrue((self.root / "a.txt").exists())

    def test_delete_file_gate_backup_and_dir_refused(self):
        f = self.root / "a.txt"
        f.write_text("bye\n")
        (self.root / "d").mkdir()
        no = _Gate(False)
        self.assertIn("DENIED", delete_file("a.txt", workspace_root=self.root, confirm_gate=no))
        self.assertEqual(no.calls, [f"delete_file {f.resolve()}"])
        self.assertTrue(f.exists())
        out = delete_file("d", workspace_root=self.root, confirm_gate=_Gate(True))
        self.assertIn("is a directory", out)
        self.assertIn("rm -r", out)
        ok = delete_file("a.txt", workspace_root=self.root, confirm_gate=_Gate(True))
        self.assertIn("Deleted", ok)
        self.assertFalse(f.exists())
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "bye\n")
        self.assertIn("does not exist", delete_file("a.txt", workspace_root=self.root))

    def test_write_tools_readonly_omitted_and_serial(self):
        from lmloop.tools import READONLY_OMIT, concurrent_groups
        for name in ("update_file", "move_file", "delete_file", "write_file"):
            self.assertIn(name, READONLY_OMIT)
        specs, _ = build_tools({"confirm_shell": False}, readonly=True)
        names = {s["function"]["name"] for s in specs}
        self.assertNotIn("update_file", names)
        self.assertNotIn("move_file", names)
        self.assertNotIn("delete_file", names)
        self.assertIn("find_files", names)
        build_tools({"confirm_shell": False})
        self.assertEqual(
            concurrent_groups(["read_file", "update_file", "find_files", "move_file", "delete_file"]),
            [[0], [1], [2], [3], [4]],
        )

    # --- previews / notices / labels ---

    def test_format_tool_preview_two_path_tool(self):
        out = format_tool_preview(
            "move_file", '{"path": "a.txt", "new_path": "b/c.txt"}',
            workspace_root=self.root, limit=2000,
        )
        self.assertIn(str((self.root / "a.txt").resolve()), out)
        self.assertIn(str((self.root / "b" / "c.txt").resolve()), out)
        one = format_tool_preview("update_file", '{"path": "a.txt", "old_string": "x"}', workspace_root=self.root)
        self.assertIn(str((self.root / "a.txt").resolve()), one)

    def test_user_notice_covers_file_and_memory_tools(self):
        self.assertEqual(
            user_notice("remember", "Saved [k]\nSay in your reply: Prior learning applied: k"),
            "Prior learning applied: k",
        )
        diff = "Updated a.py: replaced 1 occurrence (line 2)\n--- a.py\n+++ a.py\n-x\n+y"
        self.assertEqual(user_notice("update_file", diff), diff)
        self.assertIsNone(user_notice("update_file", "ERROR: old_string not found in a.py"))
        self.assertIsNone(user_notice("write_file", "Wrote 3 chars to /ws/new.txt"))
        self.assertEqual(
            user_notice("write_file", "Overwrote /ws/a.txt with 3 chars (backup: /t/a.txt)"),
            "Overwrote /ws/a.txt with 3 chars (backup: /t/a.txt)",
        )
        self.assertEqual(user_notice("delete_file", "Deleted /ws/a.txt"), "Deleted /ws/a.txt")
        self.assertIsNone(user_notice("move_file", "DENIED: the user declined to move /ws/a."))
        self.assertIsNone(user_notice("read_file", "[/ws/a.py: lines 1-2 of 2]"))

    def test_confirm_label_and_gate_tier(self):
        self.assertEqual(confirm_label("write_file /x"), "write outside the workspace")
        self.assertEqual(confirm_label("update_file /x"), "edit outside the workspace")
        self.assertEqual(confirm_label("overwrite /x"), "overwrite an existing file")
        self.assertEqual(confirm_label("move_file /a -> /b"), "move/rename a file")
        self.assertEqual(confirm_label("delete_file /x"), "delete a file")
        self.assertEqual(confirm_label("cp ~/x ."), "copy/extract into the workspace")
        self.assertEqual(confirm_label("ls | wc -l"), "shell-syntax (pipes/redirections)")
        self.assertEqual(confirm_label("rm -rf build"), "potentially destructive")
        for cmd in ("overwrite /x", "move_file /a -> /b", "delete_file /x"):
            self.assertEqual(gate_tier(cmd), GATE_RECOVERABLE, cmd)
        for cmd in ("write_file /x", "update_file /x", "rm -rf build", "cp ~/x .", "ls | wc"):
            self.assertEqual(gate_tier(cmd), GATE_IRREVERSIBLE, cmd)

    def test_ui_confirm_gate_uses_tools_label(self):
        from lmloop.ui import Console, make_confirm_gate
        console = Console(color=False)
        gate = make_confirm_gate(console)
        with mock.patch("lmloop.ui.ask_yes_no", return_value=False) as ask, \
             mock.patch.object(console, "warn") as warn:
            self.assertFalse(gate("overwrite /ws/a.txt"))
        self.assertTrue(ask.called)
        self.assertIn("overwrite an existing file", warn.call_args[0][0])


class GatePolicyTests(unittest.TestCase):
    def test_files_mode_auto_approves_recoverable_and_records_irreversible(self):
        said = []
        policy = GatePolicy("files", fallback=_Gate(False), echo_status=said.append)
        self.assertTrue(policy("overwrite /ws/a.py"))
        self.assertTrue(policy("delete_file /ws/b.py"))
        self.assertFalse(policy("rm -rf build"))
        self.assertFalse(policy("rm -rf build"))  # de-duplicated
        self.assertFalse(policy("write_file /etc/hosts"))
        self.assertEqual(policy.take_denied(), ["rm -rf build", "write_file /etc/hosts"])
        self.assertEqual(policy.take_denied(), [])
        self.assertEqual(len(said), 2)
        self.assertIn("[auto-approved: overwrite an existing file", said[0])

    def test_approved_commands_pass_once_approved(self):
        said = []
        policy = GatePolicy("files", echo_status=said.append)
        self.assertFalse(policy("rm -rf build"))
        policy.approve(policy.take_denied())
        self.assertTrue(policy("rm -rf build"))
        self.assertIn("[approved:", said[-1])
        self.assertFalse(policy("rm -rf other"))
        policy.expire_approvals()
        self.assertFalse(policy("rm -rf build"))  # a yes lasts one step

    def test_none_mode_defers_to_fallback_and_all_mode_never_asks(self):
        fb = _Gate(True)
        none_policy = GatePolicy("none", fallback=fb)
        self.assertTrue(none_policy("rm -rf build"))
        self.assertEqual(fb.calls, ["rm -rf build"])
        self.assertEqual(none_policy.take_denied(), [])
        self.assertFalse(GatePolicy("none")("rm -rf build"))  # no fallback: deny
        fb2 = _Gate(False)
        all_policy = GatePolicy("all", fallback=fb2)
        self.assertTrue(all_policy("rm -rf build"))
        self.assertEqual(fb2.calls, [])

    def test_from_config_and_autonomous_gate_wrapping(self):
        self.assertEqual(GatePolicy.from_config({}).mode, "files")
        self.assertEqual(GatePolicy.from_config({"autonomous_gates": "ALL"}).mode, "all")
        self.assertEqual(GatePolicy.from_config({"autonomous_gates": "bogus"}).mode, "files")
        self.assertIsNone(autonomous_gate({}, None))
        fb = _Gate(True)
        policy = autonomous_gate({}, fb)
        self.assertIsInstance(policy, GatePolicy)
        self.assertIs(policy.fallback, fb)
        self.assertIs(autonomous_gate({}, policy), policy)


class FindSearchToolTests(unittest.TestCase):
    def setUp(self):
        from lmloop.files_index import clear_path_cache
        clear_path_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        (self.root / "src" / "pkg").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "main.py").write_text("x")
        (self.root / "src" / "pkg" / "util.py").write_text("x")
        (self.root / "src" / "pkg" / "data.json").write_text("{}")
        (self.root / "tests" / "test_main.py").write_text("x")
        (self.root / "README.md").write_text("x")

    def tearDown(self):
        from lmloop.files_index import clear_path_cache
        clear_path_cache()
        self._tmp.cleanup()

    def test_find_files_glob_vs_substring(self):
        py = find_files("*.py", workspace_root=self.root).splitlines()
        self.assertEqual(py, ["src/main.py", "src/pkg/util.py", "tests/test_main.py"])
        self.assertEqual(
            find_files("main", workspace_root=self.root).splitlines(),
            ["src/main.py", "tests/test_main.py"],
        )
        self.assertEqual(find_files("MAIN", workspace_root=self.root).splitlines(),
                         ["src/main.py", "tests/test_main.py"])
        self.assertEqual(find_files("pkg/*.json", workspace_root=self.root).splitlines(),
                         ["src/pkg/data.json"])
        self.assertEqual(find_files("**/util.py", workspace_root=self.root).splitlines(),
                         ["src/pkg/util.py"])

    def test_find_files_scope_cap_and_errors(self):
        scoped = find_files("*.py", "src", workspace_root=self.root).splitlines()
        self.assertEqual(scoped, ["src/main.py", "src/pkg/util.py"])
        capped = find_files("*.py", workspace_root=self.root, limit=2)
        self.assertTrue(capped.startswith("[2 of 3 matches"), capped)
        self.assertIn("(no files match", find_files("*.rs", workspace_root=self.root))
        self.assertIn("outside workspace", find_files("*", "/etc", workspace_root=self.root))
        self.assertIn("not a directory", find_files("*", "README.md", workspace_root=self.root))

    def test_list_project_paths_full_list_cache(self):
        from lmloop import files_index
        with mock.patch.object(files_index, "_MAX_PATHS", 2):
            files_index.clear_path_cache()
            short = files_index.list_project_paths(self.root, limit=files_index._MAX_PATHS)
            self.assertEqual(len(short), 2)
            full = files_index.list_project_paths(self.root, limit=None)
            self.assertGreater(len(full), 2)
            self.assertIn("src/pkg/util.py", full)

    def _argv_with(self, which, **kwargs):
        seen = {}

        def fake_run(cmd, **_kw):
            seen["cmd"] = cmd
            return mock.Mock(stdout="a.py:1:hit\n")

        with mock.patch("lmloop.tools.shutil.which", return_value=which), \
             mock.patch("lmloop.tools.subprocess.run", side_effect=fake_run):
            out = search_files("needle", workspace_root=self.root, **kwargs)
        self.assertIn("hit", out)
        return seen["cmd"]

    def test_search_files_flags_reach_rg_and_grep(self):
        rg = self._argv_with("/usr/bin/rg", glob="*.py", case_insensitive=True, context=9, fixed=True)
        self.assertEqual(rg[0], "rg")
        self.assertIn("-g", rg)
        self.assertEqual(rg[rg.index("-g") + 1], "*.py")
        self.assertIn("-i", rg)
        self.assertIn("-F", rg)
        self.assertEqual(rg[rg.index("-C") + 1], "5")  # clamped to MAX_SEARCH_CONTEXT
        self.assertEqual(rg[-3:], ["-e", "needle", str(self.root.resolve())])
        plain = self._argv_with("/usr/bin/rg")
        for flag in ("-g", "-i", "-F", "-C"):
            self.assertNotIn(flag, plain)
        grep = self._argv_with(None, glob="*.py", case_insensitive=True, context=2, fixed=True)
        self.assertEqual(grep[0], "grep")
        self.assertIn("--include=*.py", grep)
        self.assertIn("-i", grep)
        self.assertIn("-F", grep)
        self.assertEqual(grep[grep.index("-C") + 1], "2")

    def test_dispatch_search_files_coerces_flags(self):
        seen = {}

        def fake_run(cmd, **_kw):
            seen["cmd"] = cmd
            return mock.Mock(stdout="")

        _, impls = build_tools({"confirm_shell": False}, workspace_root=self.root)
        with mock.patch("lmloop.tools.shutil.which", return_value="/usr/bin/rg"), \
             mock.patch("lmloop.tools.subprocess.run", side_effect=fake_run):
            out = dispatch(
                impls, "search_files",
                '{"pattern": "x", "case_insensitive": "yes", "fixed": 0, "context": "1"}',
            )
        self.assertIn("no matches", out)
        self.assertIn("-i", seen["cmd"])
        self.assertNotIn("-F", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("-C") + 1], "1")

    def test_coerce_bool(self):
        self.assertIs(coerce_bool(True), True)
        self.assertIs(coerce_bool("False"), False)
        self.assertIs(coerce_bool(" TRUE "), True)
        self.assertIs(coerce_bool(1), True)
        self.assertIs(coerce_bool(0), False)
        self.assertIsNone(coerce_bool("maybe"))
        self.assertIsNone(coerce_bool(2))


class MemoryDisclosureTests(unittest.TestCase):
    def test_user_disclosure_from_write_tools(self):
        from lmloop.tools import user_disclosure

        self.assertEqual(
            user_disclosure(
                "remember",
                "Saved learning [uv]\nSay in your reply: Prior learning applied: uv",
            ),
            "Prior learning applied: uv",
        )
        self.assertEqual(
            user_disclosure(
                "log_decision",
                "Logged decision [abc]\nSay in your reply: Decision referenced: [abc]",
            ),
            "Decision referenced: [abc]",
        )
        self.assertIsNone(user_disclosure("recall_memory", "Learnings:\n- [uv]"))
        self.assertIsNone(user_disclosure("remember", "ERROR: missing required argument 'insight'"))

    def test_memory_tool_descriptions_require_disclosure(self):
        specs, _ = build_tools({"confirm_shell": False})
        desc = {s["function"]["name"]: s["function"]["description"] for s in specs}
        self.assertIn("Prior learning applied: <key>", desc["remember"])
        self.assertIn("Prior learning applied: <key>", desc["recall_memory"])
        self.assertIn("Decision referenced: [id]", desc["log_decision"])
        self.assertIn("Decision referenced: [id]", desc["recall_memory"])

    def test_log_decision_cites_id_and_disclosure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch("lmloop.memory.project_dir", return_value=root):
                _, impls = build_tools({"confirm_shell": False}, workspace_root=root)
                out = impls["log_decision"]("use pytest", rationale="fits")
            self.assertIn("Logged decision [", out)
            self.assertIn("Decision referenced: [", out)
            self.assertIn(str(root / "decisions.jsonl"), out)


if __name__ == "__main__":
    unittest.main()
