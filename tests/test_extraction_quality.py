#!/usr/bin/env python3
"""Reading files the way they are laid out, quotations, pictures, and saying how far each file got."""

from __future__ import annotations

import json
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
from extract_package_text import pptx_units  # noqa: E402
from layout import ordered_text  # noqa: E402


def box(x0: float, y0: float, x1: float, text: str) -> dict:
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y0 + 15, "text": text}


class LayoutTests(unittest.TestCase):
    def test_documents_are_filed_by_what_they_are_and_samples_keep_their_kind(self) -> None:
        from extract_blocks import is_catalog, package_title

        def title(name: str, tier: str = "T2", path: str = "") -> str:
            return package_title({"item_id": name, "display_name": name, "path": path or f"A/{name}", "source_hash": "0" * 64},
                                 tier, {})[0]

        self.assertEqual(title("98寸DS50系列数字标牌规格书.pdf", "T1"), "规格书-98寸DS50系列数字标牌规格书")
        self.assertEqual(title("DS50—大尺寸广告机-中文版.pptx", "T1"), "产品画册-DS50—大尺寸广告机-中文版")  # 大尺寸 is not a size sheet.
        self.assertEqual(title("双面屏吊装安装说明书.docx"), "说明书-双面屏吊装安装说明书")
        self.assertEqual(title("Healthcare Solution.txt"), "解决方案-Healthcare Solution")
        self.assertEqual(title("外设考试资料.docx", path="B/产品/培训资料/外设考试资料.docx"), "内部资料-外设考试资料")
        self.assertEqual(title("自助终端常见问题.docx", path="B/产品/培训资料/自助终端常见问题.docx"), "内部资料-自助终端常见问题")
        self.assertEqual(title("SLINVUE-品牌故事.docx"), "公司介绍-SLINVUE-品牌故事")
        self.assertEqual(title("页面文本.txt", path="A/视领网页图片/Touch Table-Smartline/页面文本.txt"), "官网页面-Touch Table-Smartline")
        self.assertEqual(title("43寸双面吊挂广告机-3568D.docx", path="D/产品/广告机/双面吊挂/43寸双面吊挂广告机-3568D.docx"),
                         "产品资料-43寸双面吊挂广告机-3568D")
        self.assertEqual(title("Quotation 2026.xlsx"), "其他-Quotation 2026")
        # A T1 spec sheet leaves the catalog folder but stays a catalog sample, as confirmed before.
        spec = {"item_id": "s", "display_name": "98寸DS50系列数字标牌规格书.pdf", "path": "A/x.pdf"}
        self.assertTrue(is_catalog(spec, "T1", {}))
        self.assertFalse(is_catalog({**spec, "display_name": "公司简介.pdf"}, "T1", {}))

    def test_a_code_stays_with_the_column_it_sits_above(self) -> None:
        # Three products side by side; the catalog prints a code above the third one only.
        boxes = [box(47, 401, 160, "SHERPA JACKET"), box(688, 411, 760, "NTX S-002"), box(45, 437, 110, "FEATURES"),
                 box(38, 470, 250, "• HIGH STAND COLLAR"), box(322, 469, 540, "• 2 SIDE HAND POCKETS"), box(650, 470, 860, "• FULL ZIP")]
        parts = ordered_text(boxes, 960)
        self.assertEqual(parts[-1], "NTX S-002\n• FULL ZIP")
        self.assertNotIn("NTX S-002", parts[0])

    def test_a_wide_name_with_a_code_does_not_merge_two_products(self) -> None:
        boxes = [box(310, 404, 442, "SHERPA JACKET"), box(762, 412, 859, "NTX S-005"),
                 box(311, 474, 515, "• FULL ZIP"), box(686, 471, 890, "• SNAP BUTTON CLOSURE")]
        parts = ordered_text(boxes, 960)
        self.assertEqual(parts, ["SHERPA JACKET\n• FULL ZIP", "NTX S-005\n• SNAP BUTTON CLOSURE"])


