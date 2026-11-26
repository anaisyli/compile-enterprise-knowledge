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
from products import COMPANY_GROUPS, Matcher  # noqa: E402
from publish_vault import follow_picture_links, move_pictures  # noqa: E402

CONFIG = {"style_prefixes": ["ANSP"], "categories": {"wall-tapestry": "挂毯"}}


class PictureTests(unittest.TestCase):
    def test_restore_published_names_checks_all_hashes_before_writing_and_protects_ai(self) -> None:
        from kb_state import STATE as VAULT_STATE
        out = Path(self.temp.name) / "run"
        build = out / "04_知识库构建"
        vault = Path(self.temp.name) / "published"
        originals = {"img-a.png": {"file": "产品/A/a.png", "named_by": NUMBERED},
                     "img-b.png": {"file": "产品/B/b.png", "named_by": FROM_SOURCE}}
        current = {key: {"file": "证书/" + Path(value["file"]).name, "named_by": FROM_SOURCE}
                   for key, value in originals.items()}
        (build / "assets").mkdir(parents=True)
        for key, entry in originals.items():
            original = vault / pictures.ASSETS / entry["file"]
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(key.encode())
            (build / "assets" / key).write_bytes(key.encode())
        pictures.write_json(vault / VAULT_STATE / pictures.STATE, originals)
        pictures.write_json(build / pictures.STATE, current)
        before = (build / pictures.STATE).read_bytes()
        (build / "assets/img-b.png").write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            pictures.restore_names(out, vault, list(originals))
        self.assertEqual((build / pictures.STATE).read_bytes(), before)
        (build / "assets/img-b.png").write_bytes(b"img-b.png")
        current["img-b.png"]["named_by"] = FROM_AI
        pictures.write_json(build / pictures.STATE, current)
        with self.assertRaisesRegex(ValueError, "Protected picture name"):
            pictures.restore_names(out, vault, list(originals))
        current["img-b.png"]["named_by"] = FROM_SOURCE
        pictures.write_json(build / pictures.STATE, current)
        result = pictures.restore_names(out, vault, list(originals))
        self.assertEqual(result["restored"], 2)
        self.assertEqual(pictures.load_json(build / pictures.STATE), originals)
        self.assertEqual(pictures.load_json(vault / VAULT_STATE / pictures.STATE), originals)

    def test_restore_names_rejects_path_outside_published_assets(self) -> None:
        from kb_state import STATE as VAULT_STATE
        out = Path(self.temp.name) / "run"
        vault = Path(self.temp.name) / "published"
        state = {"img-a.png": {"file": "../../outside.png", "named_by": FROM_SOURCE}}
        pictures.write_json(out / "04_知识库构建" / pictures.STATE, state)
        pictures.write_json(vault / VAULT_STATE / pictures.STATE, state)
        with self.assertRaisesRegex(ValueError, "outside assets"):
            pictures.restore_names(out, vault, ["img-a.png"])

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
        self.assertEqual(pictures.retained_record({**design, "relpath": "产品/主图.jpg"})["note"], "留在初筛包原位，见原位置")
        self.assertEqual(pictures.retained_record({**design, "relpath": "产品/大图.jpg", "bytes": 6 * 1024 * 1024})["note"], "大文件，见原位置")
        self.assertEqual(pictures.retained_record({**design, "relpath": "产品/.jpg", "bytes": 46428})["note"], "留在初筛包原位，见原位置")
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

    def name_all(self, forbidden=None, match=None, places=None) -> dict:
        extra = {"match": match} if match is not None else {}
        return build_pictures(self.build, CONFIG, self.docs, self.by_doc, ["Q", "C", "W"], {"Q": "有效", "C": "有效", "W": "有效"},
                              set(), self.status_info, forbidden, 2, set(), places, **extra)

    def test_names_come_from_the_words_beside_the_picture(self) -> None:
        named = self.name_all()
        self.assertEqual(named["img-aaaaaaaaaaaaaaaa.png"]["file"], "报价单参考图/报价单参考图-LADIES TANK TOP-aaaaaaaa.png")
        self.assertEqual(named["img-bbbbbbbbbbbbbbbb.png"]["file"], "报价单参考图/报价单参考图-ANSP-004-bbbbbbbb.png")
        # A picture printed in a document: filed by the document, named by its page and the page's only
        # company code. The page's heading describes it, but half a sentence never becomes a file name.
        self.assertEqual(named["img-cccccccccccccccc.jpg"]["file"], "文档提取图/2026格子系列/ANSP-012-第3页-cccccccc.jpg")
        self.assertIn("SHERPA JACKET", named["img-cccccccccccccccc.jpg"]["alt"])
        self.assertEqual(named["img-dddddddddddddddd.jpg"]["file"],
                         "官网图片/Elephant Wall Tapestry/挂毯-Elephant jacquard wall tapestry-dddddddd.jpg")
        # A camera file name says nothing; the page's title does.
        self.assertEqual(named["img-eeeeeeeeeeeeeeee.jpg"]["alt"], "Elephant Wall Tapestry")
        self.assertEqual(named["img-dddddddddddddddd.jpg"]["english"], "elephant-jacquard-wall-tapestry-dddddddd.jpg")

    def test_dimension_drawing_pictures_are_named_and_filed_by_model(self) -> None:
        (self.build / "page-check").mkdir(parents=True)
        (self.build / "page-check/results.json").write_text(json.dumps({"c|page 3": {
            "dimension_drawing": True, "drawing_model": "YFD71XA", "drawing_name": "外形及安装尺寸图"}}), encoding="utf-8")
        named = self.name_all()
        entry = named["img-cccccccccccccccc.jpg"]
        self.assertEqual(entry["file"], "尺寸图/YFD71XA/YFD71XA-尺寸图-cccccccc.jpg")
        self.assertEqual(entry["use"], CHECK)

    def test_page_drawing_files_with_the_product_its_model_matches(self) -> None:
        (self.build / "page-check").mkdir(parents=True)
        (self.build / "page-check/results.json").write_text(json.dumps({"c|page 3": {
            "dimension_drawing": True, "drawing_model": "YFD71XA", "drawing_name": "外形及安装尺寸图"}}), encoding="utf-8")
        products = [{"name": "YFD71XA 蝶阀", "kind": "product", "category": "阀门", "aliases": ["YFD71XA"]}]
        named = self.name_all(match=Matcher(products))
        self.assertEqual(named["img-cccccccccccccccc.jpg"]["file"],
                         "尺寸图/阀门/YFD71XA 蝶阀/YFD71XA-尺寸图-cccccccc.jpg")

    def test_page_drawing_never_files_under_a_company_group_or_waits_for_a_model(self) -> None:
        (self.build / "page-check").mkdir(parents=True)
        (self.build / "page-check/results.json").write_text(json.dumps({
            "c|page 3": {"dimension_drawing": True, "drawing_model": "工厂", "drawing_name": "车间尺寸图"},
            "c|page 4": {"dimension_drawing": True, "drawing_model": "型号待确认"}}, ensure_ascii=False), encoding="utf-8")
        (self.build / "assets" / "img-9999999999999999.jpg").write_bytes(b"x")
        self.status_info["c"]["page_pictures"]["4"] = ["img-9999999999999999.jpg"]
        named = self.name_all(match=Matcher(COMPANY_GROUPS))
        self.assertEqual(named["img-cccccccccccccccc.jpg"]["file"], "尺寸图/工厂/工厂-尺寸图-cccccccc.jpg")
        self.assertNotIn("资料包素材", named["img-cccccccccccccccc.jpg"]["file"])
        self.assertEqual(named["img-9999999999999999.jpg"]["file"],
                         "尺寸图/2026格子系列/2026格子系列-尺寸图-99999999.jpg")

    def test_single_image_dimension_drawing_files_under_its_model(self) -> None:
        media = [{"item_id": "m1", "kind": "image", "picture_kind": "尺寸图", "status": "dimension_drawing",
                  "drawing_model": "型号待确认", "relpath": "待归类/外形图.png", "id": "img-1111111111111111.png"}]
        (self.build / "media.json").write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")
        (self.build / "assets" / "img-1111111111111111.png").write_bytes(b"x")
        named = self.name_all()
        self.assertEqual(named["img-1111111111111111.png"]["file"],
                         "尺寸图/型号待确认/型号待确认-尺寸图-11111111.png")

    def test_single_image_dimension_drawing_files_with_the_product_its_model_matches(self) -> None:
        media = [{"item_id": "m2", "kind": "image", "picture_kind": "尺寸图", "status": "dimension_drawing",
                  "drawing_model": "YFD71XA", "relpath": "待归类/外形图.png", "id": "img-2222222222222222.png"}]
        (self.build / "media.json").write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")
        (self.build / "assets" / "img-2222222222222222.png").write_bytes(b"x")
        products = [{"name": "YFD71XA 蝶阀", "kind": "product", "category": "阀门", "aliases": ["YFD71XA"]}]
        named = self.name_all(match=Matcher(products))
        self.assertEqual(named["img-2222222222222222.png"]["file"],
                         "尺寸图/阀门/YFD71XA 蝶阀/YFD71XA-尺寸图-22222222.png")

    def test_a_published_single_image_drawing_moves_folder_but_keeps_its_name(self) -> None:
        media = [{"item_id": "m1", "kind": "image", "picture_kind": "尺寸图", "status": "dimension_drawing",
                  "drawing_model": "型号待确认", "relpath": "待归类/外形图.png", "id": "img-3333333333333333.png"}]
        (self.build / "media.json").write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")
        (self.build / "assets" / "img-3333333333333333.png").write_bytes(b"x")
        (self.build / pictures.STATE).write_text(json.dumps(
            {"img-3333333333333333.png": {"file": "资料包素材/产品/型号待确认/尺寸图/X-33333333.png",
                                          "named_by": FROM_SOURCE}}), encoding="utf-8")
        named = self.name_all()
        self.assertEqual(named["img-3333333333333333.png"]["file"], "尺寸图/型号待确认/X-33333333.png")

    def drawing_pdf(self, item_id: str, digest: str, pages: int, relpath: str) -> list:
        return [{"item_id": item_id, "kind": "image", "picture_kind": "尺寸图", "status": "media_kept",
                 "drawing": True, "page": number, "id": f"img-{digest}-p{number}.png", "relpath": relpath}
                for number in range(1, pages + 1)]

    def install_media(self, entries: list) -> None:
        (self.build / "media.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        for entry in entries:
            (self.build / "assets" / entry["id"]).write_bytes(b"x")

    def test_a_multi_page_drawing_pdf_names_each_page_and_a_single_one_stays_plain(self) -> None:
        from kb_common import short_hash
        entries = self.drawing_pdf("dwg1", "0123456789abcdef", 2, "产品/YFD71XA/尺寸图/外形尺寸图.pdf")
        entries += self.drawing_pdf("dwg2", "a" * 16, 1, "产品/YFD71XA/尺寸图/单页图.pdf")
        self.install_media(entries)
        named = self.name_all()
        for number in (1, 2):
            picture_id = f"img-0123456789abcdef-p{number}.png"
            self.assertEqual(named[picture_id]["file"],
                             f"尺寸图/产品/YFD71XA/尺寸图/尺寸图-外形尺寸图-第{number}页-{short_hash(picture_id, 8)}.png")
        single = f"img-{'a' * 16}-p1.png"
        self.assertEqual(named[single]["file"], f"尺寸图/产品/YFD71XA/尺寸图/尺寸图-单页图-{single[4:12]}.png")

    def test_a_saved_drawing_page_keeps_its_file(self) -> None:
        entries = self.drawing_pdf("dwg1", "0123456789abcdef", 2, "产品/YFD71XA/尺寸图/外形尺寸图.pdf")
        self.install_media(entries)
        saved = "产品/YFD71XA/尺寸图/旧名字-aaaa1111.png"
        (self.build / pictures.STATE).write_text(json.dumps(
            {"img-0123456789abcdef-p2.png": {"file": saved, "named_by": FROM_SOURCE}}), encoding="utf-8")
        named = self.name_all()
        self.assertEqual(named["img-0123456789abcdef-p2.png"]["file"], saved)
        self.assertIn("-第1页-", named["img-0123456789abcdef-p1.png"]["file"])

    def test_mixed_drawing_page_keeps_photograph_in_its_existing_category(self) -> None:
        (self.build / "page-check").mkdir(parents=True)
        (self.build / "page-check/results.json").write_text(json.dumps({"c|page 3": {
            "dimension_drawing": True, "drawing_model": "YFD71XA", "drawing_picture_ids": []}}), encoding="utf-8")
        (self.build / "assets" / "img-ffffffffffffffff.png").write_bytes(b"x")
        self.status_info["c"]["page_images"] = {"3": "img-ffffffffffffffff.png"}
        named = self.name_all()
        self.assertTrue(named["img-ffffffffffffffff.png"]["file"].startswith("尺寸图/"))
        self.assertFalse(named["img-cccccccccccccccc.jpg"]["file"].startswith("尺寸图/"))

    def test_explicit_drawing_classification_moves_old_asset_category_but_keeps_basename(self) -> None:
        item = {"id": "img-cccccccccccccccc.jpg", "folder": "产品/YFD71XA/尺寸图", "dimension_drawing": True,
                "prefix": "YFD71XA", "label": "尺寸图", "alt": "尺寸见原图", "use": CHECK}
        saved = {item["id"]: {"file": "页面图/old-stable-cccccccc.jpg", "named_by": FROM_SOURCE}}
        actual = pictures.assign([item], saved, {}, Namer(CONFIG, None))
        self.assertEqual(actual[item["id"]]["file"], "产品/YFD71XA/尺寸图/old-stable-cccccccc.jpg")

    def test_mixed_page_drawing_caption_cannot_name_photographs(self) -> None:
        (self.build / "page-check").mkdir(parents=True)
        (self.build / "page-check/results.json").write_text(json.dumps({"c|page 3": {
            "dimension_drawing": True, "drawing_picture_ids": [], "drawing_caption": "ANSP-012 的安装尺寸图"}}), encoding="utf-8")
        self.by_doc["C"] = [{"kind": "text", "status": "flagged", "doc_id": "C", "group": "第 3 页",
                              "text": "Photo", "flags": [{"type": "unvetted_term"}]},
                            {"kind": "text", "status": "kept", "doc_id": "C", "group": "第 3 页",
                              "text": "ANSP-012 的安装尺寸图", "dimension_drawing": True}]
        named = self.name_all()
        self.assertNotIn("安装尺寸图", named["img-cccccccccccccccc.jpg"]["file"])

    def test_repair_only_automatic_photograph_name_from_adjacent_drawing(self) -> None:
        item = {"id": "img-10f6ad79b36a62ba.png", "folder": "产品/YFD71XA", "prefix": "YFD71XA",
                "label": "", "alt": "", "use": CHECK, "excluded_drawing_caption": "YFD71XA 的蝶阀外形及安装尺寸图",
                "excluded_drawing_name": "蝶阀外形及安装尺寸图"}
        old = {item["id"]: {"file": "产品/YFD71XA/YFD71XA-蝶阀外形及安装尺寸图-10f6ad79.png", "named_by": FROM_SOURCE}}
        actual = pictures.assign([item], old, {}, Namer(CONFIG, None))
        self.assertEqual(actual[item["id"]]["file"], "产品/YFD71XA/YFD71XA-10f6ad79.png")
        old[item["id"]]["named_by"] = FROM_AI
        self.assertEqual(pictures.assign([item], old, {}, Namer(CONFIG, None))[item["id"]]["file"], old[item["id"]]["file"])

    def test_vector_drawing_page_gets_preview_without_ocr_and_changed_source_is_rejected(self) -> None:
        import pymupdf
        from kb_common import sha256_file
        source = self.build / "vector.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.draw_rect(pymupdf.Rect(20, 20, 200, 200))
            doc.save(str(source))
        (self.build / "page-check").mkdir(parents=True)
        (self.build / "page-check/results.json").write_text(json.dumps({"c|page 1": {
            "item_id": "c", "unit": "page", "page": 1, "dimension_drawing": True}}), encoding="utf-8")
        routes = {"c": {"route": "kb", "source_hash": sha256_file(source), "package": str(self.build), "path": source.name}}
        info = {"c": {"page_images": {}}}
        pictures.ensure_dimension_previews(self.build, routes, info)
        self.assertTrue((self.build / "assets" / info["c"]["page_images"]["1"]).is_file())
        for image in (self.build / "assets").glob("img-*.png"):
            if image.name == info["c"]["page_images"]["1"]:
                image.unlink()
        source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Drawing source changed"):
            pictures.ensure_dimension_previews(self.build, routes, info)

    def test_word_embedded_dimension_drawing_gets_preview_and_renders_on_source_page(self) -> None:
        import zipfile
        from kb_common import sha256_file
        from render_kb import render_doc
        docx = self.build / "spec.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
            archive.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20_000)
        (self.build / "page-check").mkdir(parents=True, exist_ok=True)
        (self.build / "page-check/results.json").write_text(json.dumps({"c|image 1": {
            "item_id": "c", "unit": "image", "page": 1, "dimension_drawing": True,
            "drawing_model": "YFD71XA", "drawing_model_confirmed": True}}), encoding="utf-8")
        routes = {"c": {"route": "kb", "source_hash": sha256_file(docx), "package": str(self.build), "path": docx.name}}
        self.status_info["c"]["page_images"] = {}
        pictures.ensure_dimension_previews(self.build, routes, self.status_info)
        pic_id = self.status_info["c"]["page_images"]["image 1"]
        self.assertTrue((self.build / "assets" / pic_id).is_file())
        named = self.name_all()
        self.assertTrue(named[pic_id]["file"].startswith("尺寸图/"))
        self.assertEqual(pictures.page_group(self.status_info["c"], "image 1"), "图片 1")
        page_md = render_doc(
            {**self.docs["C"], "tier": "T2", "module": "20_产品与规格", "source_sha256": "h"},
            [{"kind": "text", "status": "kept", "doc_id": "C", "group": "图片 1", "locator": "image 1 drawing",
              "text": "YFD71XA 的外形尺寸图", "dimension_drawing": True, "drawing_model_confirmed": True}],
            "有效", [], {}, {}, {"图片 1": named[pic_id]["file"]},
        )
        self.assertIn(f"## 图片 1\n\n![[{named[pic_id]['file']}|360]]", page_md)
        self.assertIn("YFD71XA 的外形尺寸图，尺寸见原图", page_md)

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
        self.assertIn("## 90_素材目录概览", page)
        self.assertIn("| 官网图片/Elephant Wall Tapestry | 2 | 2 | 0 | 0 |", page)
        self.assertIn("## 按产品", page)
        self.assertNotIn("![[", page)
        with (self.build / "vault" / pictures.LIST_CSV).open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 5)
        self.assertIn("产品", rows[0])
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

    def test_ai_descriptions_go_into_the_alt_and_never_rename_the_file(self) -> None:
        self.by_doc["C"][0]["text"] = "2026"  # Nothing on the page describes the catalog picture.
        before = self.name_all()["img-cccccccccccccccc.jpg"]["file"]
        self.assertEqual(before, "文档提取图/2026格子系列/第3页-cccccccc.jpg")
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
        self.assertEqual((named["described_by"], named["use"]), (FROM_AI, CHECK))
        self.assertEqual(named["file"], before)  # Looking at a picture never renames it.
        self.assertEqual(named["alt"], "红黑格子衬衫平铺")
        self.assertEqual(named["english"], "red-black-plaid-shirt-cccccccc.jpg")
        self.assertEqual(pictures.export(out)["todo"], 0)

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
        from fetch_website import PageParser, normalize_crawl_url, picture_host, sections

        parser = PageParser()
        parser.feed("<h1>Wall Tapestry</h1><p>Woven.</p><img src='/a.jpg' alt='Elephant tapestry'>"
                    "<h2>Factory</h2><p>Looms.</p><img data-src='/b.jpg' src='data:,'><nav><img src='/logo.png'></nav>"
                    "<div class='faq-item'><strong>Q: What is your MOQ?</strong><p>A: 100 pcs per style.</p></div>")
        parser.flush()
        owners: list = []
        units = sections(parser.blocks, owners)
        self.assertEqual(len(units), 2)
        self.assertIn("Q: What is your MOQ?", units[1]["text"])
        self.assertIn("A: 100 pcs per style.", units[1]["text"])
        self.assertEqual(normalize_crawl_url("https://example.com/faq/#top", "https://example.com/"), "https://example.com/faq/")
        self.assertEqual(normalize_crawl_url("https://example.com", "https://example.com/"), "https://example.com/")
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
