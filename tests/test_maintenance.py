#!/usr/bin/env python3
"""Keeping only the package and the vault, and living with people and Obsidian editing the vault."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
from governance import merge_ledger, render_ledger  # noqa: E402
from publish_vault import normalized_hash  # noqa: E402


class MaintenanceTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-999")
        self.root_vaults = self.root / "vaults"
        self.vault = self.root_vaults / "P-999_测试知识库"

    def make_vault(self) -> None:
        args = ["--vault-root", str(self.root_vaults), "--project-id", "P-999", "--name", "测试", "--website",
                "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm"]
        self.assertEqual(run("kb_project.py", *args).returncode, 0)

    def publish(self):
        return run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault))

    def snapshot(self, out: Path) -> dict[str, str]:
        folders = [out / "03_净化md", out / "04_知识库构建/vault"]
        return {str(p.relative_to(out)): p.read_text(encoding="utf-8") for f in folders for p in f.rglob("*.md")}

    def test_deleted_run_folder_is_rebuilt_from_package_and_vault(self) -> None:
        self.assertEqual(self.pipeline().returncode, 0)
        decisions = {"allow_terms": ["Bigbuyer Oy"]}
        (self.build / "review-decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        before = self.snapshot(self.out)
        shutil.rmtree(self.out)
        # The fixture package holds no real files, so its extracted text stands in for them.
        result = run("run_pipeline.py", "--vault", str(self.vault), "--out", str(self.out), "--text-dir", str(self.text),
                     "--images", "none")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = self.snapshot(self.out)
        self.assertEqual(sorted(before), sorted(after))
        for name in before:
            self.assertEqual(before[name], after[name], name)

    def test_obsidian_table_formatting_does_not_stop_the_next_publish(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        register = self.vault / "02_源资料/源资料与可检索性台账.md"
        padded = "\n".join(line.replace(" | ", "   |   ") for line in register.read_text(encoding="utf-8").splitlines())
        register.write_text(padded + "\n\n", encoding="utf-8")
        self.pipeline()
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_what_people_write_in_a_ledger_survives_the_next_build(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        ledger = next(self.vault.rglob("01_待客户补充事项.md"))
        lines = ledger.read_text(encoding="utf-8").splitlines()
        row = next(i for i, line in enumerate(lines) if line.startswith("| P-999-CUS-"))
        cells = lines[row].split("|")
        record = cells[1].strip()
        cells[4] = " 已发客户，等回复 "  # 当前阶段
        lines[row] = "|".join(cells)
        at = next(i for i, line in enumerate(lines) if line.startswith(f"### {record}｜"))
        lines[at + 1:at + 1] = ["", "#### 处理记录", "", "- 11-04 已微信发给客户"]
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.pipeline()
        self.assertEqual(self.publish().returncode, 0)
        merged = ledger.read_text(encoding="utf-8")
        self.assertIn("已发客户，等回复", merged)
        self.assertIn("11-04 已微信发给客户", merged)

    def test_entry_the_machine_no_longer_sees_is_kept_for_a_person_to_close(self) -> None:
        old = render_ledger("ANM", "台账", "用途", "P-999", "2026-11-01", [
            {"id": "P-999-ANM-001", "row": ["旧问题", "1 处", "处理中", "专员"], "details": ["- 旧详情"], "updated": "2026-11-01"}])
        new = render_ledger("ANM", "台账", "用途", "P-999", "2026-11-02", [
            {"id": "P-999-ANM-002", "row": ["新问题", "2 处", "待判断", "专员"], "details": ["- 新详情"], "updated": "2026-11-02"}])
        merged = merge_ledger(old, new)
        self.assertIn("P-999-ANM-001", merged)
        self.assertIn("系统已解决，待确认", merged)
        self.assertIn("P-999-ANM-002", merged)

    def test_customer_names_in_file_names_are_masked_and_the_file_can_be_found(self) -> None:
        self.pipeline("--tiers", "T1,T2,T3", "--skip-t3-gate")
        page = next(self.clean.rglob("报价单*.md")).read_text(encoding="utf-8")
        # A quotation's name is the customer's name: the vault has the new name only.
        self.assertNotIn("Acme Fashion", page)
        self.assertNotIn("Acme Fashion", "\n".join(p.read_text(encoding="utf-8") for p in (self.build / "vault").rglob("*.md")))
        found = json.loads(run("find_source.py", "--package", str(self.package), "--id", "报价单-b1b1b1b1").stdout)
        self.assertEqual(found["original_name"], "Acme Fashion报价单.xlsx")
        # The client gets their own file names back, to recognise what they are asked about.
        table = (self.build / "客户确认表.csv").read_text(encoding="utf-8-sig")
        self.assertIn("原文件名", table)

    def test_project_page_points_at_the_package_not_the_run_folder(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        info = (self.vault / "01_工作台/10_项目基础信息.md").read_text(encoding="utf-8")
        self.assertIn(f"客户原始资料路径：{self.package.resolve()}", info)
        self.assertNotIn("03_净化md", info.split("## 说明")[0])
        self.assertIn("净化原文", info)

    def test_mineru_text_and_the_state_are_kept_in_the_vault(self) -> None:
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        (ocr / "a1.json").write_text(json.dumps({"item_id": "a1", "source_hash": "a" * 64, "extracted_at": "2026-11-03",
                                                 "units": [{"locator": "page 1 ocr", "text": "SHERPA HOODIE NTX-777"}]}), encoding="utf-8")
        self.pipeline()
        self.make_vault()
        self.publish()
        self.assertTrue((self.vault / "02_源资料/20_MinerU按需提取/a1.json").is_file())
        self.assertTrue(list((self.vault / "02_源资料/20_MinerU按需提取").glob("MinerU-*.md")))
        self.assertTrue((self.vault / "05_数据与审核/70_构建状态/kb-config.json").is_file())
        self.assertTrue((self.vault / "05_数据与审核/70_构建状态/governance-ids.json").is_file())

    def test_pictures_stay_and_coverage_is_written(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        picture = self.vault / "03_正式知识/90_素材/img-old.png"
        picture.parent.mkdir(parents=True, exist_ok=True)
        picture.write_bytes(b"png")
        receipt_path = self.vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["files"]["03_正式知识/90_素材/img-old.png"] = "x"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.pipeline()
        self.publish()
        self.assertTrue(picture.is_file())
        self.assertTrue((self.vault / "03_正式知识/90_素材/00_素材索引.md").is_file())
        self.assertTrue((self.vault / "05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md").is_file())

    def test_navigation_refresh_updates_receipt_after_timestamp_only_status_change(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        overview = self.vault / "01_工作台/00_知识库总览.md"
        old = overview.read_text(encoding="utf-8")
        overview.write_text(old.replace("2026-", "2000-", 1), encoding="utf-8")
        result = run("refresh_navigation.py", "--out", str(self.out), "--vault", str(self.vault), "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt_path = self.vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(normalized_hash(overview), receipt["files"]["01_工作台/00_知识库总览.md"])
        self.assertEqual(self.publish().returncode, 0)

    def test_founding_year_ignores_collection_years(self) -> None:
        self.write_units("a1", [{"locator": "page 1", "text": "SINCE 2026 NEW ARRIVER"},
                                {"locator": "page 2", "text": "Established in 2004, our own factory."}])
        self.pipeline()
        cus = next((self.build / "vault").rglob("01_待客户补充事项.md")).read_text(encoding="utf-8")
        self.assertNotIn("成立年份以哪个为准", cus)

    def test_excel_escapes_are_decoded(self) -> None:
        self.write_units("b1", [{"locator": "sheet 工作表 (visible)",
                                 "text": "A6=ITEM NO; B6=PRODUCT; C6=FABRIC\nA7=AB-900; B7=Sherpa jacket; C7=OUTER:_x001f_ 150GSM SHERPA"}])
        self.pipeline("--tiers", "T1,T2,T3", "--skip-t3-gate")
        page = next(self.clean.rglob("产品表*.md")).read_text(encoding="utf-8")
        self.assertNotIn("_x001f_", page)
        self.assertIn("150GSM SHERPA", page)

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
