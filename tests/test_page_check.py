#!/usr/bin/env python3
"""Looking at pages next to their extracted text, and what that changes in the cleaned text."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_extensions import Base, run  # noqa: E402
from page_check import collect, laid_out  # noqa: E402
from kb_common import sha256_file  # noqa: E402


class CheckedRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.build = self.root / "build"
        self.folder = self.build / "page-check"
        self.folder.mkdir(parents=True)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(b"unchanged original")
        self.save("routes.json", {"a1": {"package": str(self.root), "path": self.source.name,
                                         "source_hash": sha256_file(self.source)}})
        self.save("package-text/a1.json", {"units": [{"locator": "page 1", "text": "Valve DN50"}]})
        self.save("mineru-units/a1.json", {"units": [{"locator": "page 1 check", "text": "OLD",
                                                    "engine": "ai", "checked": True}]})
        self.save("page-check/results.json", {"a1|page 1": {"item_id": "a1", "unit": "page", "page": 1,
                    "reasons": ["抽查"], "draft": ["文字层"], "unclear": True}})
        (self.folder / "todo.jsonl").write_text("", encoding="utf-8")
        self.answer = self.folder / "answer-revised.jsonl"
        self.record = {"key": "a1|page 1", "missing": "", "unclear": False,
                       "structure": "表格", "structured": "| Valve | DN50 |"}
        self.write_answer()

    def save(self, name: str, value: dict) -> None:
        path = self.build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def write_answer(self) -> None:
        self.answer.write_text(json.dumps(self.record) + "\n", encoding="utf-8")

    def test_explicit_revision_reopens_checked_page_and_removes_superseded_addition(self) -> None:
        report = collect(self.build, revise_checked=True, answer_files=[self.answer])
        self.assertEqual((report["revised"], report["rejected"], report["left"]), (1, [], 0))
        result = json.loads((self.folder / "results.json").read_text(encoding="utf-8"))["a1|page 1"]
        self.assertFalse(result["unclear"])
        self.assertEqual(result["structured"], "| Valve | DN50 |")
        self.assertEqual(json.loads((self.build / "mineru-units/a1.json").read_text(encoding="utf-8"))["units"], [])

    def test_without_revision_mode_checked_result_stays_unchanged(self) -> None:
        report = collect(self.build)
        self.assertEqual(report["revised"], 0)
        self.assertTrue(json.loads((self.folder / "results.json").read_text(encoding="utf-8"))["a1|page 1"]["unclear"])

    def test_revised_structure_cannot_cite_invented_or_superseded_words(self) -> None:
        self.record["structured"] = "| Valve | Invented | OLD |"
        self.write_answer()
        collect(self.build, revise_checked=True)
        result = json.loads((self.folder / "results.json").read_text(encoding="utf-8"))["a1|page 1"]
        self.assertEqual(result["structured"], "")
        self.assertEqual(result["structured_refused"], ["Invented", "OLD"])

    def test_changed_original_blocks_revision(self) -> None:
        self.source.write_bytes(b"changed")
        report = collect(self.build, revise_checked=True)
        self.assertEqual(report["rejected"], ["a1|page 1"])
        self.assertEqual(report["revised"], 0)

    def test_revision_does_not_accept_unknown_page(self) -> None:
        self.record["key"] = "a1|page 2"
        self.write_answer()
        report = collect(self.build, revise_checked=True)
        self.assertEqual(report["rejected"], ["a1|page 2"])


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
class PageCheckTests(Base):
    def test_structured_table_cells_can_use_line_breaks_without_weakening_source_checks(self) -> None:
        accepted, refused = laid_out("| Alloy<br>Stellite |", "Alloy Stellite")
        self.assertEqual(accepted, "| Alloy<br>Stellite |")
        self.assertEqual(refused, [])
        accepted, refused = laid_out("| Alloy<br>Invented |", "Alloy Stellite")
        self.assertEqual(accepted, "")
        self.assertEqual(refused, ["Invented"])

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
        record = {"key": "a1|page 1", "missing": "", "text_errors": "", "pairing": "", "unclear": False,
                  "structure": "", "structured": "", "drawing_model": "", **fields}
        (self.build / "page-check" / "answer-001.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(run("page_check.py", "collect", "--out", str(self.out)).returncode, 0)

    def test_dimension_drawing_adds_only_caption_keeps_body_and_skips_dimension_text(self) -> None:
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": [
            {"locator": "page 1", "text": "SHERPA JACKET\nDN≤50\n619.88\nNormal page explanation"},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.pipeline()
        self.answer(structure="尺寸图", structured="外形及安装尺寸图", drawing_model="YFD71XA",
                    pairing="左边是型号 A，右边是型号 B", unclear=True, missing="DN≤50\nR150\nΦD=619.88",
                    text_errors="SHERPA 印成了 SHAPER")
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("YFD71XA 的外形及安装尺寸图，尺寸见原图", page)
        self.assertIn("Normal page explanation", page)
        self.assertNotIn("DN≤50", page)
        self.assertNotIn("619.88", page)
        self.assertNotIn("R150", page)
        self.assertNotIn("ΦD", page)
        self.assertNotIn("看图核对：左边是型号", page)
        self.assertNotIn("以原图为准", page)
        results = json.loads((self.build / "page-check" / "results.json").read_text(encoding="utf-8"))
        self.assertTrue(results["a1|page 1"]["dimension_drawing"])
        self.assertEqual(results["a1|page 1"]["text_errors"], "")
        self.assertFalse(results["a1|page 1"]["unclear"])
        self.assertTrue(results["a1|page 1"]["drawing_model_confirmed"])
        self.assertNotIn("第 1 页提取的文字和原图不一致",
                         next((self.build / "vault").rglob("01_源文与事实异常台账.md")).read_text(encoding="utf-8"))
        checked = run("verify_kb.py", "--package", str(self.package), "--text-dir", str(self.text),
                      "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_unknown_drawing_model_is_marked_and_registered_in_mat(self) -> None:
        self.pipeline()
        self.answer(structure="尺寸图", structured="安装尺寸图", drawing_model="型号待确认")
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("⚠ 型号待确认 的安装尺寸图，尺寸见原图", page)
        ledger = next((self.build / "vault").rglob("01_源资料处理台账.md")).read_text(encoding="utf-8")
        self.assertIn("尺寸图型号待确认", ledger)
        self.assertIn("第 1 页", ledger)

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

    def test_revised_answers_add_only_the_final_page_transcription(self) -> None:
        self.pipeline()
        first = {"key": "a1|page 1", "missing": "NTX S-099 WRONG DRAFT", "text_errors": "", "pairing": "", "unclear": False}
        final = {**first, "missing": "NTX S-100 SHERPA VEST", "unclear": True}
        folder = self.build / "page-check"
        (folder / "answer-001.jsonl").write_text(json.dumps(first) + "\n", encoding="utf-8")
        (folder / "answer-002.jsonl").write_text(json.dumps(final) + "\n", encoding="utf-8")
        result = run("page_check.py", "collect", "--out", str(self.out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        units = json.loads((self.build / "mineru-units/a1.json").read_text(encoding="utf-8"))["units"]
        additions = [u for u in units if u["locator"] == "page 1 check"]
        self.assertEqual([u["text"] for u in additions], ["NTX S-100 SHERPA VEST"])
        answers = json.loads((folder / "results.json").read_text(encoding="utf-8"))
        self.assertTrue(answers["a1|page 1"]["unclear"])

    def test_a_previously_accepted_answer_is_kept_when_the_page_leaves_the_plan(self) -> None:
        self.pipeline()
        self.answer(missing="NTX S-100 SHERPA VEST")
        folder = self.build / "page-check"
        (folder / "todo.jsonl").write_text("", encoding="utf-8")
        result = run("page_check.py", "collect", "--out", str(self.out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["rejected"], [])
        self.assertIn("a1|page 1", json.loads((folder / "results.json").read_text(encoding="utf-8")))

    def test_text_page_sample_does_not_expand_on_every_rebuild(self) -> None:
        import pymupdf
        document = pymupdf.open()
        units = []
        for number in range(1, 11):
            text = f"SHERPA JACKET technical information page {number}"
            document.new_page().insert_text((40, 40), text)
            units.append({"locator": f"page {number}", "text": text})
        document.save(str(self.package / "A/Catalog.pdf"))
        document.close()
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": units}), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        picked = self.todo()
        self.assertEqual(len(picked), 1)
        folder = self.build / "page-check"
        (folder / "answer-001.jsonl").write_text(json.dumps({"key": picked[0]["key"], "missing": "", "text_errors": "", "pairing": "", "unclear": False}) + "\n", encoding="utf-8")
        self.assertEqual(run("page_check.py", "collect", "--out", str(self.out)).returncode, 0)
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual(self.todo(), [])
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual(self.todo(), [])

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
        transcript, description = page.split("### ⚠ Reducto 图表描述（待核对）", 1)
        self.assertNotIn("Photo of a jacket", transcript)
        self.assertIn("Photo of a jacket", description)
        self.assertIn("不是已核实原文，也不是 OCR 转写", description)

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
