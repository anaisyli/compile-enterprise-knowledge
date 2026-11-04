#!/usr/bin/env python3
"""Looking at pages next to their extracted text, and what that changes in the cleaned text."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_extensions import Base, run  # noqa: E402


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
class PageCheckTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        import pymupdf
        # A catalog page: two product photos and one line of text, the codes printed in the photos.
        document = pymupdf.open()
        page = document.new_page(width=960, height=540)
        picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
        picture.clear_with(200)
        for x in (40, 500):
            page.insert_image(pymupdf.Rect(x, 40, x + 400, 400), pixmap=picture)
        page.insert_text((40, 480), "SHERPA JACKET", fontsize=18)
        target = self.package / "A" / "Catalog.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(target))
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": [{"locator": "page 1", "text": "SHERPA JACKET"}]}),
                                           encoding="utf-8")

    def todo(self) -> list[dict]:
        path = self.build / "page-check" / "todo.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def answer(self, **fields) -> None:
        record = {"key": "a1|page 1", "missing": "", "text_errors": "", "pairing": "", "unclear": False, **fields}
        (self.build / "page-check" / "answer-001.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(run("page_check.py", "collect", "--out", str(self.out)).returncode, 0)

    def test_a_picture_page_is_picked_and_what_it_shows_is_added_marked(self) -> None:
        self.pipeline()
        picked = self.todo()
        self.assertEqual([t["key"] for t in picked], ["a1|page 1"])
        self.assertIn("2 张图、只找到 0 个款号", picked[0]["reasons"])
        self.assertTrue(Path(picked[0]["image"]).is_file())
        self.answer(missing="NTX S-099 SHERPA JACKET\nNTX S-100 SHERPA VEST", pairing="左：NTX S-099；右：NTX S-100")
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("NTX S-100 SHERPA VEST", page)
        self.assertIn("AI 看图补充", page)
        self.assertIn("看图核对：左：NTX S-099；右：NTX S-100", page)

    def test_broken_text_and_unclear_pairing_reach_the_ledgers(self) -> None:
        self.pipeline()
        self.answer(text_errors="SHERPA 印成了 SHAPER", unclear=True)
        self.pipeline()
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("以原图为准", page)
        self.assertIn("看不出款号和产品的对应", page)
        ledgers = "\n".join(p.read_text(encoding="utf-8") for p in (self.build / "vault").rglob("01_*.md"))
        self.assertIn("提取文字和原图不一致的页", ledgers)
        self.assertIn("款号和产品对应不清楚", ledgers)

    def test_unchecked_pages_are_reported_and_half_done_fails(self) -> None:
        self.pipeline()
        report = json.loads((self.build / "校验结果.json").read_text(encoding="utf-8"))
        self.assertTrue(any("还没做看图核对" in w for w in report["warnings"]))
        # Another page waiting while one is done: the check is unfinished.
        (self.build / "page-check" / "results.json").write_text(json.dumps({"a1|page 9": {
            "item_id": "a1", "unit": "page", "page": 9, "reasons": [], "added": False,
            "text_errors": "", "pairing": "", "unclear": False}}), encoding="utf-8")
        result = self.pipeline()
        self.assertEqual(result.returncode, 1)
        self.assertIn("看图核对没做完", result.stdout)


if __name__ == "__main__":
    unittest.main()
