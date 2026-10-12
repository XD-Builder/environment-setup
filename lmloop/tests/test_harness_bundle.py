"""Harness bundle fingerprint (H1)."""

import unittest

from lmloop import harness_bundle


class HarnessBundleTests(unittest.TestCase):
    def test_stable_id_for_same_config(self):
        cfg = {"until_max_steps": 12, "autonomous_gates": "files"}
        first = harness_bundle.bundle_id(cfg)
        second = harness_bundle.bundle_id(cfg)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_graph_name_changes_id(self):
        cfg = {}
        plain = harness_bundle.bundle_id(cfg)
        named = harness_bundle.bundle_id(cfg, graph_name="company")
        self.assertNotEqual(plain, named)

    def test_check_fingerprint_differs_by_cmd(self):
        empty = harness_bundle.check_fingerprint([])
        with_cmd = harness_bundle.check_fingerprint([{"cmd": "pytest -q"}])
        self.assertNotEqual(empty, with_cmd)

    def test_attach_meta_adds_field(self):
        meta: dict = {}
        harness_bundle.attach_harness_meta(meta, {})
        self.assertIn("harness_bundle_id", meta)
        self.assertEqual(len(meta["harness_bundle_id"]), 64)


if __name__ == "__main__":
    unittest.main()
