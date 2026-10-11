"""Shared memory and flow parsers used by the CLI and the REPL."""

import unittest

from lmloop.commands import (
    MEMORY_ARG_CHOICES,
    parse_memory_words,
    parse_positive_count,
)
from support import parametrize


class PositiveCountTests(unittest.TestCase):
    @parametrize(
        ("3", 3),
        ("0", None),
        ("foo", None),
        ("", None),
        ("3 extra", None),
        names=("token", "want"),
    )
    def test_parse_positive_count(self, token, want):
        self.assertEqual(parse_positive_count(token), want)


class MemoryRequestTests(unittest.TestCase):
    def _parse(self, words, *, default):
        return parse_memory_words(
            words,
            mine_default=default,
            mine_usage="usage: memory mine [N]",
            mine_detail="detail",
        )

    def test_known_verbs_match_on_cli_and_repl(self):
        for verb in MEMORY_ARG_CHOICES:
            if verb in ("mine", "graph", "list"):
                continue
            with self.subTest(verb=verb):
                cli = self._parse([verb, "q"], default=3)
                repl = self._parse([verb, "q"], default=None)
                self.assertEqual(cli.verb, repl.verb)
                self.assertEqual(cli.verb, verb)
                self.assertEqual(cli.query, repl.query)
                self.assertEqual(cli.query, "q")
                self.assertEqual(cli.error, "")

    def test_graph_is_kg_with_hint(self):
        req = self._parse(["graph"], default=3)
        self.assertEqual(req.verb, "kg")
        self.assertIn("memory kg", req.hint)

    def test_search_keeps_the_full_query(self):
        req = self._parse(["how", "tests", "work"], default=3)
        self.assertEqual(req.verb, "search")
        self.assertEqual(req.query, "how tests work")

    def test_mine_default_differs_by_surface(self):
        cli = self._parse(["mine"], default=3)
        repl = self._parse(["mine"], default=None)
        self.assertEqual(cli.mine_count, 3)
        self.assertIsNone(repl.mine_count)
        self.assertEqual(cli.error, "")

    def test_mine_rejects_the_same_bad_tokens(self):
        for words in (["mine", "foo"], ["mine", "0"], ["mine", "2", "extra"]):
            with self.subTest(words=words):
                cli = self._parse(words, default=3)
                repl = self._parse(words, default=None)
                self.assertEqual(cli.error, repl.error)
                self.assertTrue(cli.error)
                self.assertEqual(cli.detail, "detail")
                self.assertIsNone(cli.mine_count)

    def test_empty_is_list(self):
        self.assertEqual(self._parse([], default=3).verb, "list")

    def test_view_renderer_rejects_model_verbs(self):
        from lmloop.commands import MemoryRequest
        from lmloop.memory import render_memory_view

        with self.assertRaises(ValueError):
            render_memory_view({}, MemoryRequest(verb="mine"), None)


if __name__ == "__main__":
    unittest.main()
