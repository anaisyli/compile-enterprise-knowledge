#!/usr/bin/env python3
"""Drawing PDFs (dimension sheets, six views, assembly drawings) are material, not text."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extract_blocks import drawing_kind, drawing_pictures  # noqa: E402


def asset(path: str, item_id: str = "x1") -> dict:
    return {"item_id": item_id, "display_name": Path(path).name, "path": path}


def page(text: str, number: int = 1) -> dict:
    return {"locator": f"page {number}", "text": text}


class DrawingTests(unittest.TestCase):
    def test_a_file_in_a_drawing_folder_is_a_drawing(self) -> None:
        self.assertEqual(drawing_kind(asset("产品/自助机/尺寸图/15.6.pdf"), [page("x")], {}), "尺寸图")
        self.assertEqual(drawing_kind(asset("户外壁挂广告机规格书/21.5寸壁挂横竖屏装配150.PDF"), [page("x")], {}), "尺寸图")
        self.assertEqual(drawing_kind(asset("尺寸图/23.6寸自助机效果图.pdf"), [page("x")], {}), "效果图")

    def test_a_manual_or_parameter_sheet_is_read(self) -> None:
        self.assertEqual(drawing_kind(asset("3.说明书-DS50/DS50说明书-中文版.pdf"), [page("1200 x 800")], {}), "")
        self.assertEqual(drawing_kind(asset("规格书技术参数/21.5寸壁挂款.pdf"), [page("尺寸 300 400")], {}), "")
        self.assertEqual(drawing_kind(asset("尺寸图/notes.docx"), [page("12 34")], {}), "")

    def test_a_short_pdf_of_numbers_is_a_drawing(self) -> None:
        numbers = "  ".join(str(n) for n in range(100, 130))
        self.assertEqual(drawing_kind(asset("壁挂自助款/21.5寸壁挂.pdf"), [page(numbers)], {}), "尺寸图")
        prose = "本产品采用工业级主板，支持安卓和 Windows 系统，" * 5 + "尺寸 1200 x 800"
        self.assertEqual(drawing_kind(asset("壁挂自助款/介绍.pdf"), [page(prose)], {}), "")

    def test_the_config_decides_either_way(self) -> None:
        self.assertEqual(drawing_kind(asset("资料/A.pdf"), [page("文字很多的说明")], {"drawing_items": ["x1"]}), "尺寸图")
        self.assertEqual(drawing_kind(asset("尺寸图/B.pdf"), [page("1 2")], {"not_drawing_items": ["x1"]}), "")

    def test_each_page_becomes_a_picture(self) -> None:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("PyMuPDF not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = pymupdf.open()
            for _ in range(2):
                pdf.new_page().insert_text((72, 72), "1200 800 45")
            pdf.save(root / "d.pdf")
            pdf.close()
            names = drawing_pictures(root / "d.pdf", root, "ab" * 32)
            self.assertEqual(names, ["img-abababababababab-p1.png", "img-abababababababab-p2.png"])
            self.assertTrue(all((root / n).is_file() for n in names))
            (root / "bad.pdf").write_bytes(b"not a pdf")
            self.assertEqual(drawing_pictures(root / "bad.pdf", root, "cd" * 32), [])


if __name__ == "__main__":
    unittest.main()
