#!/usr/bin/env python3
"""Worksheets laid out as people read them, sheet names kept, quote terms cleaned."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from extract_blocks import document_blocks, technical_workbook  # noqa: E402
from source_fidelity import merged_sheet_table  # noqa: E402
from guard_blocks import HARD_PATTERNS  # noqa: E402
from sheet_layout import excel_date, lay_out  # noqa: E402

QUOTE = "\n".join([
    "A1=Shenzhen Example Technology Co.,Ltd",
    "A3=Quotation",
    "B5=Company Name:; E5=Quoted date; F5=46148",
    "A7=NO.; B7=Category; C7=Model; D7=Pictures; E7=Specification; F7=Qty",
    "B8=Wall-Mounted Digital Signage; C8=WM-32N01; E8=Non touch\nBrightness 350nits",
    "C9=WM-43N01; E9=Non touch",
    "C10=WM-55N01; E10=Touch",
    "A12=Remarks:",
    "A13=1. One year warranty for free.",
])


class SheetLayoutTests(unittest.TestCase):
    def test_technical_grids_keep_headerless_data_and_shared_parameter_scope(self) -> None:
        unit = {"locator": "sheet 参数 (visible)",
                "text": "A1=型号; B1=SL55; C1=SL65\nA2=系统; B2=Android 16\nA4=21.5寸; B4=屏幕包装; C4=920*490*190mm",
                "merges": ["B2:C2"]}
        self.assertTrue(technical_workbook("尺寸重量表.xlsx", [unit]))
        block, = document_blocks("D", "x", [unit], preserve_grid=True)
        self.assertEqual(block["table_layout"]["rows"], [1, 2, 3, 4])
        self.assertEqual(block["table_layout"]["header_rows"], 0)
        rendered = merged_sheet_table(block["text"], block["table_layout"])
        self.assertIn('<td colspan="2">Android 16</td>', rendered)
        self.assertIn('<td>21.5寸</td>', rendered)
        self.assertNotIn('<th', rendered)
        self.assertIn('920*490*190mm', rendered)

    def test_technical_name_does_not_bypass_quote_cleaning(self) -> None:
        unit = {"locator": "sheet 产品 (visible)", "text": "A1=型号; B1=尺寸; C1=单价\nA2=DS50; B2=55寸; C2=1000"}
        self.assertFalse(technical_workbook("规格参数.xlsx", [unit]))

    def test_a_quote_template_reads_as_lines_a_table_and_lines(self) -> None:
        blocks = document_blocks("D", "x", [{"locator": "sheet 立式壁挂广告机 (visible)", "text": QUOTE, "merges": ["B8:B10"]}])
        self.assertEqual({b["group"] for b in blocks}, {"表 1 · 立式壁挂广告机"})  # The sheet's own name is the heading.
        above, table, below = (b["text"] for b in blocks)
        self.assertIn("Quotation", above)
        self.assertIn("Company Name:  Quoted date  2026-05-06", above)  # The Excel date number as the date.
        self.assertTrue(table.startswith("| Category | Model | Specification |"))  # The real header, empty columns out.
        self.assertIn("| Wall-Mounted Digital Signage | WM-43N01 |", table)  # The merged category on every row it covers.
        self.assertIn("Non touch<br>Brightness 350nits", table)  # Line breaks inside a cell stay.
        self.assertIn("原表还有这些列，下面没有内容：NO.、Pictures、Qty", below)
        self.assertIn("1. One year warranty for free.", below)

    def test_a_plain_list_stays_one_table(self) -> None:
        rows = {1: {"A": "简称", "B": "全称"}, 2: {"A": "WM", "B": "Wall Mount"}}
        self.assertIsNone(lay_out(rows))
        self.assertEqual(excel_date("46148"), "2026-05-06")
        self.assertIsNone(excel_date("2026"))

    def test_quote_terms_in_english_are_caught(self) -> None:
        def hit(text: str) -> bool:
            return any(p.search(text) for p in HARD_PATTERNS.values())
        self.assertTrue(hit("Payment term: Advance payment 20% from Proforma Invoice value, 80% before shipment"))
        self.assertTrue(hit("Price term: EXW SHENZHEN"))
        self.assertFalse(hit("| 出厂价 | EXW Price | 工厂交货条件下的价格 |"))
        self.assertFalse(hit("| FOB | Free On Board | 船上交货 |"))
        for quote in ("FOB NINGBO", "交货条件：FOB 宁波", "EXW Shenzhen", "| FOB | Ningbo |"):
            self.assertTrue(hit(quote), quote)
        self.assertTrue(hit("30% deposit, balance before shipment"))
        self.assertFalse(hit("One year warranty for free."))


class SheetNameTests(unittest.TestCase):
    def test_a_sheet_named_after_a_customer_gets_a_neutral_heading(self) -> None:
        from test_extensions import Base, run  # noqa: F401
        import tempfile
        from test_pipeline import build_package
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package, text, config = build_package(root)
            units = [{"locator": "sheet Bigbuyer Oy (visible)", "text": "A1=型号; B1=尺寸; C1=亮度\nA2=WM-32; B2=32寸; C2=350nits"},
                     {"locator": "sheet 会议一体机 (visible)", "text": "A1=型号; B1=尺寸; C1=亮度\nA2=SL55; B2=55寸; C2=400nits"}]
            (text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": units}, ensure_ascii=False), encoding="utf-8")
            out = root / "run"
            run("run_pipeline.py", "--package", str(package), "--text-dir", str(text), "--out", str(out), "--config", str(config),
                "--images", "none")
            blocks = [json.loads(line) for line in (out / "04_知识库构建/blocks.jsonl").read_text(encoding="utf-8").splitlines()]
            groups = {b["group"] for b in blocks if b["doc_id"].startswith("DOC-") and b.get("sheet_name")}
        self.assertIn("表 2 · 会议一体机", groups)
        self.assertIn("表 1", groups)  # “Bigbuyer Oy” is a customer the guard knows: no name in the heading.
        self.assertFalse(any("Bigbuyer" in g for g in groups))


if __name__ == "__main__":
    unittest.main()
