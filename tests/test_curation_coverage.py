#!/usr/bin/env python3
"""Pages are written in full, and every catalog has a series page even with a website."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import curate  # noqa: E402
from kb_common import BUILD_DIRECTORY  # noqa: E402


def jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.build = self.out / BUILD_DIRECTORY
        (self.build / "curation").mkdir(parents=True)
        self.docs = [{"doc_id": f"d{i}", "title": f"提花短裤-款{i}", "tier": "T1", "file_name": f"款{i}.md"} for i in range(6)]
        self.blocks = [{"block_id": f"{i:012x}", "doc_id": f"d{i}", "status": "kept", "text": f"Tapestry shorts style {i}", "group": "Products"}
                       for i in range(6)]
        jsonl(self.build / "docs.jsonl", self.docs)
        jsonl(self.build / "blocks.jsonl", self.blocks)
        (self.build / "statuses.json").write_text(json.dumps({d["doc_id"]: "有效" for d in self.docs}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def topic(self, slug: str, page_blocks: list, **extra) -> None:
        folder = self.build / "curation" / slug
        folder.mkdir()
        topic = {"slug": slug, "title": slug, "module": "product", "blocks": [b["block_id"] for b in self.blocks], **extra}
        (folder / "topic.json").write_text(json.dumps(topic), encoding="utf-8")
        lines = [f"- 款式 {{{{src:{b['block_id']}}}}}" for b in page_blocks]
        (folder / "page.md").write_text("# 页\n\n## 一句话说明\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

    def test_a_page_citing_few_of_its_documents_is_refused_and_names_them(self) -> None:
        self.topic("thin", self.blocks[:2])
        self.topic("full", self.blocks[:3])
        report = curate.check(self.out)
        self.assertFalse(report["thin"]["ok"])
        self.assertIn("只写到 2/6 份资料", report["thin"]["errors"][-1])
        self.assertIn("提花短裤-款5", report["thin"]["errors"][-1])
        self.assertTrue(report["full"]["ok"], report["full"]["errors"])
        self.assertEqual(report["full"]["coverage"], {"given": 6, "cited": 3})

    def test_a_page_of_a_topic_no_longer_listed_is_not_built(self) -> None:
        self.topic("line-old-catalog", self.blocks[:3])
        self.topic("full", self.blocks[:3])
        (self.build / "curation" / "topics.json").write_text(json.dumps([{"slug": "full"}]), encoding="utf-8")
        self.assertEqual(list(curate.check(self.out)), ["full"])

    def test_the_overview_has_to_reach_every_product_line(self) -> None:
        ids = [b["block_id"] for b in self.blocks]
        sections = {"挂毯": {"blocks": ids[:3]}, "桌布": {"blocks": ids[3:]}}
        self.topic("products", self.blocks[:1], by_line=True, sections=sections)
        report = curate.check(self.out)["products"]
        self.assertFalse(report["ok"])
        self.assertIn("只写到 1/2 条产品线", report["errors"][-1])
        self.assertIn("桌布", report["errors"][-1])

    def test_catalogs_get_series_pages_even_with_website_categories(self) -> None:
        docs = {f"w{i}": {"doc_id": f"w{i}", "title": f"挂毯-{i}", "folder": "官网页面/挂毯"} for i in range(3)}
        docs["c1"] = {"doc_id": "c1", "title": "产品画册-2026格子系列", "folder": "产品画册"}
        statuses = {d: "有效" for d in docs}
        slugs = [t["slug"] for t in curate.topics_for({}, docs, statuses)]
        self.assertIn("cat-挂毯", slugs)
        self.assertIn("line-2026格子系列", slugs)
        # A config that already names the series does not get it twice.
        listed = {"topics": [{"slug": "line-2026格子系列", "title": "产品系列-2026格子系列", "module": "product"}]}
        self.assertEqual([t["slug"] for t in curate.topics_for(listed, docs, statuses)].count("line-2026格子系列"), 1)

    def test_a_product_list_makes_one_page_per_category_from_every_kind_of_document(self) -> None:
        config = {"products": [{"name": "DS50系列", "category": "广告机", "aliases": ["DS50系列1", "DS50"]},
                               {"name": "SS50系列", "category": "广告机", "aliases": ["SS50"]},
                               {"name": "立式自助机", "category": "自助服务广告机", "folders": ["01"]}]}
        product = "20_产品介绍"
        docs = {"cn": {"doc_id": "cn", "title": "产品画册-DS50—大尺寸广告机-中文版", "folder": "产品画册", "module": product,
                       "package_path": "A/宣传/DS50—大尺寸广告机-中文版.pptx"},
                "en": {"doc_id": "en", "title": "产品画册-DS50 Large Format-英文版", "folder": "产品画册", "module": product,
                       "package_path": "A/产品/广告机/DS50系列1/英文版/x.pptx"},
                "spec": {"doc_id": "spec", "title": "规格书-98寸数字标牌规格书", "folder": "规格书", "module": product,
                         "package_path": "B/产品/广告机/DS50系列2/规格书/98寸.pdf"},
                "kiosk": {"doc_id": "kiosk", "title": "规格书-立式点餐机", "folder": "规格书", "module": product,
                          "package_path": "B/产品/自助服务广告机/Self Services Kiosk/01/立式点餐机.docx"},
                "bare": {"doc_id": "bare", "title": "产品资料-双面吊挂", "folder": "产品资料", "module": product,
                         "package_path": "D/产品/广告机/Double-Sided/双面吊挂.docx"},
                "led": {"doc_id": "led", "title": "规格书-P2 module", "folder": "规格书", "module": product,
                        "package_path": "A/LED显示屏/P2.pdf"},
                "about": {"doc_id": "about", "title": "公司介绍-DS50 工厂", "folder": "公司介绍", "module": "10_公司概述",
                          "package_path": "A/公司/DS50 工厂.docx"}}
        places = curate.place_docs(docs, curate.product_lines(config))
        self.assertEqual(places["cn"], ("广告机", "DS50系列"))  # Its own name says DS50.
        self.assertEqual(places["en"], ("广告机", "DS50系列"))  # Its folder says DS50系列1, a round of DS50系列.
        self.assertEqual(places["spec"], ("广告机", "DS50系列"))
        self.assertEqual(places["kiosk"], ("自助服务广告机", "立式自助机"))  # A numbered folder named exactly.
        self.assertEqual(places["bare"], ("广告机", ""))  # Only the 大类 folder names it.
        self.assertIsNone(places["led"])
        for doc_id, place in places.items():
            docs[doc_id]["_product"] = place
        topics = {t["slug"]: t for t in curate.topics_for(config, docs, {d: "有效" for d in docs})}
        # No page per catalog: the two language versions of one catalog are one product's section.
        self.assertEqual(sorted(s for s in topics if s.startswith(("cat-", "line-"))), ["cat-other", "cat-广告机", "cat-自助服务广告机"])
        page = topics["cat-广告机"]
        block = {"text": "x"}
        self.assertEqual({d: curate.assign(block, docs[d], page, []) for d in docs},
                         {"cn": "DS50系列", "en": "DS50系列", "spec": "DS50系列", "kiosk": None, "bare": "其他广告机资料",
                          "led": None, "about": None})
        self.assertEqual(curate.assign(block, docs["led"], topics["cat-other"], []), "其他产品资料")
        self.assertEqual(curate.line_name(docs["spec"]), "广告机")


if __name__ == "__main__":
    unittest.main()
