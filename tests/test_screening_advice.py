#!/usr/bin/env python3
"""Workbooks outside the product tables read as tables, and screening's cleanup marks treated as advice."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_extensions import Base  # noqa: E402


class ScreeningAdviceTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False), encoding="utf-8")

    def triage(self, *locators: str, codes: tuple[str, ...] = ("price_columns",)) -> None:
        record = {"items": [{"item_id": "b1", "actions": [{"locators": list(locators), "reason_codes": list(codes)}]}]}
        (self.package / "审计记录/triage-results.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def ledger(self, name: str) -> str:
        return next((self.build / "vault").rglob(name)).read_text(encoding="utf-8")

    def test_a_workbook_outside_the_product_tables_is_a_table_without_cell_addresses(self) -> None:
        self.write_units("a1", [{"locator": "sheet 某客户专用 (visible)", "text": "A1=面料; B1=克重\nA2=摇粒绒; B2=280GSM"},
                                {"locator": "sheet 隐藏 (hidden)", "text": "A1=内部; B1=不要"}])
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("| 面料 | 克重 |\n| --- | --- |\n| 摇粒绒 | 280GSM |", page)
        self.assertNotIn("A1=", page)
        self.assertNotIn("某客户专用", page)  # A sheet name can be a customer's.
        self.assertNotIn("内部", page)

    def test_a_product_column_screening_marked_stays_marked_and_prices_still_go(self) -> None:
        self.triage("工作表!C:C")  # The fabric column, marked as a price column by mistake.
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("报价单*.md")).read_text(encoding="utf-8")
        self.assertIn("100% POLYESTER 350GSM SHERPA", page)
        self.assertIn("初筛建议删除（价格列）", page)
        self.assertNotIn("15元/米", page)  # The row rules still remove a price, whatever screening said.
        self.assertIn("初筛建议删除、整理时判断保留的内容", self.ledger("01_源资料处理台账.md"))

    def test_a_place_screening_named_that_cannot_be_found_does_not_hold_the_file(self) -> None:
        self.triage("右下角那一块")
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertTrue(list(self.clean.rglob("报价单*.md")))
        self.assertIn("初筛写的清理位置找不到", self.ledger("01_源资料处理台账.md"))


if __name__ == "__main__":
    unittest.main()
