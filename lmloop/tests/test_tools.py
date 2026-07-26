"""Tests for lmloop tool safety helpers."""

import os
import tempfile
import unittest

from lmloop.tools import (
    build_tools,
    dispatch,
    is_destructive,
    list_dir,
    needs_shell,
    read_file,
    write_file,
)


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

    def test_list_dir_hides_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            open(".secret", "w").close()
            open("visible.txt", "w").close()
            result = list_dir(".")
            self.assertIn("visible.txt", result)
            self.assertNotIn(".secret", result)

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


if __name__ == "__main__":
    unittest.main()
