#!/usr/bin/env python3
"""Opening originals from the vault, tables kept as tables, and confirming samples before the batch."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
from mineru_ocr import html_table  # noqa: E402
from page_check import laid_out  # noqa: E402


class TableTests(unittest.TestCase):
    def test_a_mineru_table_stays_a_table(self) -> None:
        text = html_table("<table><tr><th>款号</th><th>面料</th></tr><tr><td>NTX-1</td><td>100% COTTON</td></tr></table>")
        self.assertEqual(text, "| 款号 | 面料 |\n| --- | --- |\n| NTX-1 | 100% COTTON |")

    def test_a_flow_laid_out_from_the_page_is_kept_and_an_added_step_is_refused(self) -> None:
        page = "下单 打样 确认 大货生产 发货"
        self.assertEqual(laid_out("1. 下单\n2. 打样\n3. 确认\n4. 大货生产\n5. 发货", page)[0], "1. 下单\n2. 打样\n3. 确认\n4. 大货生产\n5. 发货")
        kept, refused = laid_out("1. 下单\n2. 质检\n3. 发货", page)
        self.assertEqual(kept, "")
        self.assertEqual(refused, ["质检"])


class SampleTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-999", sample_review=True)
        original = self.package / "A" / "Catalog.pdf"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"%PDF-1.4 catalog")
        self.vault = self.root / "vaults" / "P-999_测试知识库"

    def page(self) -> str:
        return next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")

    def test_the_original_opens_from_the_card_under_its_new_name(self) -> None:
        self.pipeline()
        link = next(part for part in self.page().split("(") if part.startswith("file:")).split(")")[0]
        alias = Path(unquote(urlparse(link).path).lstrip("/"))
        self.assertEqual(alias.name, "产品画册-Catalog.pdf")
        self.assertTrue(alias.samefile(self.package / "A" / "Catalog.pdf"))
        self.assertNotIn("A/Catalog.pdf", unquote(self.page()))  # The original path is not in the vault.

    def test_a_word_table_is_published_as_a_table(self) -> None:
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": [
            {"locator": "table at 3", "text": "工序 | 天数\n打样 | 7\n大货 | 30"}]}, ensure_ascii=False), encoding="utf-8")
        self.pipeline()
        self.assertIn("| 工序 | 天数 |\n| --- | --- |\n| 打样 | 7 |", self.page())

    def test_kinds_wait_for_their_sample_and_a_confirmation_in_the_vault_is_kept(self) -> None:
        self.pipeline()
        plan = (self.build / "vault/02_源资料/处理计划与样例.md").read_text(encoding="utf-8")
        self.assertIn("| 产品画册 PDF | 1 |", plan)
        self.assertIn("待确认", plan)
        report = json.loads((self.build / "校验结果.json").read_text(encoding="utf-8"))
        self.assertTrue(any("还没确认样例" in w for w in report["warnings"]))
        args = ["--vault-root", str(self.root / "vaults"), "--project-id", "P-999", "--name", "测试", "--website",
                "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm"]
        run("kb_project.py", *args)
        self.assertEqual(run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault)).returncode, 0)
        page = self.vault / "02_源资料/处理计划与样例.md"
        lines = page.read_text(encoding="utf-8").splitlines()
        lines = [line[: line.rstrip().rstrip("|").rstrip().rfind("|") + 1] + " 已确认 |" if line.startswith("| 产品画册 PDF |") else line
                 for line in lines]
        page.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.pipeline()
        self.assertEqual(run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault)).returncode, 0)
        decisions = json.loads((self.vault / "05_数据与审核/70_构建状态/sample-decisions.json").read_text(encoding="utf-8"))
        self.assertEqual(decisions["catalog_pdf"]["status"], "已确认")
        self.assertIn("| 产品画册 PDF |", page.read_text(encoding="utf-8"))
        self.assertIn("已确认", page.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