SLIDE = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"""
SHAPE = """<p:sp><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="400000"/></a:xfrm></p:spPr>
<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"""


class SlideTests(unittest.TestCase):
    def test_slides_read_by_position_and_picture_slides_are_listed(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deck.pptx"
            right = SHAPE.format(x=6000000, y=1000000, cx=2000000, text="NTX-200 FLEECE VEST")
            left = SHAPE.format(x=500000, y=1000000, cx=2000000, text="NTX-100 SHERPA JACKET")
            picture = '<p:pic><p:nvPicPr><p:cNvPr id="2" name="p"/></p:nvPicPr></p:pic>'
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                                 '<p:sldSz cx="9144000" cy="5143500"/></p:presentation>')
                archive.writestr("ppt/slides/slide1.xml", SLIDE.format(shapes=right + left))
                archive.writestr("ppt/slides/slide2.xml", SLIDE.format(shapes=picture))
            units, facts = pptx_units(path)
        self.assertEqual([u["locator"] for u in units], ["slide 1", "slide 1 col 2"])
        self.assertIn("NTX-100", units[0]["text"])
        self.assertEqual(facts["ocr_pages"], [2])


class OfficeTests(unittest.TestCase):
    def test_a_converted_copy_is_reused_without_starting_libreoffice(self) -> None:
        import tempfile
        from office import convert_office
        with tempfile.TemporaryDirectory() as folder:
            done = Path(folder) / "k1.xlsx"
            done.write_bytes(b"converted earlier")
            self.assertEqual(convert_office(Path(folder) / "missing.xls", "xlsx", Path(folder), "k1", 5), done)

    def test_a_real_conversion_survives_libreoffice_still_holding_its_profile(self) -> None:
        from office import find_converter
        if not find_converter():
            self.skipTest("LibreOffice is not installed")
        result = run("check_tools.py")
        self.assertTrue(json.loads(result.stdout)["libreoffice"]["ok"], result.stdout)


class QualityTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False), encoding="utf-8")

    def test_quotation_line_numbers_are_not_style_codes_and_rows_are_summed_up(self) -> None:
        self.write_units("b1", [{"locator": "sheet 工作表 (visible)", "text":
                                 "B32=ITEM NO; E32=PRODUCT; I32=DESCRIPTION; L32=QUANTITY; O32=UNIT PRICE\n"
                                 "B36=1; I36=280gsm 全涤 单层摇粒绒夹克; L36=1000; O36=USD6.80\n"
                                 "B39=2; I39=cushion cover 45cm x 45cm; L39=100; O39=USD6.50"}])
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("报价单-*.md")).read_text(encoding="utf-8")
        self.assertNotIn("| 款号 |", page)
        self.assertIn("280gsm 全涤 单层摇粒绒夹克", page)
        summary = (self.clean / "产品表/00_产品表汇总.md").read_text(encoding="utf-8")
        self.assertIn("cushion cover 45cm x 45cm", summary)
        self.assertNotIn("USD6.80", summary)

    def test_one_mention_of_a_report_does_not_mark_a_whole_document(self) -> None:
        units = [{"locator": f"para {i}", "text": f"第 {i} 段：提花面料工艺说明。"} for i in range(1, 60)]
        units.insert(10, {"locator": "para 100", "text": "附 SGS test report 一份。"})
        self.write_units("a1", units)
        self.pipeline()
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertEqual(page.count("检测或审核报告页"), 1)

    def test_pictures_read_by_the_model_come_in_marked(self) -> None:
        self.pipeline()
        folder = self.build / "ai-read"
        folder.mkdir()
        (folder / "todo.jsonl").write_text(json.dumps({"key": "a1|5", "item_id": "a1", "page": 5, "name": "Catalog.pdf",
                                                       "image": "x.png"}) + "\n", encoding="utf-8")
        (folder / "answer-001.jsonl").write_text(json.dumps({"key": "a1|5", "text": "NTX S-077 SHERPA VEST"}) + "\n", encoding="utf-8")
        self.assertEqual(run("ai_read.py", "collect", "--out", str(self.out)).returncode, 0)
        record = json.loads((self.build / "mineru-units/a1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["units"][0]["engine"], "ai")
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("NTX S-077", page)
        self.assertIn("AI 读图转写", page)

    def test_register_reconciles_the_package_by_category(self) -> None:
        self.pipeline()
        register = (self.build / "vault/02_源资料/源资料与可检索性台账.md").read_text(encoding="utf-8")
        self.assertIn("## 初筛包处理对照", register)
        self.assertIn("| A 原文保留 |", register)
        self.assertIn("| E 排除 | 1 | 0 |", register)
        self.assertIn("初筛排除", register)

    def test_knowledge_points_register_shows_the_package_path_and_jsonl_type(self) -> None:
        from render_kb import write_register

        doc = {"doc_id": "DOC-POINTS", "item_id": "knowledge-points", "file_name": "业务要点-邮件摘录.md",
               "original_name": "初筛知识点（knowledge-points.jsonl）", "package_path": "C-知识点/knowledge-points.jsonl",
               "origin": "points", "source_sha256": "", "tier": "T4"}
        write_register(self.build, {}, {"DOC-POINTS": doc}, {"DOC-POINTS": "业务要点/业务要点-邮件摘录.md"},
                       {"DOC-POINTS": "待审"}, {}, {})
        register = (self.build / "vault/02_源资料/源资料与可检索性台账.md").read_text(encoding="utf-8")
        row = next(line for line in register.splitlines() if line.startswith("| DOC-POINTS |"))
        self.assertIn("`C-知识点/knowledge-points.jsonl`", row)
        self.assertIn("| jsonl |", row)
        self.assertNotIn("jsonl）", row)

    def test_a_document_the_rules_cut_heavily_is_listed(self) -> None:
        units = [{"locator": f"para {i}", "text": f"款式 {i} 单价 USD{i}.5"} for i in range(1, 6)]
        units.append({"locator": "para 9", "text": "我们有自有工厂。"})
        self.write_units("a1", units)
        self.pipeline()
        mat = next((self.build / "vault").rglob("01_源资料处理台账.md")).read_text(encoding="utf-8")
        self.assertIn("规则删得多的资料", mat)


if __name__ == "__main__":
    unittest.main()
