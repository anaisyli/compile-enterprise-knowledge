#!/usr/bin/env python3
"""Settings kept in the vault and in a run folder are merged entry by entry, never replaced whole."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kb_state import STATE, restore, save  # noqa: E402


def write(path: Path, value: dict, age: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


class StateMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = Path(folder.name) / "run"
        self.vault = Path(folder.name) / "vault"
        self.run_config = self.run / "04_知识库构建" / "kb-config.json"
        self.vault_config = self.vault / STATE / "kb-config.json"

    def test_a_product_list_written_into_the_vault_survives_a_later_publish(self) -> None:
        # What happened on P-010: a person's product list went into the vault, then a publish from a
        # run folder whose config was edited later (but has no product list) replaced the whole file.
        write(self.vault_config, {"company": "视领", "products": [{"name": "DS50系列"}], "categories": {"a": "旧"}}, age=3600)
        write(self.run_config, {"company": "视领", "categories": {"a": "新"}, "sample_review": True}, age=60)
        report = save(self.run, self.vault)
        merged = json.loads(self.vault_config.read_text(encoding="utf-8"))
        self.assertEqual(merged["products"], [{"name": "DS50系列"}])  # Only the vault had it: kept.
        self.assertEqual(merged["categories"], {"a": "新"})  # Both had it: the newer file wins, and it is said.
        self.assertTrue(merged["sample_review"])
        self.assertTrue(any("categories" in line for line in report["kept_newer_in_vault"]))
        # The run folder now has the product list too, so the next build uses it.
        self.assertEqual(json.loads(self.run_config.read_text(encoding="utf-8"))["products"], [{"name": "DS50系列"}])

    def test_restore_merges_the_same_way(self) -> None:
        write(self.vault_config, {"site_sections": {"home": ["首页"]}}, age=60)
        write(self.run_config, {"company": "视领"}, age=3600)
        restore(self.run, self.vault)
        merged = json.loads(self.run_config.read_text(encoding="utf-8"))
        self.assertEqual(merged, {"company": "视领", "site_sections": {"home": ["首页"]}})


if __name__ == "__main__":
    unittest.main()
