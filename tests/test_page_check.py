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
        anm = next((self.build / "vault").rglob("01_源文与事实异常台账.md")).read_text(encoding="utf-8")
        self.assertIn("页提取的文字和原图不一致", anm)
        self.assertRegex(page, r"以原图为准.*ANM-\d{3}")
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

    def test_unchecked_ocr_page_still_requires_visual_review(self) -> None:
        self.clear_catalog_pending()
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        unit_path = ocr / "a1.json"
        unit_path.write_text(json.dumps({"units": [{"locator": "page 1 ocr", "text": "SHERPA JACKET"}]}), encoding="utf-8")

        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual([entry["key"] for entry in self.todo()], ["a1|page 1"])

        unit_path.write_text(json.dumps({"units": [{"locator": "page 1 ocr", "text": "SHERPA JACKET", "checked": True}]}),
                             encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual(self.todo(), [])

    def test_the_reviewer_sees_minerus_draft_and_what_it_adds_is_kept_beside_it(self) -> None:
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        (ocr / "a1.json").write_text(json.dumps({"item_id": "a1", "source": "mineru", "units": [
            {"locator": "page 1 ocr", "text": "SHERPA JACKET\nNTX S-099"}]}), encoding="utf-8")
        self.pipeline()
        picked = self.todo()
        self.assertIn("【MinerU 识别】\nSHERPA JACKET\nNTX S-099", picked[0]["text"])  # The draft, not an empty page.
        self.assertIn("MinerU 识别", picked[0]["draft"])
        self.answer(missing="NTX S-100 SHERPA VEST")
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("NTX S-099", page)  # MinerU's reading stays.
        self.assertIn("NTX S-100 SHERPA VEST", page)  # The reviewer's addition is not swallowed by it.

    def test_a_kind_chosen_for_mineru_waits_for_the_draft_then_every_pictured_page_is_looked_at(self) -> None:
        (self.build).mkdir(parents=True, exist_ok=True)
        (self.build / "sample-decisions.json").write_text(json.dumps({"catalog_pdf": {"status": "已确认", "method": "mineru"}}),
                                                          encoding="utf-8")
        self.pipeline()
        self.assertEqual(self.todo(), [])
        plan = json.loads((self.build / "page-check/plan.json").read_text(encoding="utf-8"))
        self.assertTrue(any("等 MinerU 初稿" in s for s in plan["skipped"]))
        result = run("mineru_ocr.py", "plan", "--out", str(self.out), "--package", str(self.package), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MinerU 初稿 + AI 复核", (self.build / "mineru-plan.md").read_text(encoding="utf-8"))

    def write_reading(self, folder: str, text: str, **extra) -> None:
        engine = {"engine": "reducto"} if folder == "reducto-units" else {}
        record = {"item_id": "a1", "source": folder.split("-")[0], "units": [{"locator": "page 1 ocr", "text": text, **engine}],
                  "pages_read": [1], **extra}
        (self.build / folder).mkdir(parents=True, exist_ok=True)
        (self.build / folder / "a1.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def test_a_page_reducto_and_mineru_read_differently_is_looked_at_with_both_drafts(self) -> None:
        self.write_reading("mineru-units", "NTX S-099 SHERPA JACKET\nNTX S-100 SHERPA VEST")
        self.write_reading("reducto-units", "NTX S-099 SHERPA JACKET\nNTX S-108 SHERPA VEST")
        self.pipeline()
        picked = self.todo()
        self.assertTrue(any(r.startswith("MinerU 与 Reducto 不一致") and "S100" in r and "S108" in r for r in picked[0]["reasons"]))
        self.assertIn("【Reducto 识别（与 MinerU 不一致", picked[0]["text"])
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("NTX S-100", page)  # MinerU's reading is the one published.
        self.assertNotIn("S-108", page)

    def test_a_page_only_reducto_read_is_published_marked_and_codes_only_in_figures_are_looked_at(self) -> None:
        self.write_reading("reducto-units", "NTX S-099 SHERPA JACKET\nNTX S-100 SHERPA VEST",
                           figures={"1": ["Photo of a jacket; the label reads NTX S-777."]})
        self.pipeline()
        picked = self.todo()
        self.assertIn("Reducto 只在图片说明里提到 S777，正文没有", picked[0]["reasons"])
        self.assertIn("Reducto 识别", picked[0]["draft"])
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("NTX S-100 SHERPA VEST", page)
        self.assertIn("Reducto 识别，待对照原图核对", page)
        self.assertNotIn("Photo of a jacket", page)  # The model's picture description is never published.

    def test_a_file_read_by_reducto_waits_for_it_and_its_b_page_is_planned_but_nothing_sent_without_approval(self) -> None:
        self.build.mkdir(parents=True, exist_ok=True)
        (self.build / "sample-decisions.json").write_text(json.dumps({"文件:a1": {"method": "reducto"}}, ensure_ascii=False), encoding="utf-8")
        self.pipeline()
        triage = json.loads((self.build / "page-triage.json").read_text(encoding="utf-8"))["files"]["a1"]
        self.assertEqual((triage["method"], triage["method_from"], triage["pages"]["1"]["grade"]), ("reducto", "文件", "B"))
        self.assertEqual(self.todo(), [])  # Not looked at before Reducto has read it.
        plan_page = (self.build / "vault" / "02_源资料" / "处理计划与样例.md")
        plan_page = plan_page if plan_page.is_file() else next((self.build / "vault").rglob("处理计划与样例.md"))
        self.assertIn("| a1 | ", plan_page.read_text(encoding="utf-8"))
        args = ("--out", str(self.out), "--package", str(self.package), "--config", str(self.config))
        result = run("reducto_ocr.py", "plan", *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads((self.build / "reducto-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([(e["item_id"], e["pages"]) for e in plan], [("a1", [1])])
        self.assertEqual(run("reducto_ocr.py", "run", *args).returncode, 2)
        self.assertFalse((self.build / "reducto-units").exists())


if __name__ == "__main__":
    unittest.main()
