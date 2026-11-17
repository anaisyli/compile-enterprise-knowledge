"""Navigation must select live notes and never invent a source-page heading."""
import sys
import tempfile
import json
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from governance import CLOSED_STAGES, merge_ledger, parse_ledger, render_ledger
from pictures import retained_record
from refresh_navigation import Links, obsidian_prefix, deduplicate_term_record, status_page_update_only, archived_status_timestamp_drift, ARCHIVE
from publish_vault import normalized_hash


class NavigationTests(unittest.TestCase):
    def test_legacy_timestamp_drift_requires_matching_archive_and_unchanged_content(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            key = "01_工作台/00_知识库总览.md"
            old = vault / ARCHIVE / "20261112-navigation" / key
            current = vault / key
            old.parent.mkdir(parents=True)
            current.parent.mkdir(parents=True)
            text = "<!-- compile-enterprise-knowledge -->\n更新于 2026-11-12 15:05；最近发布：20261112-150423。\n## 内容\n"
            old.write_text(text, encoding="utf-8")
            digest = normalized_hash(old)
            current.write_text(text.replace("15:05", "15:06"), encoding="utf-8")
            self.assertTrue(archived_status_timestamp_drift(vault, key, digest))
            self.assertFalse(archived_status_timestamp_drift(vault, key, "unmatched"))
            current.write_text(text.replace("## 内容", "## 人工内容"), encoding="utf-8")
            self.assertFalse(archived_status_timestamp_drift(vault, key, digest))
            self.assertFalse(archived_status_timestamp_drift(vault, "other.md", digest))

    def test_status_refresh_accepts_only_a_generated_timestamp_change(self):
        old = "<!-- compile-enterprise-knowledge -->\n更新于 2026-11-12 15:05；最近发布：20261112-150423。\n\n## 内容\n"
        new = old.replace("15:05", "15:06")
        self.assertTrue(status_page_update_only(old, new))
        self.assertFalse(status_page_update_only(old, new.replace("## 内容", "## 手工修改")))

    def test_empty_machine_table_keeps_resolved_history_and_human_notes(self):
        old = render_ledger("CUS", "台账", "用途", "P-999", "2026-11-01", [
            {"id": "P-999-CUS-001", "row": ["问题", "1 份", "已发客户", "负责人"], "details": [], "updated": "2026-11-01"}])
        old += "\n#### 处理记录\n\n- 客户已回复\n"
        empty = render_ledger("CUS", "台账", "用途", "P-999", "2026-11-02", [])
        merged = merge_ledger(old, empty)
        self.assertIn("系统已解决，待确认", merged)
        self.assertIn("负责人", merged)
        self.assertIn("客户已回复", merged)
        self.assertEqual(len(parse_ledger(merged)[1]), 1)
        self.assertNotIn("| 无 |", merged)
        self.assertEqual(merged, merge_ledger(merged, empty))

    def test_live_paths_and_missing_heading_fallback_are_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "source.md"
            p.write_text("# 来源\n\n## 第 1 页\n", encoding="utf-8")
            links = Links({"02_源资料/10_净化原文/公司资料/来源.md": p})
            text = "| [[来源#第 2 页|来源 · 第 2 页]] |\n[[来源#第 1 页|第一]]\n"
            rewritten = links.rewrite(text)
            self.assertIn("公司资料/来源\\|来源 · 第 2 页（该页未生成段落，打开资料）", rewritten)
            self.assertIn("公司资料/来源#第 1 页|第一", rewritten)
            self.assertEqual(rewritten, links.rewrite(rewritten))

    def test_ambiguous_live_names_are_not_guessed(self):
        links = Links({"a/来源.md": Path("a"), "b/来源.md": Path("b")})
        self.assertEqual(links.rewrite("[[来源]]\n"), "[[来源]]\n")

    def test_table_formatter_spaces_do_not_become_part_of_the_link_target(self):
        links = Links({"02_源资料/来源.md": Path("source")}, "P-010_公司知识库/")
        text = "| [[P-010_公司知识库/02_源资料/来源\\ | 来源]] |\n"
        expected = "| [[P-010_公司知识库/02_源资料/来源\\|来源]] |\n"
        self.assertEqual(links.rewrite(text), expected)
        self.assertEqual(links.rewrite(expected), expected)

    def test_nested_project_paths_start_at_the_actual_obsidian_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".obsidian").mkdir()
            project = root / "P-010_公司知识库"
            project.mkdir()
            self.assertEqual(obsidian_prefix(project), "P-010_公司知识库/")
            links = Links({"01_工作台/总览.md": Path("x")}, obsidian_prefix(project))
            fixed = links.rewrite("[[01_工作台/总览|总览]]\n")
            self.assertEqual(fixed, "[[P-010_公司知识库/01_工作台/总览|总览]]\n")
            self.assertEqual(fixed, links.rewrite(fixed))
            (project / ".obsidian").mkdir()
            self.assertEqual(obsidian_prefix(project), "")

    def test_recurring_machine_issue_reopens_only_automatic_solved_stage(self):
        record = {"id": "P-999-MAT-001", "row": ["产品表判断", "1 项", "知识库专员处理中", "负责人"], "details": [], "updated": "2026-11-11"}
        current = render_ledger("MAT", "台账", "用途", "P-999", "2026-11-11", [record])
        previous = current.replace("知识库专员处理中", "系统已解决，待确认")
        self.assertIn("知识库专员处理中", merge_ledger(previous, current))
        closed = current.replace("知识库专员处理中", "已关闭")
        self.assertIn("已关闭", merge_ledger(closed, current))
        legacy_closed = current.replace("知识库专员处理中", "已核实，可结案")
        self.assertIn("已核实，可结案", CLOSED_STAGES)
        self.assertIn("已核实，可结案", merge_ledger(legacy_closed, current))
        # Same count, different detail fingerprint -> reopens automatically.
        changed = render_ledger("MAT", "台账", "用途", "P-999", "2026-11-12", [
            {**record, "details": ["- 新出现的异常项"]}])
        self.assertIn("知识库专员处理中（有新增）", merge_ledger(closed, changed))

    def test_obsidian_short_table_alignment_is_only_formatting(self):
        with tempfile.TemporaryDirectory() as folder:
            original, aligned = Path(folder) / "one.md", Path(folder) / "two.md"
            original.write_text("| 类别 | 份数 |\n| --- | ---: |\n| 公司 | 12 |\n", encoding="utf-8")
            aligned.write_text("| 类别   |  份数 |\n| ---- | --: |\n| 公司 |  12 |\n", encoding="utf-8")
            self.assertEqual(normalized_hash(original), normalized_hash(aligned))

    def test_term_risk_count_uses_case_insensitive_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            build = Path(folder)
            (build / "docs.jsonl").write_text(json.dumps({"doc_id": "d1", "file_name": "来源.md", "title": "来源", "tier": "T2"}) + "\n", encoding="utf-8")
            (build / "published.json").write_text(json.dumps({"d1": "来源.md"}), encoding="utf-8")
            (build / "blocks.jsonl").write_text(json.dumps({"doc_id": "d1", "status": "flagged", "flags": [{"type": "unverified_name", "terms": ["Panel", "PANEL", "panel"]}]}) + "\n", encoding="utf-8")
            records = [{"row": ["3 个没核实过的词出现在已生效资料里", "3 个词"], "details": ["| 词 | 出现在 |"]}]
            fixed = deduplicate_term_record(records, build)
            self.assertEqual(fixed[0]["row"][1], "1 个词")

    def test_cad_is_usable_original_drawing(self):
        record = retained_record({"relpath": "产品/1608图/1608外观图.dwg", "kind": "other", "status": "media_kept", "bytes": 2048})
        self.assertEqual(record["kind"], "图纸")
        self.assertEqual(record["status"], "已确认可用")
        self.assertIn("原位置", record["note"])

    def test_sheet_blocks_keeps_sheet_name_and_fills_merged_cells_and_images(self):
        from extract_blocks import sheet_blocks
        text = "A1=Item no.; B1=Model no.; C1=Description\nA2=1; B2=SL55HT; C2=Android 16 4K\nA3=2; B3=SL65HT"
        stats = {"sheets_without_header": [], "unknown_headers": [], "rows_kept": 0, "templates": {}}
        blocks = sheet_blocks(
            "DOC-1", "item1", "Interactive", "表 1 · Interactive", text, [],
            {"model no.": "style", "description": "description"}, {2: ["pic1.png"]},
            stats, [], {}, merges=["C2:C3", "D2:D3"], neutral_label="表 1",
        )
        self.assertEqual([b["group"] for b in blocks], ["表 1 · Interactive", "表 1 · Interactive"])
        self.assertEqual([b["cells"]["description"] for b in blocks], ["Android 16 4K", "Android 16 4K"])
        self.assertEqual([b["images"] for b in blocks], [["pic1.png"], ["pic1.png"]])

    def test_docx_units_repeats_vertical_merges_and_keeps_grid_span(self):
        import tempfile, zipfile
        from extract_package_text import docx_units
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:tbl>'
            '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Spec</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>Series A</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>A10</w:t></w:r></w:p></w:tc></w:tr>'
            '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>'
            '<w:tc><w:p><w:r><w:t>A20</w:t></w:r></w:p></w:tc></w:tr>'
            '</w:tbl></w:body></w:document>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = Path(tmp) / "t.docx"
            with zipfile.ZipFile(docx_path, "w") as zf:
                zf.writestr("word/document.xml", xml)
            units, _ = docx_units(docx_path)
        self.assertIn("| Series A | A10 |", units[0]["text"])
        self.assertIn("| Series A | A20 |", units[0]["text"])

    def test_render_source_adds_blank_line_before_table_and_keeps_split_table_whole(self):
        from render_kb import render_doc
        doc = {"doc_id": "DOC-1", "title": "Brochure", "tier": "T2", "module": "20_产品介绍",
               "item_id": "item1", "source_sha256": "abc"}
        blocks = [
            {"block_id": "b1", "doc_id": "DOC-1", "kind": "text", "group": "第 3 页", "locator": "page 3 ocr",
             "ocr": True, "reader": "mineru", "status": "valid", "flags": [],
             "text": "Wide viewing angle\n| Spec | A |\n| --- | --- |\n| Pitch | P1.25 |"},
            {"block_id": "b2", "doc_id": "DOC-1", "kind": "text", "group": "第 3 页", "locator": "page 3 ocr",
             "ocr": True, "reader": "mineru", "status": "valid",
             "flags": [{"type": "unverified_name", "terms": ["CCC"]}],
             "text": "| Protection | IP31 |"},
        ]
        rendered = render_doc(doc, blocks, "有效", ["来源卡片"], {}, {})
        self.assertIn("Wide viewing angle\n\n| Spec | A |", rendered)
        self.assertIn("| Pitch | P1.25 |\n| Protection | IP31 |\n\n> ⚠ 含没核实的词：CCC", rendered)


if __name__ == "__main__":
    unittest.main()
