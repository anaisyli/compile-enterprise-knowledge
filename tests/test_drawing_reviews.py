#!/usr/bin/env python3
"""Old reviewed page transcripts on pages later classified as dimension drawings."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from kb_common import suppress_drawing_dimensions  # noqa: E402
from source_fidelity import reviewed_units, validate_review  # noqa: E402
from verify_kb import drawing_page_errors  # noqa: E402

SOURCE = "s" * 64
TABLE = "| 功耗 | 典型值 |\n| --- | --- |\n| 整机 | 120W |"
OLD_TRANSCRIPT = "DS50 安装尺寸图\n1476.5\n851.5\nVESA 400×400"


class DrawingReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.build = Path(self.folder.name)
        image = self.build / "reviewed-page-images" / "i1" / "page.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"picture")
        self.image = {"image_file": "reviewed-page-images/i1/page.png",
                      "image_sha256": hashlib.sha256(b"picture").hexdigest()}

    def tearDown(self) -> None:
        self.folder.cleanup()

    def review(self, locator: str = "page 1", text: str = OLD_TRANSCRIPT, **extra) -> None:
        record = {"locator": locator, "text": text, "reviewed_by": "agent", "source_sha256": SOURCE, **self.image, **extra}
        (self.build / "reviewed-pages.json").write_text(json.dumps({"i1": [record]}, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def label(locator: str = "page 1") -> dict:
        return {"locator": f"{locator} drawing", "text": "DS50 的安装尺寸图，尺寸见原图", "engine": "ai",
                "checked": True, "dimension_drawing": True, "drawing_model_confirmed": True}

    def test_restoring_an_old_transcript_keeps_the_drawing_label(self) -> None:
        self.review()
        units = reviewed_units(self.build, "i1", SOURCE, [{"locator": "page 1", "text": "1476.5\n851.5"}, self.label()])
        self.assertIn("page 1 drawing", [u["locator"] for u in units])

    def test_an_old_transcript_on_a_drawing_page_stays_out_of_the_text(self) -> None:
        self.review()
        units = reviewed_units(self.build, "i1", SOURCE, [{"locator": "page 1", "text": f"1476.5\n851.5\n{TABLE}"}, self.label()])
        self.assertFalse([u for u in units if u.get("engine") == "reviewed" and u["text"].strip()])
        self.assertTrue([u for u in units if u.get("review_withheld")])
        shown = "\n".join(u["text"] for u in suppress_drawing_dimensions(units))
        self.assertNotIn("1476.5", shown)
        self.assertIn("| 整机 | 120W |", shown)  # The parameter table beside the drawing stays.
        self.assertIn("尺寸见原图", shown)

    def test_with_a_platform_page_the_old_transcript_is_not_added_beside_it(self) -> None:
        self.review()
        units = reviewed_units(self.build, "i1", SOURCE, [{"locator": "page 1 ocr", "text": "1476.5", "engine": "mineru"},
                                                         self.label()])
        self.assertNotIn("page 1 review", [u["locator"] for u in units])
        self.assertIn("page 1 drawing", [u["locator"] for u in units])

    def test_a_transcript_split_to_the_non_drawing_part_enters(self) -> None:
        self.review(text=TABLE, part="non_drawing")
        units = reviewed_units(self.build, "i1", SOURCE, [{"locator": "page 1", "text": "1476.5"}, self.label()])
        shown = "\n".join(u["text"] for u in suppress_drawing_dimensions(units))
        self.assertIn("| 整机 | 120W |", shown)
        self.assertNotIn("1476.5", shown)
        self.assertIn("page 1 drawing", [u["locator"] for u in units])

    def test_a_page_that_is_not_a_drawing_still_takes_its_transcript(self) -> None:
        self.review(text="规格书正文，亮度 500 尼特")
        units = reviewed_units(self.build, "i1", SOURCE, [{"locator": "page 1", "text": "亮度 5OO 尼特"}])
        self.assertEqual([u["text"] for u in units], ["规格书正文，亮度 500 尼特"])

    def test_an_unknown_part_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_review(self.build, "i1", SOURCE, {"locator": "page 1", "text": "x", "reviewed_by": "agent",
                                                       "source_sha256": SOURCE, "part": "drawing", **self.image})

    def test_callouts_from_a_drawing_in_a_word_file_are_filtered_too(self) -> None:
        from extract_blocks import document_blocks
        units = [{"locator": "para 1", "text": "一、产品特点"},
                 {"locator": "image 2 check", "text": f"1476.5\n851.5\n{TABLE}", "engine": "ai", "checked": True},
                 self.label("image 2")]
        shown = "\n".join(u["text"] for u in suppress_drawing_dimensions(units))
        self.assertNotIn("1476.5", shown)
        self.assertIn("| 整机 | 120W |", shown)
        self.assertIn("一、产品特点", shown)
        blocks = document_blocks("DOC-1", "i1", units)
        by_loc = {b["locator"]: b["group"] for b in blocks}
        self.assertEqual(by_loc["image 2 drawing"], "图片 2")  # Must not inherit "一、产品特点".

    def test_verify_catches_a_lost_label_and_a_transcript_in_the_text(self) -> None:
        page_check = self.build / "page-check"
        page_check.mkdir()
        (page_check / "results.json").write_text(json.dumps({"i1|page 1": {
            "item_id": "i1", "unit": "page", "page": 1, "dimension_drawing": True},
            "i1|image 2": {"item_id": "i1", "unit": "image", "page": 2, "dimension_drawing": True}}), encoding="utf-8")
        docs = {"DOC-1": {"doc_id": "DOC-1", "item_id": "i1", "title": "公司资料-DS50规格书"}}
        published = {"DOC-1": "公司资料/公司资料-DS50规格书.md"}
        lost = [{"doc_id": "DOC-1", "locator": "page 1", "status": "kept", "reader": "reviewed"},
                {"doc_id": "DOC-1", "locator": "image 2 drawing", "group": "一、产品特点", "status": "kept", "reader": "ai"}]
        errors = drawing_page_errors(self.build, docs, lost, published)
        self.assertTrue(any("尺寸图标签不见了" in e for e in errors), errors)
        self.assertTrue(any("尺寸图文字块分组错位" in e for e in errors), errors)
        self.assertTrue(any("旧人工整页稿进了正文" in e for e in errors), errors)
        fine = [{"doc_id": "DOC-1", "locator": "page 1 drawing", "group": "第 1 页", "status": "kept", "reader": "ai"},
                {"doc_id": "DOC-1", "locator": "image 2 drawing", "group": "图片 2", "status": "kept", "reader": "ai"},
                {"doc_id": "DOC-1", "locator": "page 1", "status": "kept", "reader": ""}]
        self.assertEqual(drawing_page_errors(self.build, docs, fine, published), [])
        clean = self.build / "clean"
        md = clean / published["DOC-1"]
        md.parent.mkdir(parents=True)
        md.write_text("## 第 1 页\n\n![[03_正式知识/90_素材/a.png|360]]\n\n## 图片 2\n\nDS50 的安装尺寸图，尺寸见原图\n", encoding="utf-8")
        self.assertTrue(any("尺寸图来源页没有附原图" in e and "图片 2" in e
                            for e in drawing_page_errors(self.build, docs, fine, published, clean)))
        md.write_text("## 第 1 页\n\n![[03_正式知识/90_素材/a.png|360]]\n\n## 图片 2\n\n![[03_正式知识/90_素材/b.png|360]]\n", encoding="utf-8")
        self.assertEqual(drawing_page_errors(self.build, docs, fine, published, clean), [])
        self.review(text=TABLE, part="non_drawing")
        split = fine + [{"doc_id": "DOC-1", "locator": "page 1", "status": "kept", "reader": "reviewed"}]
        self.assertEqual(drawing_page_errors(self.build, docs, split, published), [])


if __name__ == "__main__":
    unittest.main()
