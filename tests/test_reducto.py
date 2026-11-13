#!/usr/bin/env python3
"""Reducto: reading its answer by the file's own page numbers, choosing pages, and not paying twice."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from kb_common import reducto_figure_units, remaining_ocr_pages  # noqa: E402
from reducto_client import ReductoError, by_page, classify_failure, page_ranges  # noqa: E402
from reducto_ocr import candidates, compare, key_terms, store  # noqa: E402


class ClientTests(unittest.TestCase):
    def test_blocks_land_on_the_files_page_and_figure_descriptions_stay_apart(self) -> None:
        # With page_range [2, 5], Reducto numbers the parsed pages 1, 2; original_page is the file's page.
        chunks = [{"blocks": [
            {"type": "Title", "content": "Seals", "bbox": {"page": 1, "original_page": 2}},
            {"type": "Figure", "content": "Orange icon on a rounded background, label F00053", "bbox": {"page": 1, "original_page": 2}},
            {"type": "Table", "content": "<table><tr><td>A</td></tr></table>", "bbox": {"page": 2, "original_page": 5}},
            {"type": "Page Number", "content": "5", "bbox": {"page": 2, "original_page": 5}},
        ]}]
        pages = by_page(chunks)
        self.assertEqual(sorted(pages), [2, 5])
        self.assertEqual(pages[2]["text"], ["Seals"])
        self.assertEqual(pages[2]["figures"], ["Orange icon on a rounded background, label F00053"])
        self.assertEqual(pages[5]["text"], ["<table><tr><td>A</td></tr></table>"])

    def test_pages_are_sent_as_ranges_and_failures_say_what_to_do(self) -> None:
        self.assertEqual(page_ranges([5, 1, 2, 3, 9]), [{"start": 1, "end": 3}, {"start": 5, "end": 5}, {"start": 9, "end": 9}])
        self.assertEqual(classify_failure(ReductoError("HTTP 401：Invalid access token", "parse", status=401)), "credential-invalid")
        self.assertEqual(classify_failure(ReductoError("HTTP 429", "parse", status=429, retryable=True)), "quota-or-rate-limit")
        self.assertEqual(classify_failure(ReductoError("HTTP 503", "poll", status=503, retryable=True)), "temporary-network-or-service")


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.build = Path(self.folder.name)
        pdf = self.build / "a.pdf"
        pdf.write_bytes(b"%PDF")
        self.files = {"a1": {"file": pdf, "source_hash": "h1"}}
        (self.build / "routes.json").write_text(json.dumps({"a1": {"route": "kb", "display_name": "a.pdf", "ocr_pages": [2, 3]}}),
                                                encoding="utf-8")
        self.triage("reducto")

    def triage(self, method: str) -> None:
        pages = {"1": {"grade": "A", "reasons": []}, "2": {"grade": "C", "reasons": []}, "3": {"grade": "C", "reasons": []},
                 "7": {"grade": "B", "reasons": ["图片多、文字少"]}, "8": {"grade": "D", "reasons": ["MinerU 与 Reducto 不一致"]}}
        (self.build / "page-triage.json").write_text(json.dumps({"files": {"a1": {
            "name": "a.pdf", "kind": "pdf", "method": method, "method_from": "文件", "pages": pages}}}, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_only_files_read_by_reducto_send_their_b_and_c_pages_and_never_twice(self) -> None:
        [entry] = candidates(self.build, self.files)
        self.assertEqual(entry["pages"], [2, 3, 7])  # A pages are text, D pages go to the AI page check.
        self.assertIn("B 级图文页", entry["reasons"]["7"])
        result = {"job_id": "j1", "pages": {2: {"text": ["NTX S-100 120mm"], "figures": []},
                                            3: {"text": [], "figures": ["A photo, label S-777"]}}}
        store(self.build, entry, result)
        # Page 3 had only a figure description: it is not taken as read, MinerU or the model still read it.
        self.assertEqual(remaining_ocr_pages(self.build, "a1", [2, 3]), [3])
        self.assertEqual(candidates(self.build, self.files), [])  # Paid pages are not planned again.
        self.files["a1"]["source_hash"] = "h2"
        self.assertEqual(candidates(self.build, self.files)[0]["pages"], [2, 3, 7])  # A changed file is read again.
        self.triage("mineru")
        self.assertEqual(candidates(self.build, self.files), [])  # Read some other way: nothing goes to Reducto.

    def test_explicit_full_document_comparison_includes_text_pages_and_keeps_cache(self) -> None:
        [entry] = candidates(self.build, self.files, full_document_item_ids=["a1"])
        self.assertEqual(entry["pages"], [1, 2, 3, 7, 8])
        store(self.build, {**entry, "pages": [1]}, {"job_id": "j1", "pages": {1: {"text": ["cover"]}}})
        self.assertEqual(candidates(self.build, self.files, full_document_item_ids=["a1"])[0]["pages"], [2, 3, 7, 8])
        self.triage("mineru")
        self.assertEqual(candidates(self.build, self.files, full_document_item_ids=["a1"]), [])

    def test_a_file_nothing_was_read_from_is_read_by_mineru_unless_reducto_is_chosen(self) -> None:
        import pymupdf
        from page_check import unreadable_entry
        from samples import FILE_PREFIX, save_decisions

        document = pymupdf.open()
        for _ in range(3):
            document.new_page()
        document.save(str(self.files["a1"]["file"]))
        document.close()
        route = {"route": "hold", "reason": "no_extractable_content", "display_name": "a.pdf"}
        (self.build / "routes.json").write_text(json.dumps({"a1": route}), encoding="utf-8")
        entry = unreadable_entry("a1", route, self.files["a1"], self.build, {}, pymupdf)
        self.assertEqual((entry["kind"], entry["method"], entry["method_from"]), ("读不出内容", "mineru", "默认"))
        self.assertEqual(entry["counts"]["C"], 3)
        (self.build / "page-triage.json").write_text(json.dumps({"files": {"a1": entry}}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(candidates(self.build, self.files), [])  # MinerU's free quota first.
        decisions = {f"{FILE_PREFIX}a1": {"method": "reducto"}}
        save_decisions(self.build, decisions)
        entry = unreadable_entry("a1", route, self.files["a1"], self.build, decisions, pymupdf)
        (self.build / "page-triage.json").write_text(json.dumps({"files": {"a1": entry}}, ensure_ascii=False), encoding="utf-8")
        [chosen] = candidates(self.build, self.files)
        self.assertEqual(chosen["pages"], [1, 2, 3])
        self.assertIn("整份读不出内容", chosen["reasons"]["1"][0])

    def test_a_single_picture_is_read_by_the_model_unless_reducto_is_chosen_and_comes_back_as_page_1(self) -> None:
        from page_check import image_entry
        from reducto_ocr import one_page
        from samples import FILE_PREFIX

        picture = self.build / "p.png"
        picture.write_bytes(b"\x89PNG")
        self.files["p1"] = {"file": picture, "source_hash": "hp"}
        route = {"route": "hold", "reason": "image_needs_review", "display_name": "p.png"}
        (self.build / "routes.json").write_text(json.dumps({"p1": route}), encoding="utf-8")

        def grade(decisions: dict) -> dict:
            entry = image_entry("p1", route, self.files["p1"], decisions)
            (self.build / "page-triage.json").write_text(json.dumps({"files": {"p1": entry}}, ensure_ascii=False), encoding="utf-8")
            return entry

        entry = grade({})
        self.assertEqual((entry["kind"], entry["method"], entry["method_from"], entry["counts"]["C"]), ("单张图片", "ai", "默认", 1))
        self.assertEqual(candidates(self.build, self.files), [])
        self.assertEqual(grade({f"{FILE_PREFIX}p1": {"method": "mineru"}})["method"], "ai")  # MinerU takes no pictures.
        grade({f"{FILE_PREFIX}p1": {"method": "reducto"}})
        [chosen] = candidates(self.build, self.files)
        self.assertEqual((chosen["pages"], chosen["image"]), ([1], True))
        self.assertIn("单张图片", chosen["reasons"]["1"][0])
        store(self.build, chosen, {"job_id": "j1", "pages": one_page({2: {"text": ["NTX S-100"], "figures": []}})})
        units = json.loads((self.build / "reducto-units" / "p1.json").read_text(encoding="utf-8"))["units"]
        self.assertEqual([(u["locator"], u["text"]) for u in units], [("page 1 ocr", "NTX S-100")])
        self.assertEqual(candidates(self.build, self.files), [])  # Read once, not paid for again.

    def test_mineru_and_reducto_are_compared_on_codes_and_numbers_only(self) -> None:
        self.assertEqual(key_terms("NTX S-100, 120mm, 第 1 页"), key_terms("NTX S100 120 MM 第1页"))
        (self.build / "mineru-units").mkdir()
        (self.build / "mineru-units" / "a1.json").write_text(json.dumps({"source": "mineru", "units": [
            {"locator": "page 2 ocr", "text": "NTX S-100 SHERPA 120 mm F00053"}]}), encoding="utf-8")
        [entry] = candidates(self.build, self.files)
        self.assertEqual(entry["pages"], [3, 7])  # MinerU read page 2: not paid for again.
        store(self.build, {**entry, "pages": [2]}, {"job_id": "j1", "pages": {2: {"text": ["NTX S-108 sherpa 120mm"], "figures": ["label F-00053"]}}})
        page = compare(self.build, "a1")[2]
        self.assertEqual(page["only_mineru"], ["S100"])  # F00053 is in Reducto's figure description: not a disagreement.
        self.assertEqual(page["only_reducto"], ["S108"])

    def test_figure_units_keep_source_positions_and_do_not_count_as_page_transcripts(self) -> None:
        entry = {"item_id": "a1", "source_hash": "h1", "pages": [2, 3], "unit": "slide"}
        store(self.build, entry, {"job_id": "j1", "pages": {
            2: {"text": [], "figures": ["图中产品 S-100", " ", "图中产品 S-100", "表中尺寸 120 mm"]},
            3: {"text": [], "figures": ["图中产品 S-100"]},
        }})
        units = reducto_figure_units(self.build, "a1", "catalog.pptx")
        self.assertEqual([u["locator"] for u in units],
                         ["slide 2 figure 1", "slide 2 figure 4", "slide 3 figure 1"])
        self.assertEqual(units[1]["figure_source_locator"], 'figures["2"][3]')
        self.assertTrue(all(u["figure_description"] and u["engine"] == "reducto" for u in units))
        self.assertEqual(remaining_ocr_pages(self.build, "a1", [2, 3]), [2, 3])
        self.assertEqual(reducto_figure_units(self.build, "missing", "catalog.pdf"), [])


if __name__ == "__main__":
    unittest.main()
