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


class SampleFirstTests(Base):
    """On a first build only one file of each kind is extracted; the rest wait for a confirmation."""

    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-999", sample_review=True)
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        second = {**manifest["assets"][0], "item_id": "a3", "display_name": "Winter.pdf", "path": "A/Winter.pdf",
                  "source_hash": "3" * 64, "byte_size": 10}
        manifest["assets"].append(second)
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (self.text / "a3.json").write_text(json.dumps({"item_id": "a3", "units": [
            {"locator": "page 1", "text": "WINTER SERIES\nNTX W-001 FLEECE VEST"}]}), encoding="utf-8")

    def catalogs(self) -> list[str]:
        return sorted(p.name for p in self.clean.rglob("产品画册-*.md"))

    def test_one_sample_per_kind_first_then_the_batch_after_confirmation(self) -> None:
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual(len(self.catalogs()), 1)  # One of the two catalogs, not both.
        routes = json.loads((self.build / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(r["reason"] == "awaiting_sample" for r in routes.values()), 1)
        plan = (self.build / "vault/02_源资料/处理计划与样例.md").read_text(encoding="utf-8")
        self.assertIn("| 产品画册 PDF | 2 |", plan)
        self.assertIn("确认后提取其余 1 份", plan)
        self.assertIn("PyMuPDF 按版面分栏读文字", plan)  # How the kind is extracted, said beside its sample.
        self.assertNotIn("等样例确认后提取", plan.split("## 不处理的资料")[1])
        (self.build / "sample-decisions.json").write_text(json.dumps({"catalog_pdf": {"status": "已确认"}}), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual(self.catalogs(), ["产品画册-Catalog.md", "产品画册-Winter.md"])
        plan = (self.build / "vault/02_源资料/处理计划与样例.md").read_text(encoding="utf-8")
        self.assertNotIn("确认后提取", plan.split("## 特殊结构")[0].split("## 不处理的资料")[0].split("| 产品画册 PDF |")[1].split("\n")[0])

    def test_text_extraction_reads_only_the_sample_of_an_unconfirmed_kind(self) -> None:
        import hashlib
        package = self.root / "small"
        (package / "审计记录").mkdir(parents=True)
        (package / "A").mkdir()
        assets = []
        for item_id, size in (("t1", 10), ("t2", 20), ("t3", 30)):
            data = (item_id * size).encode()
            (package / "A" / f"{item_id}.txt").write_bytes(data)
            assets.append({"item_id": item_id, "display_name": f"{item_id}.txt", "decision": "A", "resource_type": "marketing",
                           "asset_kind": "document", "source_hash": hashlib.sha256(data).hexdigest(), "path": f"A/{item_id}.txt",
                           "copy_status": "copied", "usable_as_fact": True, "byte_size": len(data)})
        (package / "审计记录/package-manifest.json").write_text(json.dumps({"assets": assets}), encoding="utf-8")
        out = self.root / "small-run"
        self.assertEqual(run("sample_gate.py", "--package", str(package), "--out", str(out), "--config", str(self.config)).returncode, 0)
        self.assertEqual(run("extract_package_text.py", "--package", str(package), "--out", str(out)).returncode, 0)
        read = sorted(p.stem for p in (out / "04_知识库构建/package-text").glob("t*.json"))
        self.assertEqual(read, ["t2"])  # The file of middling size is the sample.

    def test_a_method_written_in_the_plan_page_is_read_back_with_the_confirmation(self) -> None:
        from publish_vault import merge_plan
        new = ("| 类型 | 份数 | 在初筛包哪里 | 怎么提取 | 样例 | 样例情况 | 要做的事 | 处理方式 | 确认状态 |\n"
               "| 产品画册 PDF | 2 | A | 读 | [[x]] | 建议：MinerU | — | 文字层（默认） | 待确认 |\n"
               "| 其他 PDF（公司简介、规格书等） | 3 | A | 读 | [[y]] | 建议：文字层够用 | — | 文字层（默认） | 待确认 |\n")
        current = new.replace("| 文字层（默认） | 待确认 |\n| 其他", "| MinerU+复核 | 已确认 |\n| 其他", 1)
        merged, answers = merge_plan(current, new)
        self.assertEqual(answers, {"catalog_pdf": {"status": "已确认", "method": "mineru"}})
        self.assertIn("| MinerU+复核 | 已确认 |", merged)
        self.assertIn("| 文字层（默认） | 待确认 |", merged)  # Untouched rows stay as generated.

    def test_a_quotation_confirmed_by_an_earlier_version_counts_for_spreadsheets(self) -> None:
        from samples import is_confirmed
        self.assertTrue(is_confirmed("spreadsheet", {"quotation": {"status": "已确认"}}, {}))
        self.assertFalse(is_confirmed("word", {"quotation": {"status": "已确认"}}, {}))


if __name__ == "__main__":
    unittest.main()
