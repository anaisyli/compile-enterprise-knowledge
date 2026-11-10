#!/usr/bin/env python3
"""建站资料视图: the client's material laid out by website page."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import site_view  # noqa: E402
from site_view import ROOT as SITE, Products, round_base, write_site  # noqa: E402

BASE = "知识库（总）/建站资料/产品"


def media(item_id: str, relpath: str, kind: str = "image", **extra) -> dict:
    return {"item_id": item_id, "kind": kind, "relpath": relpath, "package": "D:/包", "package_path": f"A/{relpath}",
            "bytes": 3_000_000, "status": "media_kept", **extra}


class SiteViewTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.build = Path(folder.name)
        self.media = [
            media("m1", f"{BASE}/广告机/Wall mounted Digital Signage/DS50系列1/白底图/1.jpg", id="img-1111111111111111.jpg"),
            media("m2", f"{BASE}/广告机/Wall mounted Digital Signage/DS50系列2/实拍图/2.jpg", id="img-2222222222222222.jpg"),
            media("m3", f"{BASE}/广告机/Wall mounted Digital Signage/DS20/介绍.mp4", kind="video"),
            media("m4", f"{BASE}/自助服务广告机/Self Services Kiosk/01/1.上架推广资料/3.jpg", id="img-3333333333333333.jpg"),
            media("m5", "知识库（总）/建站资料/工厂照片/4.jpg", id="img-4444444444444444.jpg"),
            media("m6", "知识库（总）/建站资料/案例，客户合作/案例/5.jpg", id="img-5555555555555555.jpg"),
            media("m7", "知识库（总）/杂项/6.jpg", id="img-6666666666666666.jpg"),
        ]
        self.pictures = {m["id"]: {"file": f"资料包素材/x/{m['item_id']}-{m['id'][4:12]}.jpg", "alt": m["item_id"], "use": "可用于官网",
                                   "doc_id": "", "group": ""} for m in self.media if m.get("id")}
        self.docs = {"D1": {"doc_id": "D1", "item_id": "d1", "title": "公司资料-DS50 Series 98-inch Specifications", "file_name": "规格书.md",
                            "folder": "公司资料", "package_path": "B/知识库（总）/规格书/DS50 Series 98-inch Specifications.pdf"},
                     "D2": {"doc_id": "D2", "item_id": "d2", "title": "产品画册-2026", "file_name": "画册.md", "folder": "产品画册",
                            "package_path": "A/画册/2026.pdf"}}
        self.routes = {"d1": {"source_relpath": "知识库（总）/规格书/DS50 Series 98-inch Specifications.pdf", "route": "kb"},
                       "d2": {"source_relpath": "知识库（总）/画册/2026.pdf", "route": "kb"},
                       "w1": {"source_relpath": f"{BASE}/广告机/Wall mounted Digital Signage/DS20/DS20规格书.docx", "route": "hold",
                              "reason": "awaiting_sample", "display_name": "DS20规格书.docx", "package": "D:/包",
                              "path": f"B/{BASE}/广告机/Wall mounted Digital Signage/DS20/DS20规格书.docx"},
                       "e1": {"source_relpath": "知识库（总）/Acme 报价.xlsx", "route": "skip", "reason": "excluded", "decision": "E"}}

    def build_site(self, config: dict | None = None) -> Path:
        write_site(self.build, config or {}, self.routes, self.docs, ["D1", "D2"], {"D1": "有效", "D2": "有效"},
                   {"D1": "file:///D:/包/审计记录/按知识库名字/规格书.pdf"}, self.media, self.pictures, re.compile("Acme"), lambda s: s)
        return self.build / "vault" / SITE

    def test_rounds_of_one_product_are_one_page_and_models_stay_apart(self) -> None:
        self.assertEqual(round_base("DS50系列1"), "DS50系列")
        self.assertEqual(round_base("DS20"), "DS20")  # A model number is not a round.
        site = self.build_site()
        page = (site / "产品" / "建站-产品-广告机-Wall mounted Digital Signage-DS50系列.md").read_text(encoding="utf-8")
        self.assertIn("客户分 2 批给的资料：DS50系列1、DS50系列2", page)
        self.assertIn("### 白底图（1 张）", page)
        self.assertIn("### 实物图（1 张）", page)
        # The spec sheet filed elsewhere finds its product by the model code in its name.
        self.assertIn("[[规格书]] | 有效 | [打开原件](file:///D:/包/审计记录/按知识库名字/规格书.pdf)", page)
        ds20 = (site / "产品" / "建站-产品-广告机-Wall mounted Digital Signage-DS20.md").read_text(encoding="utf-8")
        self.assertIn("介绍.mp4", ds20)
        self.assertIn("| DS20规格书.docx | 等样例确认后提取 |", ds20)  # Related but not processed yet, with why.

    def test_sections_are_bilingual_and_nothing_is_forced(self) -> None:
        site = self.build_site()
        names = {p.name for p in site.glob("*.md")}
        for name in ("建站-首页 Home.md", "建站-关于我们 About Us.md", "建站-关于我们-工厂与生产 Factory.md", "建站-案例 Cases.md",
                     "建站-资源中心 Resources.md", "建站-FAQ.md", "建站-联系我们 Contact Us.md", "建站-待归类 To Sort.md", "00_建站资料总览.md"):
            self.assertIn(name, names)
        self.assertIn("4.jpg", "".join((site / "建站-关于我们-工厂与生产 Factory.md").read_text(encoding="utf-8")) or "m5")
        self.assertIn("m5-", (site / "建站-关于我们-工厂与生产 Factory.md").read_text(encoding="utf-8"))
        self.assertIn("m6-", (site / "建站-案例 Cases.md").read_text(encoding="utf-8"))
        unsorted = (site / "建站-待归类 To Sort.md").read_text(encoding="utf-8")
        self.assertIn("m7-", unsorted)
        self.assertIn("没有产品名或栏目名", unsorted)
        self.assertIn("[[画册]]", (site / "建站-资源中心 Resources.md").read_text(encoding="utf-8"))
        everything = "\n".join(p.read_text(encoding="utf-8") for p in site.rglob("*.md"))
        self.assertNotIn("Acme", everything)  # Excluded files and customer names stay out.

    def test_numbered_folders_are_asked_about_and_config_names_decide(self) -> None:
        site = self.build_site()
        overview = (site / "00_建站资料总览.md").read_text(encoding="utf-8")
        self.assertIn("编号文件夹 01：是不同型号，还是同一产品分几批给的", overview)
        site = self.build_site({"products": [{"name": "立式点餐机", "category": "自助服务广告机", "sub": "Self Services Kiosk",
                                               "aliases": ["01"]}]})
        self.assertTrue((site / "产品" / "建站-产品-自助服务广告机-Self Services Kiosk-立式点餐机.md").is_file())

    def test_a_person_renames_levels_without_flattening_and_names_whole_folders_for_sections(self) -> None:
        self.media.append(media("m8", f"{BASE}/会议一体机/Interactive flat panel/ST Sceries/白底图/8.jpg", id="img-8888888888888888.jpg"))
        self.media.append(media("m9", "知识库（总）/页面图片/Smart Board Manufacturer Smartline/9.jpg", id="img-9999999999999999.jpg"))
        self.media.append(media("m10", "知识库（总）/页面图片/About Us-Smart Board Manufacturer Smartline/10.jpg", id="img-aaaaaaaaaaaaaaaa.jpg"))
        for m in self.media[-3:]:
            self.pictures[m["id"]] = {"file": f"资料包素材/x/{m['item_id']}-{m['id'][4:12]}.jpg", "alt": "", "use": "可用于官网", "doc_id": "", "group": ""}
        site = self.build_site({"products": [{"name": "交互平板", "category": "会议一体机", "aliases": ["Interactive flat panel"]}],
                                "site_sections": {"home": ["Smart Board Manufacturer Smartline"]}})
        self.assertTrue((site / "产品" / "建站-产品-会议一体机-交互平板-ST Sceries.md").is_file())  # Renamed, series kept.
        self.assertIn("m9-", (site / "建站-首页 Home.md").read_text(encoding="utf-8"))
        # Only the folder with exactly that name: “About Us-<site name>” still goes by the rules.
        self.assertNotIn("m10-", (site / "建站-首页 Home.md").read_text(encoding="utf-8"))
        self.assertIn("m10-", (site / "建站-关于我们-企业介绍 Company Profile.md").read_text(encoding="utf-8"))

    def test_a_numbered_folder_named_exactly_says_who_named_it(self) -> None:
        self.media.append(media("m11", "知识库（总）/杂项/微信图片_20260101/7.jpg", id="img-bbbbbbbbbbbbbbbb.jpg"))
        self.pictures["img-bbbbbbbbbbbbbbbb.jpg"] = {"file": "资料包素材/x/m11-bbbbbbbb.jpg", "alt": "", "use": "可用于官网", "doc_id": "", "group": ""}
        site = self.build_site({"products": [{"name": "01 立式自助机", "category": "自助服务广告机", "sub": "Self Services Kiosk",
                                               "folders": ["01"], "note": "AI 根据文件名识别的名称，待确认"}]})
        page = (site / "产品" / "建站-产品-自助服务广告机-Self Services Kiosk-01 立式自助机.md").read_text(encoding="utf-8")
        self.assertIn("名称说明：AI 根据文件名识别的名称，待确认（原文件夹：01）", page)
        self.assertIn("m4-", page)
        overview = (site / "00_建站资料总览.md").read_text(encoding="utf-8")
        self.assertIn("01 立式自助机]]（AI 根据文件名识别的名称，待确认）", overview)
        self.assertNotIn("编号文件夹", overview)  # Answered, so no longer asked.
        # “01” inside a date elsewhere does not pull that picture to the kiosk.
        self.assertIn("m11-", (site / "建站-待归类 To Sort.md").read_text(encoding="utf-8"))

    def test_home_candidates_wait_for_a_person(self) -> None:
        out = self.build.parent / (self.build.name + "-run")
        build = out / "04_知识库构建"
        build.mkdir(parents=True)
        (build / "pictures.json").write_text(json.dumps({"img-1111111111111111.jpg": {
            "file": "资料包素材/广告机/Wall mounted Digital Signage/白底图/a-11111111.jpg", "use": "可用于官网"}}), encoding="utf-8")
        self.assertEqual(site_view.export_home(out)["todo"], 1)
        (build / "site-home" / "answers.jsonl").write_text(json.dumps({"id": "img-1111111111111111.jpg", "reason": "白底完整"},
                                                                       ensure_ascii=False) + "\n", encoding="utf-8")
        site_view.collect_home(out)
        (self.build / "site-home.json").write_text((build / "site-home.json").read_text(encoding="utf-8"), encoding="utf-8")
        home = (self.build_site() / "建站-首页 Home.md").read_text(encoding="utf-8")
        self.assertIn("候选，待确认：白底完整", home)

    def test_update_writes_only_the_view_into_a_published_vault(self) -> None:
        out = self.build.parent / (self.build.name + "-upd")
        build = out / "04_知识库构建"
        build.mkdir(parents=True)
        for name, value in (("routes.json", self.routes), ("published.json", {"D1": "公司资料/规格书.md", "D2": "产品画册/画册.md"}),
                            ("statuses.json", {"D1": "有效", "D2": "有效"}), ("media.json", self.media), ("pictures.json", self.pictures),
                            ("guard-terms.json", {"hard": ["Acme"], "soft": []})):
            (build / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        (build / "docs.jsonl").write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in self.docs.values()), encoding="utf-8")
        vault = self.build.parent / (self.build.name + "-vault")
        receipt = vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
        receipt.parent.mkdir(parents=True)
        source = vault / "02_源资料/10_净化原文/公司资料/规格书.md"
        source.parent.mkdir(parents=True)
        source.write_text("原文不动", encoding="utf-8")
        receipt.write_text(json.dumps({"files": {}}), encoding="utf-8")
        report = site_view.update(out, vault)
        self.assertGreater(report["pages"], 10)
        self.assertTrue((vault / SITE / "00_建站资料总览.md").is_file())
        self.assertEqual(source.read_text(encoding="utf-8"), "原文不动")  # Nothing outside the view is touched.
        files = json.loads(receipt.read_text(encoding="utf-8"))["files"]
        self.assertIn(f"{SITE}/00_建站资料总览.md", files)
        # A page a person edited is not overwritten.
        edited = vault / SITE / "建站-FAQ.md"
        edited.write_text(edited.read_text(encoding="utf-8") + "\n人写的补充\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            site_view.update(out, vault)


if __name__ == "__main__":
    unittest.main()
