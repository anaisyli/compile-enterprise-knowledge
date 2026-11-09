#!/usr/bin/env python3
"""End-to-end checks on a small synthetic package."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MANAGE_SKILL = Path(r"D:\manage-article-knowledge\manage-article-knowledge-v0.6.4")

QUOTE_TEXT = (
    "A1=NORDTEX; C1=Date: 2026\n"
    "B3=Prepared for\n"
    "B4=Acme Fashion Traders\n"
    "A6=ITEM NO; B6=DESCRIPTION; C6=FABRIC; D6=UNIT PRICE; E6=客户; F6=内部代码\n"
    "A7=AB-100; B7=Sherpa jacket; C7=100% POLYESTER 350GSM SHERPA; D7=USD7.30; E7=Bigbuyer Oy; F7=ZZ9\n"
    "A8=AB-200; B8=Fleece shirt; C8=面料：摇粒绒 价格：15元/米含税; D8=USD6.10; E8=Bigbuyer Oy; F8=ZZ8\n"
    "A9=AB-100; B9=Sherpa jacket; C9=100% COTTON 200GSM CANVAS; D9=USD8.00; E9=Bigbuyer Oy; F9=ZZ7"
)


def asset(item_id: str, name: str, decision: str, resource: str, **extra) -> dict:
    return {
        "item_id": item_id, "display_name": name, "decision": decision, "resource_type": resource,
        "asset_kind": "document", "source_hash": (item_id * 64)[:64], "path": f"{decision}/{name}",
        "copy_status": "copied", "usable_as_fact": decision == "A" and resource == "marketing",
        "pending_client_review": False, "pending_units": [], **extra,
    }


def build_package(root: Path) -> tuple[Path, Path, Path]:
    package, text = root / "package", root / "text"
    (package / "审计记录").mkdir(parents=True)
    (package / "C-知识点").mkdir()
    text.mkdir()
    assets = [
        asset("a1", "Catalog.pdf", "A", "marketing", pending_client_review=True, pending_units=["第 3 页"]),
        asset("a2", "公司简介.pdf", "A", "marketing", usable_as_fact=False, pending_client_review=True),
        asset("b1", "Acme Fashion报价单.xlsx", "B", "product"),
        asset("c1", "2024-01-01--mail.md", "C", "business_scenario"),
        asset("d1", "scan.pdf", "D", "product"),
        asset("e1", "2024-02-02--order.md", "E", "transaction"),
        asset("x1", "rival.pdf", "A", "competitor"),
    ]
    (package / "审计记录/package-manifest.json").write_text(
        json.dumps({"assets": assets}, ensure_ascii=False), encoding="utf-8"
    )
    triage = {
        "items": [
            {"item_id": "b1", "actions": [{"locators": ["工作表!第 1–5 行（客户信息）", "D:D（价格列）"]}]},
        ]
    }
    (package / "审计记录/triage-results.json").write_text(json.dumps(triage, ensure_ascii=False), encoding="utf-8")
    points = {"knowledge_id": "k1", "knowledge_point": "公司常规做法是先水洗面料再裁剪。", "source_id": "c1",
              "locator": "邮件1", "scope": "single_communication", "usable_as_fact": True}
    (package / "C-知识点/knowledge-points.jsonl").write_text(json.dumps(points, ensure_ascii=False) + "\n", encoding="utf-8")

    units = {
        "a1": [
            {"locator": "page 1", "text": "SHERPA SERIES\nNTX S-001 SHERPA JACKET\n• MACHINE WASHABLE\nwww.nordtex.example"},
            {"locator": "page 2", "text": "Bigbuyer Oy loved this polar fleece range\n• MACHINE WASHABLE\nwww.nordtex.example"},
            {"locator": "page 3", "text": "客户案例：某北美品牌\n• MACHINE WASHABLE\nwww.nordtex.example"},
            {"locator": "page 4", "text": "Unit cost USD9.20 for winter\n• MACHINE WASHABLE\nwww.nordtex.example"},
            {"locator": "page 5", "text": "Tel: +86 138 0013 8000 sales@nordtex.example\nwww.nordtex.example"},
        ],
        "b1": [{"locator": "sheet 工作表 (visible)", "text": QUOTE_TEXT},
               {"locator": "sheet 隐藏 (hidden)", "text": "A1=ITEM NO; B1=FABRIC\nA2=SECRET-1; B2=Hidden fabric"}],
        "e1": [{"locator": "file", "text": "From: Pat Doe <pat@acmefashion.example>\nOrder note"}],
    }
    for item_id, content in units.items():
        (text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": content}, ensure_ascii=False), encoding="utf-8")
    config = root / "kb-config.json"
    config.write_text(
        json.dumps(
            {"company": "测试公司", "own_terms": ["NORDTEX", "AB"], "own_domains": ["nordtex.example"],
             "own_contacts": ["+86 138 0013 8000"], "deny_terms": ["RivalCo"], "vocabularies": ["apparel"],
             # The fixture's kinds count as confirmed; the sample review has tests of its own.
             "sample_review": False},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return package, text, config


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.package, self.text, self.config = build_package(self.root)
        self.out = self.root / "run"
        self.build = self.out / "04_知识库构建"
        self.clean = self.out / "03_净化md"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def run_pipeline(self, *extra: str) -> subprocess.CompletedProcess:
        extra = extra or ("--tiers", "T1,T2,T3,T4", "--skip-t3-gate")
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "run_pipeline.py"), "--package", str(self.package), "--text-dir", str(self.text),
             "--out", str(self.out), "--config", str(self.config), "--images", "none", *extra],
            capture_output=True, text=True, encoding="utf-8",
        )

    def verify(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "verify_kb.py"), "--package", str(self.package), "--text-dir", str(self.text),
             "--out", str(self.out), "--config", str(self.config)],
            capture_output=True, text=True, encoding="utf-8",
        )

    def published_text(self) -> str:
        pages = list(self.clean.rglob("*.md")) + list((self.build / "views").rglob("*.md"))
        return "\n".join(path.read_text(encoding="utf-8") for path in pages)

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False), encoding="utf-8")

    def write_quote(self, rows: str) -> None:
        header = "A6=ITEM NO; B6=PRODUCT; C6=FABRIC"
        self.write_units("b1", [{"locator": "sheet 工作表 (visible)", "text": f"{header}\n{rows}"}])

    def config_header(self, cell: str) -> None:
        path = self.text / "b1.json"
        path.write_text(path.read_text(encoding="utf-8").replace("C6=FABRIC", f"C6=FABRIC; {cell}"), encoding="utf-8")

    def clear_catalog_pending(self) -> None:
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"][0].update(pending_client_review=False, pending_units=[])
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def add_copy_of_a1(self) -> None:
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"].append(asset("a3", "Catalog copy.pdf", "A", "marketing"))
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.write_units("a3", [{"locator": "page 1", "text": "SHERPA SERIES NTX S-001"}])

    def ledger(self, relative: str) -> str:
        return (self.build / "vault/05_数据与审核/30_异常与待决定" / relative).read_text(encoding="utf-8")

    def decide(self, **decisions) -> None:
        (self.build / "review-decisions.json").write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")

    def test_point_waiting_for_the_client_enters_once_kept(self) -> None:
        waiting = {"knowledge_id": "k2", "knowledge_point": "北美客户案例：交期四周。", "source_id": "c1",
                   "locator": "邮件2", "scope": "single_communication", "usable_as_fact": False,
                   "pending_client_review": True}
        with (self.package / "C-知识点/knowledge-points.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(waiting, ensure_ascii=False) + "\n")
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("北美客户案例", self.published_text())

        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["client_answers"] = {"k2": "keep"}
        self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("北美客户案例：交期四周。", self.published_text())
        verified = self.verify()
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_free_text_tiers_are_published_as_pending_review_by_default(self) -> None:
        result = self.run_pipeline("--tiers", "T1,T2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for pattern in ("报价单*.md", "业务要点*.md"):
            page = next(self.clean.rglob(pattern)).read_text(encoding="utf-8")
            self.assertIn('status: "待审"', page)
        self.assertNotIn("AB-100", (self.build / "exports/blocks-public.jsonl").read_text(encoding="utf-8"))

    def test_moq_and_lead_time_in_company_prose_are_kept(self) -> None:
        # Companies print these on their own websites; only a quotation's rows are one customer's terms.
        self.clear_catalog_pending()
        self.write_units("a1", [{"locator": "page 1", "text": "支持超低门槛小MOQ试单，交期 15 天。"}, {"locator": "page 2", "text": "SHERPA JACKET"}])
        self.run_pipeline()
        self.assertIn("小MOQ", self.published_text())

    def test_only_the_sentence_with_a_price_is_removed(self) -> None:
        self.clear_catalog_pending()
        self.write_units("a1", [{"locator": "page 1", "text": "我们有自有工厂。单价 USD6.8 起。支持来图定制。价格有竞争力。"}])
        self.run_pipeline()
        text = self.published_text()
        self.assertIn("我们有自有工厂。", text)
        self.assertIn("支持来图定制。价格有竞争力。", text)
        self.assertNotIn("USD6.8", text)
        self.assertEqual(self.verify().returncode, 0)

    def test_moq_in_a_quotation_row_is_removed(self) -> None:
        self.write_quote("A7=AB-900; B7=Sherpa jacket MOQ 300; C7=100% POLYESTER 350GSM SHERPA")
        self.run_pipeline()
        self.assertNotIn("AB-900", self.published_text())

    def test_internal_package_path_is_not_published(self) -> None:
        self.run_pipeline()
        self.assertNotIn("package_path", self.published_text())

    def test_cost_words_in_company_prose_are_not_held(self) -> None:
        self.clear_catalog_pending()
        self.write_units("a1", [{"locator": "page 1", "text": "配备服装CAD制版设备，有效控制成本与交期。"}])
        self.run_pipeline()
        self.assertIn("有效控制成本与交期", self.published_text())

    def test_sales_notes_with_names_and_usage_hold_the_row(self) -> None:
        self.write_quote(
            "A7=AB-801; B7=Sherpa jacket; C7=王师傅3/20报给\n"
            "A8=AB-802; B8=Fleece shirt; C8=门幅净155 净0.265米/件"
        )
        self.run_pipeline()
        text = self.published_text()
        self.assertNotIn("AB-801", text)
        self.assertNotIn("AB-802", text)

    def test_only_product_columns_of_b_items_are_published(self) -> None:
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.published_text()
        self.assertIn("AB-100", text)
        self.assertIn("100% POLYESTER 350GSM SHERPA", text)
        for secret in ("USD7.30", "Bigbuyer", "ZZ9", "Acme", "Prepared for", "SECRET-1", "工作表"):
            self.assertNotIn(secret, text)

    def test_b_file_name_does_not_name_the_customer(self) -> None:
        self.run_pipeline()
        names = " ".join(path.name for path in self.clean.rglob("*.md"))
        self.assertIn("报价单", names)
        self.assertNotIn("Acme", names)

    def test_price_inside_a_kept_column_holds_the_row(self) -> None:
        self.run_pipeline()
        text = self.published_text()
        self.assertNotIn("15元/米", text)
        self.assertNotIn("AB-200", text)

    def test_excluded_and_third_party_items_stay_out(self) -> None:
        self.run_pipeline()
        routes = json.loads((self.build / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual(routes["e1"]["route"], "skip")
        self.assertEqual(routes["x1"]["route"], "skip")
        self.assertEqual(routes["d1"]["route"], "hold")
        # Waiting for the client no longer keeps a file out; this one simply has no text.
        self.assertNotEqual(routes["a2"]["route"], "skip")

    def test_pending_page_is_published_marked_and_listed_for_the_client(self) -> None:
        self.run_pipeline()
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("🕒 待客户确认", page)
        self.assertIn("客户案例", page)
        self.assertIn("第 3 页", self.ledger("10_待客户补充/01_待客户补充事项.md"))

    def test_customer_learned_from_a_quote_is_held_in_a_catalog(self) -> None:
        self.run_pipeline()
        self.assertNotIn("Bigbuyer", self.published_text())
        queue = (self.build / "review-queue.md").read_text(encoding="utf-8")
        self.assertIn("Bigbuyer Oy", queue)

    def test_allowing_a_term_releases_every_block_that_uses_it(self) -> None:
        self.run_pipeline()
        self.decide(allow_terms=["Bigbuyer Oy"])
        result = self.run_pipeline()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("polar fleece range", self.published_text())

    def test_keep_decision_cannot_release_a_price(self) -> None:
        self.run_pipeline()
        blocks = [json.loads(line) for line in (self.build / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
        priced = next(block for block in blocks if "USD9.20" in block["text"])
        self.decide(keep_blocks=[priced["block_id"]])
        self.run_pipeline()
        self.assertNotIn("USD9.20", self.published_text())

    def test_company_contact_details_pass(self) -> None:
        self.run_pipeline()
        self.assertIn("sales@nordtex.example", self.published_text())

    def test_repeated_specification_survives_but_a_repeated_footer_does_not(self) -> None:
        self.run_pipeline()
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("• MACHINE WASHABLE", page)
        self.assertNotIn("www.nordtex.example", page)

    def test_unknown_brand_in_a_product_row_hides_only_its_cell(self) -> None:
        # In a quotation a capitalised unknown word is usually the customer's own label.
        self.write_quote("A7=AB-100; B7=Hobbylux sherpa jacket; C7=100% POLYESTER 350GSM SHERPA")
        self.run_pipeline()
        text = self.published_text()
        self.assertNotIn("Hobbylux", text)
        self.assertIn("| 7 ⚠ | AB-100 | 100% POLYESTER 350GSM SHERPA |", text)
        self.assertIn("Hobbylux", (self.build / "review-queue.md").read_text(encoding="utf-8"))
        self.decide(allow_terms=["Hobbylux"])
        self.run_pipeline()
        self.assertIn("Hobbylux", self.published_text())
        self.assertNotIn("⚠", next(self.clean.rglob("产品表-*.md")).read_text(encoding="utf-8"))

    def test_bare_cost_and_sales_notes_hold_the_row(self) -> None:
        self.write_quote(
            "A7=AB-300; B7=Sherpa jacket; C7=面料19/m\n"
            "A8=AB-400; B8=Fleece shirt; C8=绣花：5元每件\n"
            "A9=AB-500; B9=Fleece shirt; C9=按照客人样做\n"
            "A10=AB-600; B10=36PCS/箱; C10=100% POLYESTER 200GSM FLEECE"
        )
        self.run_pipeline()
        text = self.published_text()
        for held in ("AB-300", "AB-400", "AB-500", "AB-600"):
            self.assertNotIn(held, text)

    def test_column_without_a_sheet_does_not_remove_a_product_column(self) -> None:
        self.write_quote("A7=AB-700; B7=Sherpa jacket; C7=100% POLYESTER 350GSM SHERPA; D7=Hood with zip")
        self.config_header("D6=DESCRIPTION")
        self.run_pipeline()
        self.assertIn("Hood with zip", self.published_text())

    def test_answer_to_a_held_question_is_held_too(self) -> None:
        self.clear_catalog_pending()
        self.write_units(
            "a1",
            [{"locator": "para 1", "text": "单价 USD5 能做吗？"}, {"locator": "para 2", "text": "不同品类要求不同。"}],
        )
        self.run_pipeline()
        self.assertNotIn("不同品类要求不同", self.published_text())

    def test_saved_again_copy_is_published_once(self) -> None:
        self.clear_catalog_pending()
        self.write_units("a1", [{"locator": "page 1", "text": "SHERPA SERIES NTX S-001"}])
        self.add_copy_of_a1()
        self.run_pipeline()
        self.assertEqual(len(list(self.clean.rglob("*Catalog*.md"))), 1)
        self.assertIn("same_content_as", (self.build / "coverage.csv").read_text(encoding="utf-8-sig"))

    def test_differing_fabric_stays_side_by_side(self) -> None:
        self.run_pipeline()
        master = (self.build / "exports/product-master.csv").read_text(encoding="utf-8-sig")
        self.assertIn("100% POLYESTER 350GSM SHERPA / 100% COTTON 200GSM CANVAS", master)
        issues = self.ledger("10_待客户补充/01_待客户补充事项.md")
        self.assertIn("面料写法不一致", issues)
        self.assertIn("AB-100", issues)

    def test_points_are_tier_four_and_not_public(self) -> None:
        self.run_pipeline()
        public = (self.build / "exports/blocks-public.jsonl").read_text(encoding="utf-8")
        everything = (self.build / "exports/blocks-all.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("先水洗面料", public)
        self.assertIn("先水洗面料", everything)

    def test_verifier_catches_a_price_added_to_a_finished_file(self) -> None:
        self.assertEqual(self.run_pipeline().returncode, 0)
        page = next(self.clean.rglob("*Catalog.md"))
        page.write_text(page.read_text(encoding="utf-8") + "\nUnit cost USD5.00\n", encoding="utf-8")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("price", result.stdout)

    def test_verifier_catches_rewritten_text(self) -> None:
        self.assertEqual(self.run_pipeline().returncode, 0)
        path = self.build / "blocks.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("NTX S-001 SHERPA JACKET", "NTX S-001 PREMIUM JACKET"), encoding="utf-8")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("text not in source", result.stdout)

    def test_verifier_catches_a_lost_source_line(self) -> None:
        self.assertEqual(self.run_pipeline().returncode, 0)
        path = self.build / "blocks.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("• MACHINE WASHABLE", ""), encoding="utf-8")
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn("source line lost", result.stdout)

    def test_number_near_a_contact_label_is_not_a_phone_unless_it_follows_it(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from guard_blocks import contact_hits

        self.assertEqual(contact_hits("Whatsapp:86- spec 350 gsm 2026 style 100234", set(), []), [])
        self.assertEqual(contact_hits("Tel: 0575-8551 6777", set(), []), ["phone"])

    def test_quote_rows_drop_accessory_counts_and_size_chart_caption_style(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from guard_blocks import ROW_PATTERNS, scan_block

        for text in ("7个/条", "5副/件", "11颗/件", "4副/件"):
            self.assertIsNotNone(ROW_PATTERNS["quantity"].search(text))
        row = {"group": "", "text": "尺寸表如下", "unjudged_text": "尺寸表如下",
               "kind": "row", "cells": {"style": "尺寸表如下"}, "doc_id": "DOC-test"}
        hard, _, _ = scan_block(row, None, None, set(), [], set())
        self.assertIn("invalid_style_label", hard)

    def test_company_year_review_finds_split_established_timeline_entry(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        from governance import company_facts

        blocks = [{"doc_id": "DOC-test", "tier": "T1", "status": "kept", "group": "第 8 页",
                   "text": "2003\nAnisen estab-lished\nand set up our own factory in Shaoxing City"}]
        docs = {"DOC-test": {"tier": "T1", "file_name": "company.pdf", "title": "公司资料-公司简介"}}
        self.assertIn("2003", company_facts(blocks, docs)["成立年份"])

    def test_a_package_item_with_no_route_fails_verification(self) -> None:
        self.assertEqual(self.run_pipeline().returncode, 0)
        path = self.build / "routes.json"
        routes = json.loads(path.read_text(encoding="utf-8"))
        del routes["e1"]
        path.write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.verify().returncode, 1)

    @unittest.skipUnless(MANAGE_SKILL.is_dir(), "manage-article-knowledge not installed here")
    def test_a_vault_made_by_manage_gets_one_set_of_ledgers(self) -> None:
        # This skill is the builder: manage's empty ledger templates give way to its ledgers.
        self.assertEqual(self.run_pipeline("--tiers", "T1,T2").returncode, 0)
        vault = self.root / "vaults" / "P-999_测试知识库"
        # With manage installed, its own initializer goes first and owns the workbench files.
        init = [sys.executable, str(MANAGE_SKILL / "scripts" / "initialize_project.py"), "--project", str(vault),
                "--project-name", "测试", "--source-root", str(self.clean), "--website", "https://example.com",
                "--content-owner", "测试"]
        self.assertEqual(subprocess.run(init, capture_output=True).returncode, 0)
        project = [sys.executable, str(SCRIPTS / "kb_project.py"), "--vault-root", str(self.root / "vaults"),
                   "--project-id", "P-999", "--name", "测试", "--website", "https://example.com", "--owner", "测试",
                   "--source-root", str(self.clean), "--confirm"]
        self.assertEqual(subprocess.run(project, capture_output=True).returncode, 0)
        publish = [sys.executable, str(SCRIPTS / "publish_vault.py"), "--out", str(self.out), "--vault", str(vault)]
        result = subprocess.run(publish, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ledgers = vault / "05_数据与审核/30_异常与待决定"
        self.assertFalse(list(ledgers.rglob("02_知识库构建_*.md")))
        self.assertIn("本 Skill 自动登记", (ledgers / "10_待客户补充/01_待客户补充事项.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
