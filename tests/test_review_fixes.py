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
from guard_blocks import apply_reviewed_removals, deduplicate_blocks  # noqa: E402
from governance import screening_location_resolved, screening_contacts_resolved  # noqa: E402
from extract_blocks import find_header  # noqa: E402


class ProductHeaderTests(unittest.TestCase):
    def test_terminology_rows_are_not_a_product_header(self) -> None:
        rows = {1: {"A": "中文术语 / 表达", "B": "English Term / Expression", "C": "说明 / Usage"},
                2: {"A": "屏幕尺寸", "B": "Screen Size", "C": "显示屏对角线尺寸，通常以英寸表示"}}
        self.assertIsNone(find_header(rows, [], "terms", {}))

    def test_price_term_row_or_definition_column_is_not_a_glossary(self) -> None:
        quote = {1: {"A": "Price Term", "B": "FOB Ningbo"}, 3: {"A": "Item No.", "B": "Description", "C": "Unit Price"},
                 4: {"A": "S-100", "B": "Vest", "C": "3.5"}}
        self.assertEqual(find_header(quote, [], "quote", {})[0], 3)
        product = {1: {"A": "Model", "B": "Description", "C": "Definition"}, 2: {"A": "S-100", "B": "Vest", "C": "x"}}
        self.assertEqual(find_header(product, [], "products", {})[0], 1)

    def test_product_header_below_company_information_is_still_found(self) -> None:
        rows = {1: {"A": "Company Ltd"}, 3: {"A": "Model", "B": "Description", "C": "Price"},
                4: {"A": "DS50", "B": "Digital signage", "C": "100"}}
        self.assertEqual(find_header(rows, [], "products", {})[0], 3)


class ReviewedRemovalTests(unittest.TestCase):
    def test_website_product_contacts_are_checked_after_cleanup(self) -> None:
        doc = {"resource_type": "product", "folder": "官网页面/产品"}
        locations = ["第 9 行完整联系信息段落", "第 11 行完整联系信息段落"]
        blocks = [{"kind": "text", "status": "removed", "text": "Tel: +8613480721502"},
                  {"kind": "text", "status": "kept", "text": "Email: info@own.example"}]
        self.assertTrue(screening_contacts_resolved(doc, locations, blocks, {"own.example"}, []))
        self.assertTrue(screening_contacts_resolved({**doc, "folder": "官网页面"}, locations, blocks, {"own.example"}, []))
        blocks[1]["text"] = "Email: buyer@external.example"
        self.assertFalse(screening_contacts_resolved(doc, locations, blocks, {"own.example"}, []))

    def test_contacts_do_not_close_addresses_or_nonwebsite_products(self) -> None:
        blocks = [{"kind": "text", "status": "kept", "text": "Product data"}]
        website = {"resource_type": "product", "folder": "官网页面/产品"}
        self.assertFalse(screening_contacts_resolved(website, ["第 17 行完整详细地址段落"], blocks, set(), []))
        self.assertFalse(screening_contacts_resolved({"resource_type": "product"}, ["第 9 行完整联系信息段落"], blocks, set(), []))
        self.assertFalse(screening_contacts_resolved(website, ["联系信息段落"], [], set(), []))

    def test_exact_span_preserves_body_identity_and_audit(self) -> None:
        block = {"block_id": "first", "doc_id": "doc", "kind": "text", "text": "Before\nPrivate address\nAfter"}
        decision = {"block_id": "first", "source_sha256": "hash", "text": "Private address",
                    "reason": "detailed_address", "screening_locator": "line 2"}
        parts = apply_reviewed_removals([block], [decision], {"doc": "hash"})
        self.assertEqual("".join(p["text"] for p in parts), block["text"])
        self.assertEqual(parts[0]["block_id"], "first")
        self.assertEqual(parts[1]["part_of"], "first")
        self.assertEqual(parts[1]["reviewed_removal"], decision)
        self.assertFalse(screening_location_resolved("line 2", parts))
        parts[1]["status"] = "removed"
        self.assertTrue(screening_location_resolved("line 2", parts))
        for given, hashes, why in (([block], {"doc": "changed"}, "原件已变"),
                                   ([{**block, "text": block["text"] * 2}], {"doc": "hash"}, "正好出现一次")):
            skipped: list[str] = []
            self.assertEqual(apply_reviewed_removals(given, [decision], hashes, skipped), given)
            self.assertIn(why, skipped[0])

    def test_full_page_needs_every_block_removed(self) -> None:
        location = "page 1 整页删除（联系信息页）"
        blocks = [{"locator": "page 1 col 1", "status": "removed"},
                  {"locator": "page 1 col 2", "status": "kept"}]
        self.assertFalse(screening_location_resolved(location, []))
        self.assertFalse(screening_location_resolved(location, blocks))
        blocks[1]["status"] = "removed"
        self.assertTrue(screening_location_resolved(location, blocks))


class DuplicateBlockTests(unittest.TestCase):
    def test_dedup_preserves_locations_and_review_decisions(self) -> None:
        first = {"block_id": "first", "doc_id": "doc", "kind": "text", "locator": "para 1", "text": "Same words"}
        copy = {**first, "block_id": "copy"}
        other_page = {**copy, "block_id": "other", "locator": "para 2"}
        self.assertEqual(deduplicate_blocks([first, copy, other_page], set(), set()), [first, other_page])
        self.assertEqual(deduplicate_blocks([first, copy], {"first"}, set()), [first, copy])
        self.assertEqual(deduplicate_blocks([first, copy], set(), {"copy"}), [first, copy])


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
