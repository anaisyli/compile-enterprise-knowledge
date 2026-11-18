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

    def test_docx_units_preserves_merges_and_renders_html_table(self):
        import tempfile, zipfile
        from extract_blocks import document_blocks
        from extract_package_text import docx_units
        from render_kb import render_doc
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
        self.assertEqual(units[0]["merges"], ["A1:B1", "A2:A3"])
        blocks = document_blocks("DOC-W", "itemw", units)
        blocks[0]["status"] = "valid"
        doc = {"doc_id": "DOC-W", "title": "WordSpec", "tier": "T2", "module": "20_产品介绍",
               "item_id": "itemw", "source_sha256": "w1"}
        rendered = render_doc(doc, blocks, "有效", ["来源卡片"], {}, {})
        self.assertIn('<td colspan="2">Spec</td>', rendered)
        self.assertIn('<td rowspan="2">Series A</td><td>A10</td>', rendered)
        self.assertIn('<tr><td>A20</td></tr>', rendered)

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

    def test_render_doc_emits_missing_slide_picture_and_infers_drawing_model_from_title(self):
        from render_kb import PICTURE_FILES, render_doc
        PICTURE_FILES["slide4.png"] = "90_素材/页面图/slide4.png"
        doc = {"doc_id": "DOC-2", "title": "产品资料-43寸双面吊挂广告机-3568D", "tier": "T2", "module": "20_产品介绍",
               "item_id": "item2", "source_sha256": "def"}
        blocks = [
            {"block_id": "b1", "doc_id": "DOC-2", "kind": "text", "group": "第 3 张幻灯片", "locator": "slide 3",
             "status": "valid", "flags": [], "text": "Slide 3"},
            {"block_id": "b2", "doc_id": "DOC-2", "kind": "text", "group": "第 5 张幻灯片", "locator": "slide 5",
             "status": "valid", "flags": [], "dimension_drawing": True, "drawing_model_confirmed": False,
             "text": "型号待确认 的双面吊挂装配图"},
        ]
        rendered = render_doc(doc, blocks, "有效", ["来源卡片"], {}, {}, pictures={"第 4 张幻灯片": "slide4.png"})
        self.assertIn("## 第 4 张幻灯片\n\n![[90_素材/页面图/slide4.png|360]]", rendered)
        self.assertIn("43寸双面吊挂广告机-3568D（型号来自文件名，待核对） 的双面吊挂装配图，尺寸见原图", rendered)

    def test_docx_units_extracts_inline_images_and_render_doc_embeds_them(self):
        import tempfile, zipfile
        from extract_package_text import docx_units
        from render_kb import PICTURE_FILES, render_doc
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
            '<w:p><w:r><w:t>效果图：</w:t></w:r></w:p>'
            '<w:p><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>'
            '</w:body></w:document>'
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/effect.png"/>'
            '</Relationships>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            docx_path = tmp_path / "t.docx"
            assets_dir = tmp_path / "assets"
            with zipfile.ZipFile(docx_path, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
                zf.writestr("word/_rels/document.xml.rels", rels_xml)
                zf.writestr("word/media/effect.png", b"\x89PNG\r\n\x1a\n" + b"0" * 6000)
            units, facts = docx_units(docx_path, assets_dir, "k1")
        img_id = facts["page_pictures"]["1"][0]
        self.assertTrue(img_id.startswith("img-"), img_id)
        self.assertEqual(units[1]["images"], [img_id])
        PICTURE_FILES[img_id] = f"90_素材/产品/{img_id}"
        doc = {"doc_id": "DOC-3", "title": "产品资料-43寸", "tier": "T2", "module": "20_产品介绍",
               "item_id": "item3", "source_sha256": "ghi"}
        blocks = [
            {"block_id": "p1", "doc_id": "DOC-3", "kind": "text", "group": "正文", "locator": "para 1",
             "status": "valid", "flags": [], "text": "效果图："},
            {"block_id": "p2", "doc_id": "DOC-3", "kind": "text", "group": "正文", "locator": "para 2",
             "status": "valid", "flags": [], "text": "", "images": [img_id]},
        ]
        rendered = render_doc(doc, blocks, "有效", ["来源卡片"], {}, {})
        self.assertIn(f"效果图：\n\n![[90_素材/产品/{img_id}|360]]", rendered)
        self.assertIn("Touch type | Non-touch | Capacitive Touch", render_doc(
            doc, [{"block_id": "t1", "doc_id": "DOC-3", "kind": "text", "group": "第 2 页", "locator": "page 2",
                   "status": "valid", "flags": [],
                   "text": "SS50 Series\nTouch type\tNon-touch\tCapacitive Touch\nLCD Panel\nSize\t43\t55\nRes\t1080\t2160"}],
            "有效", ["来源卡片"], {}, {},
        ))

    def test_document_blocks_preserves_sheet_images_for_technical_workbooks(self):
        from extract_blocks import document_blocks
        units = [{"locator": "sheet T series (visible)", "text": "A1=Model; B1=SL55HT\nA2=Size; B2=55inch", "merges": []}]
        blocks = document_blocks("DOC-4", "item4", units, preserve_grid=True, sheet_images={"T series": {2: ["t1.png"]}})
        self.assertEqual(blocks[0]["images"], ["t1.png"])

    def test_document_blocks_does_not_promote_numbered_sentences_to_headings(self):
        from extract_blocks import document_blocks
        units = [
            {"locator": "para 1", "text": "一、产品特点"},
            {"locator": "para 2", "text": "1.1 外壳采用1-2.5mm镀锌板，喷户外专用粉末"},
            {"locator": "para 3", "text": "1.3 整机屏前保护玻璃采用6mm超白钢化玻璃"},
            {"locator": "para 4", "text": "1.5 整机采用模块化设计，方便安装维护。"},
            {"locator": "para 5", "text": "二、产品性能描述"},
        ]
        blocks = document_blocks("DOC-5", "item5", units)
        self.assertEqual([b["group"] for b in blocks], ["一、产品特点", "一、产品特点", "一、产品特点", "一、产品特点", "二、产品性能描述"])

    def test_path_uri_and_relink_handle_unc_shares_for_obsidian(self):
        from kb_common import path_uri
        from kb_relink import FILE_LINK, link_path
        unc = Path("//100.115.130.127/onlytest1/SEO-Runs/test.pdf")
        uri = path_uri(unc)
        self.assertTrue(uri.startswith("file:///%5C%5C100.115.130.127/"), uri)
        self.assertIsNotNone(FILE_LINK.fullmatch(uri))
        self.assertEqual(link_path(uri), "//100.115.130.127/onlytest1/SEO-Runs/test.pdf")
        self.assertEqual(link_path("file://100.115.130.127/onlytest1/SEO-Runs/test.pdf"), "//100.115.130.127/onlytest1/SEO-Runs/test.pdf")

    def test_html_table_expands_colspan_and_rowspan_into_every_covered_cell(self):
        from mineru_ocr import html_table
        table_html = (
            "<table>"
            "<tr><th rowspan='2'>DN</th><th colspan='2'>ISO5211</th><th colspan='2'>PN10</th></tr>"
            "<tr><th>Top Flange</th><th>F</th><th>D</th><th>K</th></tr>"
            "<tr><td>50</td><td>F05</td><td>50</td><td>165</td><td>125</td></tr>"
            "</table>"
        )
        md = html_table(table_html)
        lines = md.splitlines()
        self.assertEqual(lines[0], "| DN | ISO5211 | ISO5211 | PN10 | PN10 |")
        self.assertEqual(lines[2], "| DN | Top Flange | F | D | K |")
        self.assertEqual(lines[3], "| 50 | F05 | 50 | 165 | 125 |")

    def test_mineru_extract_persists_batch_id_and_reuses_it_after_download_failure(self):
        import io, tempfile, zipfile
        from mineru_client import MinerUError, extract
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("full.md", "hello")
        valid_zip = buf.getvalue()

        class FakeApi:
            def __init__(self):
                self.submits = 0
                self.waits = 0
                self.downloads = 0

            def submit(self, source, options):
                self.submits += 1
                return "batch-123"

            def wait(self, batch_id, timeout):
                self.waits += 1
                assert batch_id == "batch-123"
                return {"state": "done", "full_zip_url": "https://example.com/r.zip"}

            def download(self, url):
                self.downloads += 1
                if self.downloads == 1:
                    raise MinerUError("结果下载失败：IncompleteRead", "result-download", retryable=True)
                return valid_zip

        api = FakeApi()
        opts = {"model": "vlm", "ocr": True, "table": True, "language": "ch"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "a.pdf"
            src.write_bytes(b"%PDF-1.4")
            task_file = root / "a.task.json"
            with self.assertRaises(MinerUError):
                extract(api, src, root / "out1", opts, task_file=task_file)
            self.assertTrue(task_file.is_file())
            self.assertEqual(api.submits, 1)
            members = extract(api, src, root / "out2", opts, task_file=task_file)
            self.assertEqual(members, ["full.md"])
            self.assertEqual(api.submits, 1)
            self.assertEqual(api.waits, 2)
            self.assertFalse(task_file.is_file())


if __name__ == "__main__":
    unittest.main()
