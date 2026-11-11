#!/usr/bin/env python3
"""What the second and later runs must not lose: cited passages, titles, the website, documents."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
import curate  # noqa: E402
from curate import plan  # noqa: E402
from kb_common import load_config  # noqa: E402
from kb_state import merge_pages  # noqa: E402
from sections import classify, load_sections  # noqa: E402


def block(block_id: str, doc_id: str, text: str) -> dict:
    return {"block_id": block_id, "doc_id": doc_id, "text": text, "status": "kept", "group": "第 1 页", "locator": "page 1"}


class CandidateTests(unittest.TestCase):
    docs = {"long": {"doc_id": "long", "tier": "T2", "title": "公司资料-清单", "module": "20_产品介绍", "folder": "公司资料"},
            "cat": {"doc_id": "cat", "tier": "T1", "title": "产品画册-Sherpa", "module": "20_产品介绍", "folder": "产品画册"}}
    statuses = {"long": "有效", "cat": "有效"}

    def chosen(self, topic: dict, blocks: list, keep: set = frozenset()) -> list:
        return [b for s in plan(topic, blocks, self.docs, self.statuses, load_sections({}), keep).values() for b in s["chosen"]]

    def test_one_long_document_cannot_crowd_out_the_catalogs(self) -> None:
        blocks = [block(f"l{i:011d}", "long", f"long list entry number {i}") for i in range(10)]
        blocks += [block(f"c{i:011d}", "cat", f"catalog sherpa jacket {i}") for i in range(3)]
        chosen = self.chosen({"keywords": r".", "max": 4}, blocks)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(sum(b["doc_id"] == "cat" for b in chosen), 2)

    def test_passages_the_page_cites_stay_in_after_a_rerun(self) -> None:
        blocks = [block(f"l{i:011d}", "long", f"long list entry number {i}") for i in range(10)]
        chosen = self.chosen({"keywords": r".", "max": 2}, blocks, {"l00000000009"})
        self.assertIn("l00000000009", [b["block_id"] for b in chosen])

    def test_every_product_line_gets_its_own_share(self) -> None:
        blocks = [block(f"l{i:011d}", "long", f"long list entry number {i}") for i in range(50)]
        blocks += [block(f"c{i:011d}", "cat", f"catalog sherpa jacket {i}") for i in range(3)]
        sections = plan({"by_line": True, "modules": ["20_产品介绍"]}, blocks, self.docs, self.statuses, load_sections({}))
        self.assertEqual(len(sections["Sherpa"]["chosen"]), 3)
        self.assertEqual(sections["其他产品资料"]["available"], 50)


class SectionTests(unittest.TestCase):
    def test_passages_are_sorted_into_sub_sections_by_keyword(self) -> None:
        company = load_sections({})["company"]
        self.assertIn("发展历程", classify("Established in 2004, our own factory.", company))
        self.assertIn("资质认证", classify("BSCI and OEKO-TEX certified", company))
        self.assertNotIn("质量保证", classify("our latest arrivals", company))

    def test_config_renames_and_removes_sub_sections(self) -> None:
        table = load_sections({"sections": {"company": [{"name": "销售市场", "remove": True},
                                                        {"name": "环保", "keywords": ["recycled"], "how": "问客户"}]}})
        names = [s["name"] for s in table["company"]]
        self.assertNotIn("销售市场", names)
        self.assertIn("环保", names)


class SnapshotTests(unittest.TestCase):
    def test_a_failed_crawl_does_not_empty_the_saved_website(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            saved, empty = Path(folder) / "saved.json", Path(folder) / "empty.json"
            saved.write_text(json.dumps([{"page_id": "web-1", "url": "https://a.example/x", "fetched_at": "2026-10-01"}]), encoding="utf-8")
            empty.write_text("[]", encoding="utf-8")
            merge_pages(empty, saved)
            self.assertEqual(len(json.loads(saved.read_text(encoding="utf-8"))), 1)


class RerunTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-999")
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": [
            {"locator": "page 1", "text": "SHERPA SERIES\nNTX S-001 SHERPA JACKET"},
            {"locator": "page 2", "text": "Established in 2004, we run our own factory."}]}), encoding="utf-8")
        self.vault = self.root / "vaults" / "P-999_测试知识库"

    def make_vault(self) -> None:
        args = ["--vault-root", str(self.root / "vaults"), "--project-id", "P-999", "--name", "测试", "--website",
                "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm"]
        self.assertEqual(run("kb_project.py", *args).returncode, 0)

    def publish(self, *extra: str):
        return run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault), *extra)

    def test_page_heading_names_the_page(self) -> None:
        self.pipeline()
        topic = json.loads((self.build / "curation/company/topic.json").read_text(encoding="utf-8"))
        cited = topic["blocks"][0]
        (self.build / "curation/company/page.md").write_text(f"# 公司简介\n\n## 一句话说明\n\n- 见原文。{{{{src:{cited}}}}}\n", encoding="utf-8")
        run("curate.py", "prepare", "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(json.loads((self.build / "curation/company/topic.json").read_text(encoding="utf-8"))["title"], "公司简介")
        run("curate.py", "build", "--out", str(self.out))
        built = next((self.build / "vault/03_正式知识").rglob("公司-概况.md")).read_text(encoding="utf-8")
        self.assertIn("# 公司简介", built)  # The heading is the writer's; the file name stays 类别-名称.

    def test_gap_page_lists_every_sub_section_with_what_to_do(self) -> None:
        self.pipeline()
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        gaps = (self.vault / "05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md").read_text(encoding="utf-8")
        self.assertIn("| 发展历程 | 1 段 | 1 份 | 有原文没整理 |", gaps)
        self.assertIn("| 资质认证 | 0 段 | 0 份 | 缺 | 向客户要证书扫描件或审核报告 |", gaps)
        self.assertIn("小板块：", (self.vault / "01_工作台/00_知识库总览.md").read_text(encoding="utf-8"))

    def test_written_heading_counts_as_covered(self) -> None:
        self.pipeline()
        topic = json.loads((self.build / "curation/company/topic.json").read_text(encoding="utf-8"))
        cited = topic["sections"]["发展历程"]["blocks"][0]
        (self.build / "curation/company/page.md").write_text(
            f"# 公司概况\n\n## 一句话说明\n\n- 自有工厂。{{{{src:{cited}}}}}\n\n## 发展历程\n\n- 成立于 2004 年。{{{{src:{cited}}}}}\n", encoding="utf-8")
        from kb_status import section_rows
        rows = {r["section"]: r["status"] for r in section_rows(self.out) if r["slug"] == "company"}
        self.assertEqual(rows["发展历程"], "薄弱")  # Written, but from one passage of one document.

    def test_a_big_sub_section_becomes_its_own_page(self) -> None:
        self.pipeline()
        old = curate.SPLIT_AT
        curate.SPLIT_AT = 0
        try:
            curate.prepare(self.out, load_config(self.config))
        finally:
            curate.SPLIT_AT = old
        topics = json.loads((self.build / "curation/topics.json").read_text(encoding="utf-8"))
        children = [t for t in topics if t.get("parent") == "company"]
        self.assertTrue(children)
        self.assertIn(children[0]["slug"], json.loads((self.build / "curation/company/topic.json").read_text(encoding="utf-8"))["split"].values())

    def test_citation_to_a_vanished_passage_says_so(self) -> None:
        self.pipeline()
        (self.build / "curation/company/page.md").write_text("# 公司概况\n\n- 旧说法。{{src:000000000000}}\n", encoding="utf-8")
        result = run("curate.py", "check", "--out", str(self.out))
        self.assertEqual(result.returncode, 1)
        self.assertIn("原文已不可用", result.stdout)

    def test_registered_website_with_no_pages_fails_the_check(self) -> None:
        self.edit_config(websites=[{"url": "http://127.0.0.1:9/", "role": "primary"}])
        crawl = run("fetch_website.py", "--out", str(self.out), "--config", str(self.config), "--delay", "0")
        self.assertEqual(crawl.returncode, 4)
        report = json.loads((self.build / "website/fetch-report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["failed"])
        result = self.pipeline()
        self.assertEqual(result.returncode, 1)
        self.assertIn("没有官网页面", result.stdout)
        mat = next((self.build / "vault").rglob("01_源资料处理台账.md")).read_text(encoding="utf-8")
        self.assertIn("官网没有读到的页面", mat)

    def test_many_documents_leaving_at_once_stops_the_publish(self) -> None:
        self.pipeline()
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        receipt_path = self.vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        for i in range(8):
            key = f"02_源资料/10_净化原文/官网页面/页面-{i}.md"
            path = self.vault / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'---\ndoc_id: "WEB-{i}"\ntitle: "页面-{i}"\nstatus: "有效"\n---\n\n# 页面-{i}\n', encoding="utf-8")
            from publish_vault import normalized_hash
            receipt["files"][key] = normalized_hash(path)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("变成已失效", result.stdout + result.stderr)
        self.assertIn('"withdrawn": 8', self.publish("--allow-withdraw").stdout)

    def test_renamed_document_is_moved_not_withdrawn(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        self.edit_config(doc_titles={"a1": "产品画册-Sherpa 系列"})
        self.assertEqual(self.pipeline().returncode, 0)
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"withdrawn": 0', result.stdout)
        self.assertIn('"renamed": 1', result.stdout)
        self.assertFalse((self.vault / "02_源资料/10_净化原文/产品画册/产品画册-Catalog.md").exists())

    def test_a_pdf_now_read_through_its_word_file_leaves_no_second_page(self) -> None:
        # Built before the rule: the PDF had a page of its own, named with a _xxxxxx suffix.
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"] += [{**manifest["assets"][0], "item_id": "w1", "display_name": "规格书.docx", "path": "A/规格书.docx",
                                "source_hash": "1" * 64, "pending_client_review": False, "pending_units": []},
                               {**manifest["assets"][0], "item_id": "w2", "display_name": "规格书.pdf", "path": "A/旧/规格书.pdf",
                                "source_hash": "2" * 64, "pending_client_review": False, "pending_units": []}]
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        for item, text in (("w1", "规格：350GSM，可机洗。"), ("w2", "规格：350GSM，\n可机洗。")):
            (self.text / f"{item}.json").write_text(json.dumps({"item_id": item, "units": [{"locator": "page 1", "text": text}]},
                                                               ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        self.make_vault()
        self.assertEqual(self.publish().returncode, 0)
        pages = lambda: sorted(p.name for p in (self.vault / "02_源资料/10_净化原文").rglob("*规格书*.md"))  # noqa: E731
        self.assertEqual(len(pages()), 2)
        manifest["assets"][-1]["path"] = "A/规格书.pdf"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"withdrawn": 0', result.stdout)
        self.assertEqual(len(pages()), 1, pages())

    def test_old_receipt_gives_one_warning_and_archives_once(self) -> None:
        self.pipeline()
        self.make_vault()
        self.publish()
        receipt_path = self.vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt.pop("hash_mode")
        receipt["files"] = {key: "old-raw-hash" for key in receipt["files"]}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(output["warnings"]), 1, output["warnings"])
        notes = "\n".join(p.read_text(encoding="utf-8") for p in self.vault.rglob("00_版本说明.md"))
        self.assertEqual(output["archived"], 0, notes)  # Nothing changed, so nothing needed archiving.


if __name__ == "__main__":
    unittest.main()
