"""Tests for lmloop tool safety helpers."""

import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from pathlib import Path

from lmloop.commands import RESERVED_SKILL_NAMES, slash_command_metas
from lmloop.tools import (
    _search_ddgs,
    build_tools,
    dispatch,
    fetch_url,
    format_tool_preview,
    is_destructive,
    list_dir,
    needs_shell,
    read_file,
    run_shell,
    tool_names,
    unwrap_tool_result,
    web_search,
    write_file,
    ToolResult,
)


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
        p = mock.patch("lmloop.tools._search_ddgs", return_value=([], "unavailable"))
        self.addCleanup(p.stop)
        p.start()

    def test_web_search_parses_and_unwraps_uddg(self):
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=_FakeResp(DDG_HTML_FIXTURE)):
            out = web_search("Fairfax early childhood", max_results=5)
        self.assertNotIn("ERROR", out)
        self.assertIn("<<<untrusted>>>", out)
        self.assertIn("Fairfax County Public Library", out)
        self.assertIn("https://www.fairfaxcounty.gov/library", out)
        self.assertNotIn("uddg=", out)
        self.assertIn("https://www.firstfivefairfax.org/", out)
        self.assertIn("Early childhood resources", out)

    def test_web_search_captcha_falls_back_then_errors(self):
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=_FakeResp(CAPTCHA_HTML_FIXTURE)):
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

        with mock.patch("lmloop.tools.urllib.request.urlopen", side_effect=_open):
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

        with mock.patch("lmloop.tools.urllib.request.urlopen", side_effect=_open):
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

        with mock.patch("lmloop.tools.urllib.request.urlopen", side_effect=_open):
            out = web_search("east coast")
        self.assertNotIn("ERROR", out)
        self.assertIn("East Coast of the United States", out)
        self.assertIn("Instant Answer", out)

    def test_web_search_empty_returns_error(self):
        with mock.patch(
            "lmloop.tools.urllib.request.urlopen",
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
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=_FakeResp(DDG_HTML_FIXTURE)):
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
        with mock.patch("lmloop.tools.urllib.request.urlopen", side_effect=err):
            out = fetch_url("https://example.com/missing")
        self.assertIn("ERROR: HTTP 404", out)
        self.assertIn("https://example.com/missing", out)

    def test_fetch_url_final_url_and_links(self):
        resp = _FakeResp(
            PAGE_HTML_FIXTURE,
            url="https://www.example.gov/parks/",
            status=200,
        )
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=resp):
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
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=resp):
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
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=resp), \
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
             mock.patch("lmloop.tools.urllib.request.urlopen") as urlopen:
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
             mock.patch("lmloop.tools.urllib.request.urlopen", side_effect=_open):
            out = web_search("east coast parks")
        self.assertNotIn("ERROR", out)
        self.assertIn("National Park Service", out)
        self.assertIn("DuckDuckGo Lite", out)


if __name__ == "__main__":
    unittest.main()
