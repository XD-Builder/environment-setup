"""Tests for lmloop tool safety helpers."""

import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from lmloop.commands import RESERVED_SKILL_NAMES, slash_command_metas
from lmloop.tools import (
    build_tools,
    dispatch,
    fetch_url,
    is_destructive,
    list_dir,
    needs_shell,
    read_file,
    tool_names,
    web_search,
    write_file,
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

CAPTCHA_HTML_FIXTURE = """
<html><body>
<form class="anomaly-modal__form" action="/challenge">
  Please complete the captcha to continue.
</form>
</body></html>
"""

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
        self.assertFalse(is_destructive("echo hello"))

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

    def test_read_file_inside_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            path = os.path.join(d, "hello.txt")
            with open(path, "w") as f:
                f.write("line1\nline2\n")
            result = read_file("hello.txt")
            self.assertIn("line1", result)
            self.assertNotIn("ERROR", result)

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

    def test_web_search_captcha_returns_error(self):
        with mock.patch("lmloop.tools.urllib.request.urlopen", return_value=_FakeResp(CAPTCHA_HTML_FIXTURE)):
            out = web_search("test query")
        self.assertIn("ERROR:", out)
        self.assertIn("CAPTCHA", out)

    def test_web_search_empty_returns_error(self):
        with mock.patch(
            "lmloop.tools.urllib.request.urlopen",
            return_value=_FakeResp("<html><body><p>no results</p></body></html>"),
        ):
            out = web_search("zzzzunlikelyquery")
        self.assertIn("ERROR:", out)
        self.assertIn("no search results", out)

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

    def test_tool_registry_is_single_source(self):
        specs, impls = build_tools({"confirm_shell": False})
        names = [s["function"]["name"] for s in specs]
        self.assertEqual(names, list(impls.keys()))
        self.assertEqual(set(names), set(tool_names()))
        for s in specs:
            self.assertIsInstance(s["function"]["name"], str)
            self.assertTrue(s["function"]["parameters"]["properties"] or True)

    def test_command_table_reserves_slash_stems(self):
        slash = {c.name for c in slash_command_metas() if c.reserve_skill}
        self.assertTrue(slash <= RESERVED_SKILL_NAMES)
        self.assertIn("system", RESERVED_SKILL_NAMES)
        self.assertIn("continue", RESERVED_SKILL_NAMES)
        # Packaged skills may still be overridden by users.
        self.assertNotIn("compact", RESERVED_SKILL_NAMES)

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


if __name__ == "__main__":
    unittest.main()
