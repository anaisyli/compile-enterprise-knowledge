#!/usr/bin/env python3
"""Old full-page transcripts on dimension-drawing pages, split by choosing lines."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import split_reviews  # noqa: E402
from kb_common import suppress_drawing_dimensions  # noqa: E402
from source_fidelity import reviewed_units  # noqa: E402

SOURCE = "s" * 64
OLD = "DS50 安装尺寸图\n1476.5\n851.5\n| 功耗 | 典型值 |\n| --- | --- |\n| 整机 | 120W |\n整机需由专业人员安装。"
LABEL = {"locator": "page 3 drawing", "text": "DS50 的安装尺寸图，尺寸见原图", "engine": "ai", "checked": True,
         "dimension_drawing": True, "drawing_model_confirmed": True}


class SplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.build = Path(self.folder.name)
        image = self.build / "reviewed-page-images" / "i1" / "p3.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"page three")
        record = {"locator": "page 3", "text": OLD, "reviewed_by": "agent", "source_sha256": SOURCE,
                  "image_file": "reviewed-page-images/i1/p3.png", "image_sha256": hashlib.sha256(b"page three").hexdigest()}
        self.reviewed = self.build / "reviewed-pages.json"
        self.reviewed.write_text(json.dumps({"i1": [record]}, ensure_ascii=False), encoding="utf-8")
        (self.build / "page-check").mkdir()
        (self.build / "page-check" / "results.json").write_text(json.dumps({"i1|page 3": {
            "item_id": "i1", "unit": "page", "page": 3, "dimension_drawing": True,
            "drawing_caption": "DS50 的安装尺寸图"}}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def answer(self, **fields) -> None:
        path = self.build / "page-check" / "split-answer-001.jsonl"
        path.write_text(json.dumps({"key": "i1|page 3", **fields}, ensure_ascii=False) + "\n", encoding="utf-8")

    def todo(self) -> list[dict]:
        path = self.build / "page-check" / "split-todo.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def records(self) -> list[dict]:
        return json.loads(self.reviewed.read_text(encoding="utf-8"))["i1"]

    def test_plan_lists_the_page_with_numbered_lines_and_its_picture(self) -> None:
        self.assertEqual(split_reviews.plan(self.build), 1)
        entry = self.todo()[0]
        self.assertEqual(entry["key"], "i1|page 3")
        self.assertEqual(entry["lines"][3], {"n": 4, "text": "| 功耗 | 典型值 |"})
        self.assertTrue(entry["image"].endswith("p3.png"))

    def test_chosen_lines_become_the_non_drawing_part_word_for_word(self) -> None:
        split_reviews.plan(self.build)
        self.answer(keep_lines=[4, 5, 6, 7])
        self.assertEqual(split_reviews.collect(self.build)["split"], 1)
        record = self.records()[0]
        self.assertEqual(record["part"], "non_drawing")
        self.assertEqual(record["text"], "| 功耗 | 典型值 |\n| --- | --- |\n| 整机 | 120W |\n整机需由专业人员安装。")
        self.assertEqual(record["withheld_original"]["text"], OLD)  # The old transcript is kept whole.
        self.assertTrue(list(self.build.glob("reviewed-pages.before-split-*.json.bak")))
        self.assertEqual(split_reviews.split_errors(json.loads(self.reviewed.read_text(encoding="utf-8"))), [])
        units = reviewed_units(self.build, "i1", SOURCE, [{"locator": "page 3", "text": "1476.5"}, dict(LABEL)])
        shown = "\n".join(u["text"] for u in suppress_drawing_dimensions(units))
        self.assertIn("| 整机 | 120W |", shown)
        self.assertIn("尺寸见原图", shown)
        self.assertNotIn("1476.5", shown)
        split_reviews.plan(self.build)
        self.assertEqual(self.todo(), [])  # Split pages are not asked again.

    def test_a_line_number_outside_the_old_transcript_is_refused(self) -> None:
        split_reviews.plan(self.build)
        before = self.reviewed.read_text(encoding="utf-8")
        for bad in ([8], [0], [4, 4], ["4"], None):
            with self.subTest(keep_lines=bad):
                self.answer(keep_lines=bad)
                with self.assertRaises(ValueError):
                    split_reviews.collect(self.build)
                self.assertEqual(self.reviewed.read_text(encoding="utf-8"), before)

    def test_a_page_of_callouts_only_is_resolved_and_shows_nothing_old(self) -> None:
        split_reviews.plan(self.build)
        self.answer(keep_lines=[])
        self.assertEqual(split_reviews.collect(self.build)["drawing_only"], 1)
        self.assertEqual(self.records()[0]["split"], "drawing_only")
        units = reviewed_units(self.build, "i1", SOURCE, [dict(LABEL)])
        marker = [u for u in units if u.get("review_withheld")]
        self.assertTrue(marker and marker[0].get("review_resolved"))
        self.assertNotIn("1476.5", "\n".join(u["text"] for u in units))

    def test_an_unsplittable_page_stays_withheld_and_is_not_asked_again(self) -> None:
        split_reviews.plan(self.build)
        self.answer(unsplittable=True, note="尺寸和功耗写在同一行")
        self.assertEqual(split_reviews.collect(self.build)["unsplittable"], 1)
        self.assertEqual(self.records()[0]["text"], OLD)
        units = reviewed_units(self.build, "i1", SOURCE, [dict(LABEL)])
        marker = [u for u in units if u.get("review_withheld")]
        self.assertTrue(marker and not marker[0].get("review_resolved"))
        split_reviews.plan(self.build)
        self.assertEqual(self.todo(), [])

    def test_the_check_refuses_a_split_with_words_the_old_transcript_lacks(self) -> None:
        split_reviews.plan(self.build)
        self.answer(keep_lines=[6, 7])
        split_reviews.collect(self.build)
        data = json.loads(self.reviewed.read_text(encoding="utf-8"))
        data["i1"][0]["text"] = "| 整机 | 150W |\n整机需由专业人员安装。"
        self.assertTrue(split_reviews.split_errors(data))
        data["i1"][0]["text"] = "整机需由专业人员安装。\n| 整机 | 120W |"  # Same lines, other order.
        self.assertTrue(split_reviews.split_errors(data))


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import importlib.util  # noqa: E402

from kb_common import sha256_file  # noqa: E402
from test_extensions import Base, run  # noqa: E402


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
class SplitPipelineTests(Base):
    """The whole way: a page turns out to be a drawing, its old transcript is held back and listed,
    the model picks its lines, the split enters the text, and the old transcript reaches the vault."""

    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-997")
        import pymupdf
        document = pymupdf.open()
        page = document.new_page(width=960, height=540)
        picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
        picture.clear_with(200)
        page.insert_image(pymupdf.Rect(40, 40, 440, 400), pixmap=picture)
        page.insert_text((40, 480), "SHERPA JACKET", fontsize=18)
        self.pdf = self.package / "A" / "Catalog.pdf"
        self.pdf.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(self.pdf))
        manifest_path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        next(a for a in manifest["assets"] if a["item_id"] == "a1")["source_hash"] = sha256_file(self.pdf)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": [
            {"locator": "page 1", "text": "SHERPA JACKET"}]}), encoding="utf-8")

    def built(self) -> None:
        result = self.pipeline()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def page(self) -> str:
        return next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")

    def ledger(self) -> str:
        return (self.build / "vault/05_数据与审核/30_异常与待决定/20_源资料处理/01_源资料处理台账.md").read_text(encoding="utf-8")

    def test_a_split_old_transcript_enters_and_the_whole_one_reaches_the_vault(self) -> None:
        self.built()
        snapshot = next(json.loads(line) for line in (self.build / "page-check/todo.jsonl").read_text(encoding="utf-8").splitlines()
                        if json.loads(line)["key"] == "a1|page 1")
        (self.build / "page-check" / "answer-001.jsonl").write_text(json.dumps({
            "key": "a1|page 1", "review_digest": snapshot["review_digest"],
            "missing": "", "text_errors": "", "pairing": "", "unclear": False,
            "structure": "尺寸图", "structured": "安装尺寸图", "drawing_model": "NTX"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.assertEqual(run("page_check.py", "collect", "--out", str(self.out)).returncode, 0)
        image = self.build / "reviewed-page-images" / "a1" / "p1.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"page one")
        old = "SHERPA JACKET\n1476.5\n851.5\nMACHINE WASHABLE"
        (self.build / "reviewed-pages.json").write_text(json.dumps({"a1": [{
            "locator": "page 1", "text": old, "reviewed_by": "agent", "source_sha256": sha256_file(self.pdf),
            "image_file": "reviewed-page-images/a1/p1.png", "image_sha256": sha256_file(image)}]}), encoding="utf-8")
        self.built()
        self.assertNotIn("1476.5", self.page())
        self.assertIn("NTX 的安装尺寸图", self.page())
        self.assertIn("尺寸图页的旧人工整页稿没有进正文", self.ledger())
        todo = [json.loads(l) for l in (self.build / "page-check/split-todo.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([t["key"] for t in todo], ["a1|page 1"])
        (self.build / "page-check" / "split-answer-001.jsonl").write_text(
            json.dumps({"key": "a1|page 1", "keep_lines": [4]}) + "\n", encoding="utf-8")
        collected = run("page_check.py", "collect", "--out", str(self.out))
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        self.built()
        self.assertIn("MACHINE WASHABLE", self.page())
        self.assertNotIn("1476.5", self.page())
        self.assertNotIn("尺寸图页的旧人工整页稿没有进正文", self.ledger())
        vault = self.root / "vaults" / "P-997_测试知识库"
        made = run("kb_project.py", "--vault-root", str(self.root / "vaults"), "--project-id", "P-997", "--name", "测试",
                   "--website", "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.assertEqual(run("publish_vault.py", "--out", str(self.out), "--vault", str(vault)).returncode, 0)
        kept = json.loads((vault / "05_数据与审核/70_构建状态/reviewed-pages.json").read_text(encoding="utf-8"))
        self.assertEqual(kept["a1"][0]["withheld_original"]["text"], old)

    def test_an_unsplittable_page_stays_listed_for_a_person(self) -> None:
        self.built()
        snapshot = next(json.loads(line) for line in (self.build / "page-check/todo.jsonl").read_text(encoding="utf-8").splitlines()
                        if json.loads(line)["key"] == "a1|page 1")
        (self.build / "page-check" / "answer-001.jsonl").write_text(json.dumps({
            "key": "a1|page 1", "review_digest": snapshot["review_digest"],
            "missing": "", "text_errors": "", "pairing": "", "unclear": False,
            "structure": "尺寸图", "structured": "安装尺寸图", "drawing_model": "NTX"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        run("page_check.py", "collect", "--out", str(self.out))
        image = self.build / "reviewed-page-images" / "a1" / "p1.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"page one")
        (self.build / "reviewed-pages.json").write_text(json.dumps({"a1": [{
            "locator": "page 1", "text": "SHERPA JACKET 1476.5", "reviewed_by": "agent", "source_sha256": sha256_file(self.pdf),
            "image_file": "reviewed-page-images/a1/p1.png", "image_sha256": sha256_file(image)}]}), encoding="utf-8")
        self.built()
        (self.build / "page-check" / "split-answer-001.jsonl").write_text(
            json.dumps({"key": "a1|page 1", "unsplittable": True, "note": "款名和尺寸在同一行"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.assertEqual(run("page_check.py", "collect", "--out", str(self.out)).returncode, 0)
        self.built()
        self.assertIn("判为无法拆分，需人工处理：款名和尺寸在同一行", self.ledger())


if __name__ == "__main__":
    unittest.main()
