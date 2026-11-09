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


if __name__ == "__main__":
    unittest.main()
