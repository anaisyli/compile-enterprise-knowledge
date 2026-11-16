#!/usr/bin/env python3
"""Settings kept in the vault and in a run folder are merged entry by entry, never replaced whole."""

from __future__ import annotations

import contextlib
import io
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
        self.base_config = self.run / "04_知识库构建" / "vault-base" / "kb-config.json"

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
        # Saving writes the vault only; the next build gets the product list when it restores.
        self.assertNotIn("products", json.loads(self.run_config.read_text(encoding="utf-8")))
        restore(self.run, self.vault)
        self.assertEqual(json.loads(self.run_config.read_text(encoding="utf-8"))["products"], [{"name": "DS50系列"}])

    def test_restore_merges_the_same_way(self) -> None:
        write(self.vault_config, {"site_sections": {"home": ["首页"]}}, age=60)
        write(self.run_config, {"company": "视领"}, age=3600)
        restore(self.run, self.vault)
        merged = json.loads(self.run_config.read_text(encoding="utf-8"))
        self.assertEqual(merged, {"company": "视领", "site_sections": {"home": ["首页"]}})
        # Restoring reads the published vault; it never writes it (the vault may be read-only).
        self.assertEqual(json.loads(self.vault_config.read_text(encoding="utf-8")), {"site_sections": {"home": ["首页"]}})

    def test_a_key_deleted_in_the_run_is_deleted_in_the_vault_on_publish(self) -> None:
        # With the vault-base record, "the run deleted it" is told apart from "only one side had it".
        write(self.base_config, {"company": "视领", "drop_me": 1}, age=7200)
        write(self.vault_config, {"company": "视领", "drop_me": 1, "vault_only": 2}, age=3600)
        write(self.run_config, {"company": "视领", "vault_only": 2}, age=60)  # drop_me deleted here
        report = save(self.run, self.vault)
        merged = json.loads(self.vault_config.read_text(encoding="utf-8"))
        self.assertNotIn("drop_me", merged)
        self.assertEqual(merged["vault_only"], 2)  # A key both sides kept is untouched.
        self.assertTrue(any("drop_me" in line for line in report["removed"]))
        # The base is the run folder's config as published.
        self.assertEqual(json.loads(self.base_config.read_text(encoding="utf-8")),
                         json.loads(self.run_config.read_text(encoding="utf-8")))

    def test_merge_vault_config_leaves_a_deleted_key_deleted(self) -> None:
        from run_pipeline import merge_vault_config
        write(self.base_config, {"company": "视领", "drop_me": 1}, age=7200)
        write(self.vault_config, {"company": "视领", "drop_me": 1, "products": [{"name": "DS50系列"}]}, age=3600)
        write(self.run_config, {"company": "视领"}, age=60)  # drop_me deleted; products not known yet
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            merge_vault_config(self.run_config, self.vault)  # --config is the build copy: base sits next to it
        merged = json.loads(self.run_config.read_text(encoding="utf-8"))
        self.assertNotIn("drop_me", merged)  # Same as the vault's last-published value: the run deleted it.
        self.assertEqual(merged["products"], [{"name": "DS50系列"}])  # A genuinely new vault key is still added.
        self.assertIn("drop_me", out.getvalue())
        self.assertIn("products", out.getvalue())

    def test_a_setting_taken_from_the_vault_can_be_deleted_before_the_next_publish(self) -> None:
        from run_pipeline import sync_config
        # An earlier publish's base lacks products; the person then wrote it into the vault copy.
        write(self.base_config, {"company": "视领"}, age=7200)
        write(self.vault_config, {"company": "视领", "products": [{"name": "DS50系列"}]}, age=3600)
        # First sync: restore + merge bring products into --config (the build copy here).
        sync_config(self.run, self.vault, self.run_config)
        self.assertEqual(json.loads(self.run_config.read_text(encoding="utf-8"))["products"],
                         [{"name": "DS50系列"}])
        # The person deletes it from --config before publishing.
        write(self.run_config, {"company": "视领"}, age=60)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sync_config(self.run, self.vault, self.run_config)
        self.assertNotIn("products", json.loads(self.run_config.read_text(encoding="utf-8")))
        self.assertIn("products", out.getvalue())  # Says the run deleted it, not that it is new.
        # And the next publish removes it from the vault too.
        save(self.run, self.vault)
        self.assertNotIn("products", json.loads(self.vault_config.read_text(encoding="utf-8")))

    def test_a_product_list_added_to_the_vault_after_the_base_still_arrives(self) -> None:
        # P-010 again, but now a base exists that predates the person's product list.
        from run_pipeline import sync_config
        write(self.base_config, {"company": "视领", "categories": {"a": "旧"}}, age=7200)
        write(self.vault_config, {"company": "视领", "categories": {"a": "旧"},
                                  "products": [{"name": "DS50系列"}]}, age=3600)
        write(self.run_config, {"company": "视领", "categories": {"a": "旧"}}, age=60)
        save(self.run, self.vault)
        self.assertEqual(json.loads(self.vault_config.read_text(encoding="utf-8"))["products"],
                         [{"name": "DS50系列"}])
        # --config and the run copy both get it through the same sync run_pipeline --vault does.
        config_path = self.run.parent / "kb-config.json"
        write(config_path, {"company": "视领", "categories": {"a": "旧"}}, age=30)
        sync_config(self.run, self.vault, config_path)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["products"],
                         [{"name": "DS50系列"}])
        self.assertEqual(json.loads(self.run_config.read_text(encoding="utf-8"))["products"],
                         [{"name": "DS50系列"}])

    def test_a_key_the_vault_deleted_is_deleted_from_the_run_on_restore(self) -> None:
        write(self.base_config, {"company": "视领", "drop_me": 1}, age=7200)
        write(self.vault_config, {"company": "视领"}, age=3600)  # deleted in the vault
        write(self.run_config, {"company": "视领", "drop_me": 1}, age=60)  # run still has the base value
        report = restore(self.run, self.vault)
        merged = json.loads(self.run_config.read_text(encoding="utf-8"))
        self.assertNotIn("drop_me", merged)
        self.assertTrue(any("drop_me" in line for line in report["removed"]))
        # The vault copy is not touched by a restore.
        self.assertEqual(json.loads(self.vault_config.read_text(encoding="utf-8")), {"company": "视领"})

    def test_a_key_changed_on_both_sides_uses_the_newer_file_and_says_so(self) -> None:
        write(self.base_config, {"company": "视领", "tier": "T1"}, age=7200)
        write(self.vault_config, {"company": "视领", "tier": "T2"}, age=3600)  # vault moved it
        write(self.run_config, {"company": "视领", "tier": "T3"}, age=60)  # run moved it differently, newer
        report = save(self.run, self.vault)
        self.assertEqual(json.loads(self.vault_config.read_text(encoding="utf-8"))["tier"], "T3")
        self.assertTrue(any("tier" in line for line in report["kept_newer_in_vault"]))
        self.assertFalse(report["removed"])

    def test_a_deleted_key_beats_a_change_only_when_it_is_the_newer_side(self) -> None:
        # Run deleted it, vault modified it: the newer file's choice wins.
        write(self.base_config, {"company": "视领", "drop_me": "old"}, age=7200)
        write(self.vault_config, {"company": "视领", "drop_me": "new"}, age=3600)
        write(self.run_config, {"company": "视领"}, age=60)  # run deleted it, and is newer
        report = save(self.run, self.vault)
        self.assertNotIn("drop_me", json.loads(self.vault_config.read_text(encoding="utf-8")))
        self.assertTrue(any("drop_me" in line for line in report["removed"]))
        # Vault modified it and is newer: its value stays.
        write(self.base_config, {"company": "视领", "drop_me": "old"}, age=7200)
        write(self.vault_config, {"company": "视领", "drop_me": "new"}, age=60)  # vault newer now
        write(self.run_config, {"company": "视领"}, age=3600)
        save(self.run, self.vault)
        self.assertEqual(json.loads(self.vault_config.read_text(encoding="utf-8"))["drop_me"], "new")

    def test_a_vault_won_conflict_is_recorded_so_a_later_vault_delete_still_propagates(self) -> None:
        # Base holds the old agreed value; run and vault both changed it, vault is newer.
        write(self.base_config, {"company": "视领", "tier": "old"}, age=7200)
        write(self.run_config, {"company": "视领", "tier": "run-val"}, age=3600)
        write(self.vault_config, {"company": "视领", "tier": "vault-val"}, age=60)
        restore(self.run, self.vault)
        self.assertEqual(json.loads(self.run_config.read_text(encoding="utf-8"))["tier"], "vault-val")
        # The adopted value becomes the agreed value in the base.
        self.assertEqual(json.loads(self.base_config.read_text(encoding="utf-8"))["tier"], "vault-val")
        # So when the vault deletes the key next, the run copy drops it too — not a conflict.
        write(self.vault_config, {"company": "视领"}, age=30)
        restore(self.run, self.vault)
        self.assertNotIn("tier", json.loads(self.run_config.read_text(encoding="utf-8")))

    def test_other_json_files_still_merge_by_union(self) -> None:
        # The three-way merge and the vault-base record are only for kb-config.json.
        write(self.base_config, {"company": "视领", "drop_me": 1}, age=7200)
        decisions_run = self.run / "04_知识库构建" / "review-decisions.json"
        decisions_vault = self.vault / STATE / "review-decisions.json"
        write(decisions_vault, {"a": 1, "b": 2}, age=3600)
        write(decisions_run, {"a": 1}, age=60)  # b absent — union keeps it anyway
        save(self.run, self.vault)
        self.assertEqual(json.loads(decisions_vault.read_text(encoding="utf-8")), {"a": 1, "b": 2})

    def test_site_view_update_config_does_not_resurrect_a_deleted_key(self) -> None:
        from site_view import update_config
        write(self.base_config, {"company": "视领", "drop_me": 1}, age=3600)
        write(self.vault_config, {"company": "视领", "drop_me": 1}, age=1800)
        write(self.run / "kb-config.json", {"company": "视领", "drop_me": 1}, age=7200)  # older run-root copy
        write(self.run_config, {"company": "视领"}, age=60)  # newer build copy: the key is deleted
        config = update_config(self.run, self.vault)
        self.assertNotIn("drop_me", config)
        self.assertEqual(config["company"], "视领")

    def test_site_view_update_config_falls_back_to_whichever_side_exists(self) -> None:
        from site_view import update_config
        write(self.vault_config, {"only_vault": 1}, age=60)
        self.assertEqual(update_config(self.run, self.vault)["only_vault"], 1)
        self.vault_config.unlink()
        write(self.run / "kb-config.json", {"only_run": 2}, age=60)
        write(self.run_config, {"only_run": 3}, age=30)  # The newer run copy wins as a whole file.
        self.assertEqual(update_config(self.run, self.vault)["only_run"], 3)


if __name__ == "__main__":
    unittest.main()
