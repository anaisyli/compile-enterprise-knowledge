#!/usr/bin/env python3
"""产品与方案素材总览: one page per product with its documents, pictures by kind, and videos."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from products import HUB, Matcher, catalog, material_folders, place_media, proposed, write_pages  # noqa: E402

BASE = "知识库（总）/建站资料/建站资料/建站资料/产品"


def media(item_id: str, relpath: str, kind: str = "image", **extra) -> dict:
    return {"item_id": item_id, "kind": kind, "relpath": relpath, "package": "D:/包", "package_path": f"A/{relpath}",
            "bytes": 2_000_000, "status": "media_kept", **extra}


class ProductTests(unittest.TestCase):
    files = [
        media("m1", f"{BASE}/广告机/Wall mounted Digital Signage/DS20/白底图/1.jpg", id="img-1111111111111111.jpg"),
        media("m2", f"{BASE}/广告机/Outdoor Wall mounted Digital Signage/实物图/2.jpg", id="img-2222222222222222.jpg"),
        media("m3", f"{BASE}/LED显示屏/LED显示分类图片/outdoor led/3.jpg", id="img-3333333333333333.jpg"),
        media("m4", f"{BASE}/LED显示屏/出货图片/4.jpg", id="img-4444444444444444.jpg"),
        media("m5", f"{BASE}/广告机/Wall mounted Digital Signage/介绍.mp4", kind="video"),
        media("m6", "视领网页图片/页面图片/Healthcare Solution-Smart Board Manufacturer Smartline/6.jpg", id="img-6666666666666666.jpg"),
        media("m7", "视领网页图片/页面图片/About Us-Smart Board Manufacturer Smartline/7.jpg"),
        media("m8", "视领网页图片/页面图片/Factory Tour-Smart Board Manufacturer Smartline/8.jpg"),
        media("m9", "视领网页图片/页面图片/Mini LED Smart Screen-Smartline/9.jpg"),
        media("m10", "视领网页图片/页面图片/OPS-Smartline/10.jpg"),
        media("m11", "视领网页图片/页面图片/Education Solution-Smart Board Manufacturer Smartline/11.jpg"),
    ]

    def test_products_come_from_the_product_folders_and_solutions_from_their_names(self) -> None:
        found = {(e["kind"], e["category"], e["name"]) for e in proposed({}, self.files, {}, None)}
        self.assertIn(("product", "广告机", "Wall mounted Digital Signage"), found)
        self.assertIn(("product", "LED显示屏", "outdoor led"), found)  # 分类图片 is skipped.
        self.assertIn(("product", "LED显示屏", "LED显示屏"), found)  # 出货图片 names no product.
        self.assertIn(("solution", "解决方案", "Healthcare Solution"), found)  # The site's tail is dropped.
        self.assertNotIn("出货图片", {name for _, _, name in found})

    def test_the_longest_name_wins_and_kinds_come_from_folders(self) -> None:
        places = place_media(self.files, Matcher(catalog({}, self.files, {}, None)))
        self.assertEqual(places["m1"]["product"]["name"], "Wall mounted Digital Signage")
        self.assertEqual(places["m2"]["product"]["name"], "Outdoor Wall mounted Digital Signage")
        self.assertEqual((places["m1"]["kind"], places["m2"]["kind"], places["m5"]["kind"]), ("白底图", "实物图", "视频"))
        self.assertEqual(places["m7"]["product"]["name"], "公司介绍")  # Company material has fixed groups.
        self.assertEqual(places["m8"]["product"]["name"], "工厂与生产")
        self.assertIsNone(places["m9"]["product"])  # A website page nobody named yet stays unplaced.
        self.assertEqual(material_folders(places["m1"], None), ["广告机", "Wall mounted Digital Signage", "白底图"])

    def test_aliases_in_the_config_join_english_and_chinese_names(self) -> None:
        config = {"products": [{"name": "壁挂广告机", "category": "广告机", "aliases": ["Wall mounted Digital Signage", "DS20"]},
                               {"name": "Mini LED 智能屏", "category": "广告机", "aliases": ["Mini LED Smart Screen"]}]}
        places = place_media(self.files, Matcher(catalog(config, self.files, {}, None)))
        self.assertEqual(places["m1"]["product"]["name"], "壁挂广告机")
        self.assertEqual(places["m9"]["product"]["name"], "Mini LED 智能屏")

    def test_each_product_gets_a_page_with_documents_pictures_and_videos(self) -> None:
        products = catalog({}, self.files, {}, None)
        match = Matcher(products)
        places = place_media(self.files, match)
        docs = {"D1": {"doc_id": "D1", "title": "公司资料-Wall mounted Digital Signage 规格书", "file_name": "规格书.md", "folder": "公司资料",
                       "package_path": "B/规格书.pdf"},
                "W1": {"doc_id": "W1", "title": "广告机-壁挂", "file_name": "官网-壁挂.md", "folder": "官网页面/广告机", "origin": "website",
                       "original_name": "Wall mounted Digital Signage-Smartline", "package_path": "https://example.com/wall"}}
        pictures = {"img-1111111111111111.jpg": {"file": "资料包素材/广告机/Wall mounted Digital Signage/白底图/x-11111111.jpg"}}
        with tempfile.TemporaryDirectory() as folder:
            build = Path(folder)
            result = write_pages(build, products, match, docs, ["D1", "W1"], {"D1": "有效", "W1": "有效"}, self.files, places,
                                 pictures, lambda s: s)
            page = (build / "vault" / HUB / "产品素材-Wall mounted Digital Signage.md").read_text(encoding="utf-8")
            overview = (build / "vault" / HUB / "00_产品与方案素材总览.md").read_text(encoding="utf-8")
        self.assertIn("[[规格书]]", page)
        self.assertIn("[[官网-壁挂]] · [原网页](https://example.com/wall)", page)
        self.assertIn("### 白底图（1 张）", page)
        self.assertIn("![[x-11111111.jpg|150]]", page)
        self.assertIn("## 视频", page)
        self.assertIn("介绍.mp4", page)
        self.assertIn("[[产品素材-Wall mounted Digital Signage|Wall mounted Digital Signage]]", overview)
        self.assertIn("## 未归类的素材文件夹", overview)
        self.assertGreater(result["pages"], 3)


if __name__ == "__main__":
    unittest.main()
