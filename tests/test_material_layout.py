#!/usr/bin/env python3
"""Where a picture is filed in 90_素材 and what it is called: by where it came from, nothing guessed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pictures import CONFIG_FREE_NAMER, doc_folder, media_folders, named_kind, product_section  # noqa: E402
from products import Matcher, catalog, clean_levels, match_folders, parts_of, place_media  # noqa: E402


def media(item_id: str, relpath: str) -> dict:
    return {"item_id": item_id, "relpath": relpath, "kind": "image"}


class LayoutTests(unittest.TestCase):
    def test_iso_counts_only_as_a_certification_number_and_only_in_a_title(self) -> None:
        self.assertIsNone(named_kind("阀门法兰按 ISO5211 标准"))
        self.assertIsNone(named_kind("ISO 5211 mounting pad"))
        self.assertEqual(named_kind("ISO 9001 质量管理体系证书"), "证书")
        self.assertEqual(named_kind("BSCI 审核报告"), "证书")
        self.assertEqual(named_kind("工厂与生产线照片"), "工厂与设备")
        self.assertIsNone(named_kind("machine washable"))  # A word on a page is not a factory.
        spec = {"title": "规格书-YFD71XA 蝶阀", "package_path": "A/产品/阀门/YFD71XA.pdf"}
        self.assertEqual(doc_folder(spec, "文档提取图"), "文档提取图/YFD71XA 蝶阀")
        certificate = {"title": "公司资料-ISO 9001 证书", "package_path": "A/资质/ISO9001.pdf"}
        self.assertEqual(doc_folder(certificate, "文档提取图"), "证书/ISO 9001 证书")
        in_folder = {"title": "公司资料-扫描件", "package_path": "A/公司/资质证书/扫描件.pdf"}
        self.assertEqual(doc_folder(in_folder, "页面图"), "证书/扫描件")

    def test_the_deepest_folder_naming_a_product_wins(self) -> None:
        # For the index's 按产品 section only; the picture stays where the client filed it.
        config = {"products": [{"name": "Wall mounted Digital Signage", "category": "广告机"},
                               {"name": "DS50系列", "category": "广告机", "aliases": ["DS50"]}]}
        files = [media("m1", "产品/广告机/Wall mounted Digital Signage/DS50系列1/白底图/1.jpg")]
        place = place_media(files, Matcher(catalog(config, files, {}, None)))["m1"]
        self.assertEqual(place["product"]["name"], "DS50系列")

    def test_in_one_folder_the_longest_name_wins(self) -> None:
        config = {"products": [{"name": "Wall mounted Digital Signage", "category": "广告机"},
                               {"name": "Outdoor Wall mounted Digital Signage", "category": "广告机"}]}
        product, level = match_folders(parts_of("产品/广告机/Outdoor Wall mounted Digital Signage/1.jpg"),
                                       Matcher(catalog(config, [], {}, None)))
        self.assertEqual((product["name"], level), ("Outdoor Wall mounted Digital Signage", 2))

    def test_package_pictures_keep_the_package_path(self) -> None:
        self.assertEqual(media_folders("产品/LED显示屏/LED显示分类图片/outdoor led/户外/1.jpg", CONFIG_FREE_NAMER),
                         ["产品", "LED显示屏", "LED显示分类图片", "outdoor led", "户外"])
        self.assertEqual(media_folders("知识库（总）/建站资料/建站资料/建站资料/产品/自助服务广告机/Self Services Kiosk/02/"
                                       "1.上架推广资料/详情页/详情2.jpg", CONFIG_FREE_NAMER),
                         ["建站资料", "产品", "自助服务广告机", "Self Services Kiosk", "上架推广资料", "详情页"])

    def test_a_very_long_package_path_keeps_its_deepest_folders(self) -> None:
        deep = "/".join(f"第{i}层目录名字比较长一些" for i in range(12)) + "/1.jpg"
        kept = media_folders(deep, CONFIG_FREE_NAMER)
        self.assertLessEqual(len(kept), 6)
        self.assertLessEqual(len("/".join(kept)), 100)
        self.assertEqual(kept[-1], "第11层目录名字比较长一些")

    def test_folders_are_tidied_without_losing_their_words(self) -> None:
        self.assertEqual(clean_levels(["01", "1.上架推广资料", "白底效果图"]), ["上架推广资料", "白底效果图"])
        self.assertEqual(clean_levels(["DS50系列", "2.规格书", "中文版"], "DS50系列"), ["规格书", "中文版"])
        self.assertEqual(clean_levels(["a", "b", "c", "d"]), ["b", "c", "d"])  # Paths stay short for Windows.
        self.assertEqual(clean_levels(["10_产品图"]), ["产品图"])
        self.assertEqual(clean_levels(["2025 ISE"]), ["2025 ISE"])  # A year is not a serial number.

    def test_a_certificate_folder_goes_to_证书_from_that_folder_down(self) -> None:
        from pictures import named_folders
        self.assertEqual(named_folders("建站资料/公司/资质证书/ISO9001.jpg", CONFIG_FREE_NAMER), ("证书", ["资质证书"]))
        self.assertEqual(named_folders("建站资料/公司/工厂照片/车间/1.jpg", CONFIG_FREE_NAMER), ("工厂与设备", ["车间"]))
        self.assertIsNone(named_folders("建站资料/产品/阀门/ISO5211/1.jpg", CONFIG_FREE_NAMER))

    def test_the_index_lists_pictures_by_product_and_what_matched_none(self) -> None:
        entries = {
            "img-1": {"file": "资料包素材/广告机/DS50系列/白底图/DS50系列-1-11111111.jpg", "product": ("广告机", "DS50系列"), "use": "可用于官网"},
            "img-2": {"file": "文档提取图/DS50 说明书/第3页-22222222.jpg", "product": ("广告机", "DS50系列"), "use": "内部"},
            "img-3": {"file": "官网图片/About Us/官网-About Us-33333333.jpg", "product": None, "use": "可用于官网"},
        }
        lines = "\n".join(product_section(entries))
        self.assertIn("| 广告机 / DS50系列 | 2 | 1 | 0 | 1 |", lines)
        self.assertIn("### 未对应产品（1 张）", lines)
        self.assertIn("- 官网图片/About Us：1 张", lines)


if __name__ == "__main__":
    unittest.main()
