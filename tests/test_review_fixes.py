#!/usr/bin/env python3
"""Compiled reference drafts, pages that borrow other categories, report marks, and the gap page."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
from curate import assign, topics_for  # noqa: E402
from sections import load_sections  # noqa: E402


class AssignTests(unittest.TestCase):
    def test_a_product_page_stays_out_of_other_categories_and_the_company_page(self) -> None:
        docs = {"bag": {"doc_id": "bag", "folder": "官网页面/Tapestry Bag", "title": "Tapestry Bag-x", "module": "20_产品介绍", "tier": "T1"},
                "jacket": {"doc_id": "jacket", "folder": "官网页面/Tapestry Jacket", "title": "Tapestry Jacket-y", "module": "20_产品介绍", "tier": "T1"}}
        docs.update({f"{c}{i}": {**docs[c], "doc_id": f"{c}{i}"} for c in ("bag", "jacket") for i in range(3)})
        topics = {t["slug"]: t for t in topics_for({}, docs, {d: "有效" for d in docs})}
        table = load_sections({})
        row = {"text": "Factory Direct Tapestry Bag 100% polyester 350gsm fabric, size 40cm"}
        self.assertIsNone(assign(row, docs["bag"], topics["cat-Tapestry Jacket"], table["product"]))
        self.assertIsNotNone(assign(row, docs["bag"], topics["cat-Tapestry Bag"], table["product"]))
        self.assertIsNone(assign(row, docs["bag"], topics["company"], table["company"]))


class ReviewFixTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-999")

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False), encoding="utf-8")

    def reference_draft(self) -> None:
        self.write_units("a2", [{"locator": f"para {i}", "text": text} for i, text in enumerate([
            "公司中英文行业知识库清单 V4 状态：内部审核版 / Internal review draft",
            "夹克参考款，沿用公开参考记录，示例公司待确认。",
            "面料 350GSM，参考供应商数据。"], start=1)])
        self.edit_config(client_answers={"a2": "keep"})

    def test_a_compiled_reference_is_marked_and_kept_out_of_the_facts(self) -> None:
        self.reference_draft()
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("公司介绍-公司简介.md")).read_text(encoding="utf-8")
        self.assertIn('status: "参考稿"', page)
        self.assertIn("参考整理稿", page)
        anm = next((self.build / "vault").rglob("01_源文与事实异常台账.md")).read_text(encoding="utf-8")
        self.assertIn("参考整理稿不是公司自己的资料", anm)

    def test_a_line_citing_a_reference_must_carry_the_mark(self) -> None:
        self.reference_draft()
        self.pipeline()
        topic = json.loads((self.build / "curation/company/topic.json").read_text(encoding="utf-8"))
        blocks = {json.loads(l)["block_id"]: json.loads(l) for l in (self.build / "blocks.jsonl").read_text(encoding="utf-8").splitlines()}
        cited = next(b for b in topic["blocks"] if blocks[b].get("reference"))
        (self.build / "curation/company/page.md").write_text(f"# 公司概况\n\n- 面料 350GSM。{{{{src:{cited}}}}}\n", encoding="utf-8")
        result = run("curate.py", "check", "--out", str(self.out))
        self.assertIn("没写 ⚠", result.stdout)

    def test_a_word_document_mentioning_a_report_marks_one_paragraph(self) -> None:
        self.write_units("a2", [{"locator": f"para {i}", "text": f"第 {i} 段：提花工艺。"} for i in range(1, 8)]
                         + [{"locator": "para 9", "text": "可提供 SGS test report。"}])
        self.edit_config(client_answers={"a2": "keep"})
        self.pipeline()
        page = next(self.clean.rglob("公司介绍-公司简介.md")).read_text(encoding="utf-8")
        self.assertEqual(page.count("检测或审核报告页"), 1)

    def test_knowledge_pages_are_filed_as_category_and_name(self) -> None:
        self.pipeline()
        topic = json.loads((self.build / "curation/products/topic.json").read_text(encoding="utf-8"))
        (self.build / "curation/products/page.md").write_text(
            f"# 示例公司产品总览：提花服饰与家纺\n\n## Catalog\n\n- 摇粒绒夹克。{{{{src:{topic['blocks'][0]}}}}}\n", encoding="utf-8")
        run("curate.py", "build", "--out", str(self.out))
        self.assertTrue(list((self.build / "vault/03_正式知识").rglob("产品-总览.md")))

    def test_a_single_catalog_line_is_not_called_thin(self) -> None:
        self.write_units("a1", [{"locator": f"page {i}", "text": f"NTX S-00{i} SHERPA JACKET\n• FULL ZIP {i}"} for i in range(1, 5)])
        self.pipeline()
        topic = json.loads((self.build / "curation/products/topic.json").read_text(encoding="utf-8"))
        cited = topic["blocks"][0]
        (self.build / "curation/products/page.md").write_text(f"# 产品总览\n\n## Catalog\n\n- 夹克。{{{{src:{cited}}}}}\n", encoding="utf-8")
        from kb_status import section_rows
        rows = {r["section"]: r["status"] for r in section_rows(self.out) if r["slug"] == "products"}
        self.assertEqual(rows["Catalog"], "有")

    def test_a_skipped_topic_gives_its_reason_once(self) -> None:
        self.edit_config(skip_topics={"cases": "没有案例原文"})
        self.pipeline()
        from kb_status import section_rows
        rows = [r for r in section_rows(self.out) if r["status"] == "决定不写"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 4)


if __name__ == "__main__":
    unittest.main()
