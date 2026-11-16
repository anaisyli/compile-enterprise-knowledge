#!/usr/bin/env python3
"""The 80_建站资料 view is made only when switched on, and switching it off is said out loud."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
import run_pipeline  # noqa: E402
import site_view  # noqa: E402

SITE = "03_正式知识/80_建站资料"


class SiteSwitchTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-996")
        self.vault = self.root / "vaults" / "P-996_测试知识库"

    def built(self, *extra: str):
        result = self.pipeline(*extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def make_vault(self) -> None:
        made = run("kb_project.py", "--vault-root", str(self.root / "vaults"), "--project-id", "P-996", "--name", "测试",
                   "--website", "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)

    def publish(self, *extra: str):
        return run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault), *extra)

    def site_pages(self, root: Path) -> list[Path]:
        return list((root / SITE).rglob("*.md")) if (root / SITE).is_dir() else []

    def test_off_by_default_and_made_when_switched_on(self) -> None:
        self.built()
        self.assertEqual(self.site_pages(self.build / "vault"), [])
        self.edit_config(site_view={"enabled": True})
        self.built()
        self.assertTrue(self.site_pages(self.build / "vault"))

    def test_switching_off_archives_the_old_pages_and_says_so(self) -> None:
        self.edit_config(site_view={"enabled": True})
        self.built()
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        self.assertTrue(self.site_pages(self.vault))
        self.edit_config(site_view={"enabled": False})
        self.built()
        preview = self.publish("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertGreater(json.loads(preview.stdout)["site_view_archived"], 0)
        self.assertTrue(self.site_pages(self.vault))  # The preview changed nothing.
        published = self.publish()
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertEqual(self.site_pages(self.vault), [])
        self.assertIn("建站资料视图已关闭", published.stdout)
        notes = list((self.vault / "05_数据与审核/60_版本归档").rglob("00_版本说明.md"))
        self.assertTrue(any("## 建站资料视图已关闭" in n.read_text(encoding="utf-8") for n in notes))

    def test_no_setting_with_an_old_view_stops_the_publish(self) -> None:
        self.edit_config(site_view={"enabled": True})
        self.built()
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.pop("site_view")
        self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.built()  # Without --vault nothing restores the setting.
        stopped = self.publish()
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("没写 site_view", stopped.stdout + stopped.stderr)
        self.assertTrue(self.site_pages(self.vault))

    def test_the_overview_links_the_view_only_when_it_is_made(self) -> None:
        self.built()
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        overview = (self.vault / "01_工作台/00_知识库总览.md").read_text(encoding="utf-8")
        self.assertNotIn("80_建站资料/00_建站资料总览", overview)
        self.assertIn("建站资料视图没有打开", overview)


class SettingWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_a_vault_that_has_the_view_keeps_it_open(self) -> None:
        vault, config = self.root / "vault", self.root / "kb-config.json"
        (vault / SITE).mkdir(parents=True)
        config.write_text(json.dumps({"project_id": "P-1"}), encoding="utf-8")
        run_pipeline.keep_site_view_open(config, vault)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["site_view"], {"enabled": True})
        config.write_text(json.dumps({"site_view": {"enabled": False}}), encoding="utf-8")
        run_pipeline.keep_site_view_open(config, vault)  # An explicit choice is never overridden.
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["site_view"], {"enabled": False})

    def test_a_new_project_stays_off(self) -> None:
        vault, config = self.root / "vault", self.root / "kb-config.json"
        vault.mkdir()
        config.write_text(json.dumps({"project_id": "P-1"}), encoding="utf-8")
        run_pipeline.keep_site_view_open(config, vault)
        self.assertNotIn("site_view", json.loads(config.read_text(encoding="utf-8")))

    def test_asking_for_the_view_switches_it_on_everywhere(self) -> None:
        out, vault = self.root / "run", self.root / "vault"
        copies = [out / "kb-config.json", out / "04_知识库构建" / "kb-config.json", vault / "05_数据与审核/70_构建状态/kb-config.json"]
        for path in copies:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"project_id": "P-1", "site_view": {"enabled": False}}), encoding="utf-8")
        self.assertEqual(len(site_view.switch_on(out, vault)), 3)
        for path in copies:
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["site_view"], {"enabled": True})
        self.assertEqual(site_view.switch_on(out, vault), [])


if __name__ == "__main__":
    unittest.main()
