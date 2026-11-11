#!/usr/bin/env python3
"""Material pictures: named from the words beside them, sorted by use, renamed without breaking links."""

from __future__ import annotations

import csv
import json
import re
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pictures  # noqa: E402
from pictures import CHECK, FROM_AI, FROM_SOURCE, INTERNAL, NUMBERED, PUBLIC, Namer, build_pictures, write_index  # noqa: E402
from publish_vault import follow_picture_links, move_pictures  # noqa: E402

CONFIG = {"style_prefixes": ["ANSP"], "categories": {"wall-tapestry": "挂毯"}}


class PictureTests(unittest.TestCase):
    def test_large_size_classification_repair_preserves_the_given_basename(self) -> None:
        old = "资料包素材/广告机/Wall mounted Digital Signage/尺寸图/DS50-aabbccdd.jpg"
        item = {"id": "img-aabbccddeeff0011.jpg", "folder": "资料包素材/广告机/Wall mounted Digital Signage/详情图",
                "source_relpath": "DS50/大尺寸/中文版/DS50详情中文版/1.jpg", "label": "1", "prefix": "DS50",
                "alt": "DS50", "use": PUBLIC}
        saved = {item["id"]: {"file": old, "named_by": FROM_SOURCE}}
        result = pictures.assign([item], saved, {}, Namer(CONFIG, None))
        self.assertEqual(result[item["id"]]["file"], old.replace("/尺寸图/", "/详情图/"))
        item["source_relpath"] = "DS50/普通组/详情图/1.jpg"
        self.assertEqual(pictures.assign([item], saved, {}, Namer(CONFIG, None))[item["id"]]["file"], old)

    def test_retained_originals_are_usable_unless_waiting(self) -> None:
        design = {"kind": "image", "status": "media_kept", "relpath": "产品/X1/主图.psd", "bytes": 3 * 1024 * 1024}
        record = pictures.retained_record(design)
        self.assertEqual((record["name"], record["kind"], record["size"], record["folder"], record["status"]),
                         ("主图.psd", "设计稿", "3.0 MB", "产品/X1", "已确认可用"))
        self.assertEqual(pictures.retained_record({**design, "relpath": "产品/大图.jpg"})["note"], "大文件，见原位置")
        self.assertEqual(pictures.retained_record({**design, "relpath": "产品/.jpg", "bytes": 46428})["note"], "图片已保留，原件见原位置")
        video = pictures.retained_record({**design, "kind": "video", "relpath": "产品/宣传片.mp4"})
        self.assertEqual((video["kind"], video["status"]), ("视频", "已确认可用"))
        self.assertIn("待转写", video["note"])
        waiting = {**design, "kind": "video", "status": "media_awaiting_client", "relpath": "Acme 现场/1.mp4"}
        self.assertEqual(pictures.retained_record(waiting)["status"], "待客户确认")
        self.assertEqual(pictures.retained_record(waiting, re.compile("Acme"))["folder"], pictures.MASKED_NAME)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.build = Path(self.temp.name) / "build"
        (self.build / "assets").mkdir(parents=True)
        (self.build / "vault").mkdir()
        for name in ("img-aaaaaaaaaaaaaaaa.png", "img-bbbbbbbbbbbbbbbb.png", "img-cccccccccccccccc.jpg", "img-dddddddddddddddd.jpg",
                     "img-eeeeeeeeeeeeeeee.jpg"):
            (self.build / "assets" / name).write_bytes(b"x")
        self.docs = {
            "Q": {"doc_id": "Q", "item_id": "q", "title": "报价单-1a2b", "file_name": "报价单-1a2b.md", "folder": "产品表/报价单"},
            "C": {"doc_id": "C", "item_id": "c", "title": "产品画册-2026格子系列", "file_name": "产品画册-2026格子系列.md", "folder": "产品画册"},
            "W": {"doc_id": "W", "item_id": "web-1", "title": "挂毯-Elephant Wall Tapestry", "file_name": "挂毯-Elephant.md",
                  "folder": "官网页面/挂毯", "original_name": "Elephant Wall Tapestry"},
        }
        row = lambda locator, cells, images, flags=(): {"kind": "row", "status": "kept", "doc_id": "Q", "group": "表 1",
                                                       "locator": locator, "cells": cells, "images": images, "text": " | ".join(cells.values()),
                                                       "flags": list(flags)}
        self.by_doc = defaultdict(list)
        self.by_doc["Q"] = [row("表 1 第 2 行", {"style": "DG-7959 HOOD", "name": "LADIES TANK TOP"}, ["img-aaaaaaaaaaaaaaaa.png"]),
                            row("表 1 第 3 行", {"style": "ANSP-004"}, ["img-bbbbbbbbbbbbbbbb.png"])]
        self.by_doc["C"] = [{"kind": "text", "status": "kept", "doc_id": "C", "group": "第 3 页", "locator": "page 3",
                             "text": "SHERPA JACKET\nANSP-012 350GSM SHERPA FLEECE"}]
        self.by_doc["W"] = [{"kind": "text", "status": "kept", "doc_id": "W", "group": "Elephant Wall Tapestry", "locator": "section 1",
                             "text": "Elephant Wall Tapestry"}]
        self.status_info = {"c": {"page_pictures": {"3": ["img-cccccccccccccccc.jpg"]}}}
        (self.build / "website").mkdir()
        (self.build / "website/pictures.json").write_text(json.dumps({"web-1": [
            {"id": "img-dddddddddddddddd.jpg", "alt": "Elephant jacquard wall tapestry", "section": 1},
            {"id": "img-eeeeeeeeeeeeeeee.jpg", "alt": "IMG_0042.jpg", "section": 1}]}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def name_all(self, forbidden=None) -> dict:
        return build_pictures(self.build, CONFIG, self.docs, self.by_doc, ["Q", "C", "W"], {"Q": "有效", "C": "有效", "W": "有效"},
                              set(), self.status_info, forbidden, 2, set())

    def test_names_come_from_the_words_beside_the_picture(self) -> None:
        named = self.name_all()
        self.assertEqual(named["img-aaaaaaaaaaaaaaaa.png"]["file"], "报价单参考图/报价单参考图-LADIES TANK TOP-aaaaaaaa.png")
        self.assertEqual(named["img-bbbbbbbbbbbbbbbb.png"]["file"], "报价单参考图/报价单参考图-ANSP-004-bbbbbbbb.png")
        self.assertEqual(named["img-cccccccccccccccc.jpg"]["file"], "产品/2026格子系列/2026格子系列-ANSP-012 SHERPA JACKET-cccccccc.jpg")
        self.assertEqual(named["img-dddddddddddddddd.jpg"]["file"], "产品/挂毯/挂毯-Elephant jacquard wall tapestry-dddddddd.jpg")
        # A camera file name says nothing; the page's title does.
        self.assertEqual(named["img-eeeeeeeeeeeeeeee.jpg"]["alt"], "Elephant Wall Tapestry")
        self.assertEqual(named["img-dddddddddddddddd.jpg"]["english"], "elephant-jacquard-wall-tapestry-dddddddd.jpg")

    def test_buyers_numbers_and_customer_names_stay_out_of_names(self) -> None:
        namer = Namer(CONFIG, re.compile("Acme"))
        self.assertEqual(namer.label("DG-7959 HOOD"), "HOOD")
        self.assertEqual(namer.label("ANSP-004 280克摇粒绒"), "ANSP-004")
        self.assertEqual(namer.label("Acme fleece jacket"), "")
        self.by_doc["Q"][0]["flags"] = [{"type": "unvetted_term"}]  # A ⚠ passage does not name anything.
        self.assertEqual(self.name_all()["img-aaaaaaaaaaaaaaaa.png"]["named_by"], NUMBERED)

    def test_quotation_pictures_are_internal_and_website_ones_public(self) -> None:
        named = self.name_all()
        self.assertEqual(named["img-aaaaaaaaaaaaaaaa.png"]["use"], INTERNAL)
        self.assertEqual(named["img-aaaaaaaaaaaaaaaa.png"]["english"], "")
        self.assertEqual(named["img-cccccccccccccccc.jpg"]["use"], PUBLIC)
        self.assertEqual(named["img-dddddddddddddddd.jpg"]["use"], PUBLIC)
        write_index(self.build, named, self.docs, lambda s: s)
        page = (self.build / "vault" / pictures.INDEX).read_text(encoding="utf-8")
        self.assertIn("## 产品/挂毯（2 张）", page)
        self.assertNotIn("![[", page)
        with (self.build / "vault" / pictures.LIST_CSV).open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 5)
        self.assertIn("可用于官网", {r["能否用于官网"] for r in rows})

    def test_a_name_stays_and_only_a_numbered_one_gives_way(self) -> None:
        self.by_doc["Q"][0]["flags"] = [{"type": "unvetted_term"}]
        first = self.name_all()
        self.assertEqual(first["img-aaaaaaaaaaaaaaaa.png"]["named_by"], NUMBERED)
        self.by_doc["Q"][0]["flags"] = []
        self.by_doc["Q"][1]["cells"] = {"style": "ANSP-004", "name": "Sherpa Vest"}
        again = self.name_all()
        self.assertEqual(again["img-aaaaaaaaaaaaaaaa.png"]["named_by"], FROM_SOURCE)  # Numbered gave way.
        self.assertEqual(again["img-bbbbbbbbbbbbbbbb.png"]["file"], first["img-bbbbbbbbbbbbbbbb.png"]["file"])  # A name stays.

    def test_ai_names_are_used_for_numbered_pictures_and_marked(self) -> None:
        self.by_doc["C"][0]["text"] = "2026"  # Nothing to name the catalog picture by.
        self.assertEqual(self.name_all()["img-cccccccccccccccc.jpg"]["named_by"], NUMBERED)
        out = Path(self.temp.name)
        (out / "04_知识库构建").mkdir(exist_ok=True)
        for item in self.build.iterdir():
            item.rename(out / "04_知识库构建" / item.name)
        self.build = out / "04_知识库构建"
        (self.build / "docs.jsonl").write_text("".join(json.dumps(d) + "\n" for d in self.docs.values()), encoding="utf-8")
        self.name_all()
        self.assertEqual(pictures.export(out)["todo"], 1)
        (self.build / pictures.AI_FOLDER / "answers.jsonl").write_text(json.dumps(
            {"id": "img-cccccccccccccccc.jpg", "name": "格子衬衫 平铺", "alt": "红黑格子衬衫平铺", "english": "red black plaid shirt"},
            ensure_ascii=False) + "\n", encoding="utf-8")
        pictures.collect(out)
        named = self.name_all()["img-cccccccccccccccc.jpg"]
        self.assertEqual((named["named_by"], named["use"]), (FROM_AI, CHECK))
        self.assertIn("格子衬衫 平铺", named["file"])
        self.assertEqual(named["english"], "red-black-plaid-shirt-cccccccc.jpg")

    def test_published_numbered_pictures_move_and_links_follow(self) -> None:
        vault = Path(self.temp.name) / "vault"
        old = vault / "03_正式知识/90_素材/img-aaaaaaaaaaaaaaaa.png"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x")
        note = vault / "04_文章任务/草稿.md"
        note.parent.mkdir(parents=True)
        note.write_text("配图 ![[img-aaaaaaaaaaaaaaaa.png|200]]\n", encoding="utf-8")
        self.name_all()
        previous = {"03_正式知识/90_素材/img-aaaaaaaaaaaaaaaa.png": "h"}
        renamed: list = []
        moved = move_pictures(vault, self.build, previous, renamed)
        new = "03_正式知识/90_素材/报价单参考图/报价单参考图-LADIES TANK TOP-aaaaaaaa.png"
        self.assertTrue((vault / new).is_file())
        self.assertFalse(old.exists())
        self.assertIn(new, previous)
        self.assertEqual(follow_picture_links(vault, moved, set()), 1)
        self.assertIn("![[报价单参考图-LADIES TANK TOP-aaaaaaaa.png|200]]", note.read_text(encoding="utf-8"))

    def test_corrected_folder_moves_an_already_named_picture_without_duplicate(self) -> None:
        from kb_state import STATE
        vault = Path(self.temp.name) / "vault"
        old = "资料包素材/广告机/Wall mounted Digital Signage/尺寸图/DS50-aaaaaaaa.png"
        new = old.replace("/尺寸图/", "/详情图/")
        original = vault / "03_正式知识/90_素材" / old
        original.parent.mkdir(parents=True)
        original.write_bytes(b"image")
        state = vault / STATE / "picture-names.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"img-aaaaaaaaaaaaaaaa.png": {"file": old}}), encoding="utf-8")
        (self.build / "pictures.json").write_text(json.dumps({"img-aaaaaaaaaaaaaaaa.png": {"file": new}}), encoding="utf-8")
        previous = {f"03_正式知识/90_素材/{old}": "digest"}
        renamed = []
        self.assertEqual(move_pictures(vault, self.build, previous, renamed), {})
        self.assertFalse(original.exists())
        self.assertEqual((vault / "03_正式知识/90_素材" / new).read_bytes(), b"image")
        self.assertEqual(len(renamed), 1)


class WebsitePictureTests(unittest.TestCase):
    def test_pictures_keep_their_alt_and_section(self) -> None:
        from fetch_website import PageParser, picture_host, sections

        parser = PageParser()
        parser.feed("<h1>Wall Tapestry</h1><p>Woven.</p><img src='/a.jpg' alt='Elephant tapestry'>"
                    "<h2>Factory</h2><p>Looms.</p><img data-src='/b.jpg' src='data:,'><nav><img src='/logo.png'></nav>")
        parser.flush()
        owners: list = []
        units = sections(parser.blocks, owners)
        self.assertEqual(len(units), 2)
        self.assertEqual([(src, alt) for _, src, alt in parser.images], [("/a.jpg", "Elephant tapestry"), ("/b.jpg", "")])
        self.assertEqual([owners[min(max(p, 0), len(owners) - 1)] for p, _, _ in parser.images], [1, 2])
        self.assertTrue(picture_host("img.example.com", {"www.example.com", "example.com"}))
        self.assertFalse(picture_host("cdn.other.com", {"www.example.com", "example.com"}))


class SlideTests(unittest.TestCase):
    def deck(self, folder: Path) -> Path:
        """Three slide files shown in the order 3, 1 with slide 2 hidden."""
        import zipfile
        path = folder / "deck.pptx"
        p = "http://schemas.openxmlformats.org/presentationml/2006/main"
        r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst>'
                             '<p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId1"/><p:sldId id="258" r:id="rId2"/>'
                             '</p:sldIdLst></p:presentation>')
            archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                             + "".join(f'<Relationship Id="rId{i}" Target="slides/slide{i}.xml"/>' for i in (1, 2, 3)) + "</Relationships>")
            for number in (1, 2, 3):
                hidden = ' show="0"' if number == 2 else ""
                archive.writestr(f"ppt/slides/slide{number}.xml", f'<p:sld xmlns:p="{p}"{hidden}/>')
        return path

    def test_slides_follow_the_order_the_deck_shows_them(self) -> None:
        from extract_package_text import slide_order
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(slide_order(self.deck(Path(folder))), [3, 1])

    def test_slide_pictures_go_under_the_slide_heading(self) -> None:
        self.assertEqual(pictures.page_group({"page_unit": "slide"}, "3"), "第 3 张幻灯片")
        self.assertEqual(pictures.page_group({}, "3"), "第 3 页")

    def test_a_deck_without_libreoffice_still_reads_and_says_why(self) -> None:
        import extract_package_text
        original = extract_package_text.convert_office
        extract_package_text.convert_office = lambda *a, **k: (_ for _ in ()).throw(LookupError("needs_libreoffice"))
        try:
            with tempfile.TemporaryDirectory() as folder:
                result = extract_package_text.slide_pictures(self.deck(Path(folder)), Path(folder) / "converted", "k", 10)
        finally:
            extract_package_text.convert_office = original
        if "PyMuPDF" not in result["page_images_skipped"]:
            self.assertEqual(result["page_images_skipped"], "需要 LibreOffice")


class PdfPictureTests(unittest.TestCase):
    def test_photos_are_kept_but_icons_and_logos_are_not(self) -> None:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:
            self.skipTest("PyMuPDF is not installed")
        import os
        from extract_package_text import page_pictures

        noise = lambda side: pymupdf.Pixmap(pymupdf.csRGB, side, side, os.urandom(side * side * 3), 0)
        photo, logo, icon = noise(400), noise(400), noise(40)
        with tempfile.TemporaryDirectory() as folder:
            document = pymupdf.open()
            for number in range(5):
                page = document.new_page()
                page.insert_image(pymupdf.Rect(0, 0, 60, 60), pixmap=logo)  # On every page: a logo.
                page.insert_image(pymupdf.Rect(70, 0, 80, 10), pixmap=icon)
                if number == 1:
                    page.insert_image(pymupdf.Rect(100, 100, 400, 400), pixmap=photo)
            found = page_pictures(document, pymupdf, Path(folder))
            self.assertEqual(list(found), ["2"])
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
