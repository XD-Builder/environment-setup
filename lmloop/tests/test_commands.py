"""Shared memory and flow parsers used by the CLI and the REPL."""

import unittest

from lmloop.commands import (
    EVAL_FLAGS,
    HISTORY_DEFAULT_CLI,
    HISTORY_DEFAULT_REPL,
    MEMORY_ARG_CHOICES,
    count_usage,
    eval_usage,
    parse_count_words,
    parse_eval_words,
    parse_graph_words,
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


class CountRequestTests(unittest.TestCase):
    def test_empty_uses_the_surface_default(self):
        cli = parse_count_words([], default=HISTORY_DEFAULT_CLI, usage=count_usage("lmloop history"))
        repl = parse_count_words([], default=HISTORY_DEFAULT_REPL, usage=count_usage("/history"))
        self.assertEqual(cli.count, HISTORY_DEFAULT_CLI)
        self.assertEqual(repl.count, HISTORY_DEFAULT_REPL)
        self.assertEqual(cli.error, "")

    def test_positive_count_matches_on_both_surfaces(self):
        usage = count_usage("/history")
        for token in ("1", "4"):
            with self.subTest(token=token):
                cli = parse_count_words([token], default=HISTORY_DEFAULT_CLI, usage=usage)
                repl = parse_count_words([token], default=HISTORY_DEFAULT_REPL, usage=usage)
                self.assertEqual(cli.count, repl.count)
                self.assertEqual(cli.count, int(token))
                self.assertEqual(cli.error, "")

    def test_bad_tokens_share_one_error(self):
        usage = count_usage("/checkpoints")
        for words in (["0"], ["foo"], ["2", "extra"], ["-3"]):
            with self.subTest(words=words):
                cli = parse_count_words(words, default=HISTORY_DEFAULT_CLI, usage=usage)
                repl = parse_count_words(words, default=HISTORY_DEFAULT_REPL, usage=usage)
                self.assertEqual(cli.error, repl.error)
                self.assertEqual(cli.error, usage)
                self.assertEqual(cli.count, 0)


class EvalRequestTests(unittest.TestCase):
    def test_flags_are_exclusive(self):
        self.assertEqual(parse_eval_words([]).mode, "text")
        self.assertEqual(parse_eval_words(["--json"]).mode, "json")
        self.assertEqual(parse_eval_words(["--design"]).mode, "design")
        self.assertEqual(parse_eval_words(["--design-doc"]).mode, "design")
        abstain = parse_eval_words(["--abstention"])
        self.assertEqual(abstain.mode, "abstention")
        self.assertFalse(abstain.as_json)
        both = parse_eval_words(["--json", "--abstention"])
        self.assertEqual(both.mode, "abstention")
        self.assertTrue(both.as_json)
        self.assertEqual(both.error, "")
        for words in (
            ["--json", "--design"],
            ["--design", "--design-doc"],
            ["--json", "--json"],
            ["--abstention", "--design"],
            ["--nope"],
        ):
            with self.subTest(words=words):
                req = parse_eval_words(words)
                self.assertEqual(req.error, eval_usage())
                self.assertEqual(req.mode, "text")
        gated = parse_eval_words(["--gate", "commit", "--json"])
        self.assertEqual(gated.mode, "gate")
        self.assertEqual(gated.gate, "commit")
        self.assertTrue(gated.as_json)
        self.assertEqual(parse_eval_words(["--drain"]).mode, "drain")
        self.assertEqual(parse_eval_words(["--inbox"]).mode, "inbox")
        for words in (
            ["--json", "--design"],
            ["--design", "--design-doc"],
            ["--json", "--json"],
            ["--abstention", "--design"],
            ["--gate", "commit", "--abstention"],
            ["--gate", "hourly"],
            ["--gate"],
            ["--drain", "--inbox"],
            ["--nope"],
        ):
            with self.subTest(words=words):
                req = parse_eval_words(words)
                self.assertEqual(req.error, eval_usage())
                self.assertEqual(req.mode, "text")
        self.assertEqual(
            EVAL_FLAGS,
            ("--json", "--design", "--design-doc", "--abstention", "--gate", "--drain", "--inbox"),
        )


class GraphRequestTests(unittest.TestCase):
    def _parse(self, words):
        return parse_graph_words(
            words,
            run_usage="usage: graph <name>",
            propose_usage="usage: graph propose <name>",
        )

    def test_surfaces_agree(self):
        self.assertEqual(self._parse([]).action, "resume")
        run = self._parse(["company"])
        self.assertEqual((run.action, run.name, run.error), ("run", "company", ""))
        proposed = self._parse(["propose", "demo"])
        self.assertEqual((proposed.action, proposed.name), ("propose", "demo"))

    def test_extra_tokens_error(self):
        self.assertEqual(self._parse(["company", "extra"]).error, "usage: graph <name>")
        self.assertEqual(self._parse(["propose"]).error, "usage: graph propose <name>")
        self.assertEqual(self._parse(["propose", "demo", "extra"]).error, "usage: graph propose <name>")


if __name__ == "__main__":
    unittest.main()
