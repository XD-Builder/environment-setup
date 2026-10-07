"""Smoke tests for shared unittest helpers."""

import unittest

from support import parametrize, requires_rich, rich_available


class ParametrizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ran: list[tuple[int, int]] = []

    @parametrize((1, 2), (3, 4), names=("a", "b"))
    def test_doubles(self, a: int, b: int) -> None:
        self.ran.append((a, b))
        self.assertEqual(a + 1, b)

    def test_all_cases_executed(self) -> None:
        self.test_doubles()
        self.assertEqual(self.ran, [(1, 2), (3, 4)])


class RequiresRichTests(unittest.TestCase):
    @requires_rich
    def test_rich_gate(self) -> None:
        self.assertTrue(rich_available())


if __name__ == "__main__":
    unittest.main()
