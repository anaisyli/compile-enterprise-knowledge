#!/usr/bin/env python3
"""Products read off the package's folders, and where their pictures are filed."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from products import Matcher, catalog, material_folders, picture_kind, place_media, proposed  # noqa: E402

BASE = "知识库（总）/建站资料/建站资料/建站资料/产品"


def media(item_id: str, relpath: str, kind: str = "image", **extra) -> dict:
    return {"item_id": item_id, "kind": kind, "relpath": relpath, "package": "D:/包", "package_path": f"A/{relpath}",
            "bytes": 2_000_000, "status": "media_kept", **extra}


class ProductTests(unittest.TestCase):
    def test_large_size_group_does_not_override_nearest_material_folder(self) -> None:
        self.assertEqual(picture_kind("DS50/大尺寸/中文版/DS50详情中文版/1.jpg", "image"), "详情图")
        self.assertEqual(picture_kind("DS50/大尺寸/中文版/1.jpg", "image"), "其他图片")
        self.assertEqual(picture_kind("DS50/尺寸图/渲染图/1.jpg", "image"), "效果图")
        self.assertEqual(picture_kind("DS50/装配图/1.png", "image"), "尺寸图")

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


if __name__ == "__main__":
    unittest.main()
