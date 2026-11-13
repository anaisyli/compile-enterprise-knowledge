#!/usr/bin/env python3
"""A package on its own, with no screening work folder, must still build correctly."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>')


def make_xlsx(path: Path, rows: list[list[str]]) -> None:
    cells = []
    for r, row in enumerate(rows, start=1):
        values = "".join(
            f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{value}</t></is></c>' for c, value in enumerate(row) if value
        )
        cells.append(f'<row r="{r}">{values}</row>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{SHEET_NS}" xmlns:r="{REL_NS}"><sheets>'
                                            '<sheet name="Quote" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels",
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{SHEET_NS}"><sheetData>{"".join(cells)}</sheetData></worksheet>')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PackageOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.package = root / "02_初筛包"
        self.out = root / "run"
        (self.package / "审计记录").mkdir(parents=True)
        (self.package / "C-知识点").mkdir()
        files = {
            "A-原文保留/营销宣传/公司知识库.docx": ("a1", "A", "marketing"),
            "B-待清理/Acme报价单.xlsx": ("b1", "B", "product"),
            "E-排除/交易往来/2024-01-01--order.md": ("e1", "E", "transaction"),
        }
        for relative in files:
            (self.package / relative).parent.mkdir(parents=True, exist_ok=True)
        make_docx(self.package / "A-原文保留/营销宣传/公司知识库.docx",
                  ["1 公司介绍", "公司专注户外服装与摇粒绒夹克开发。", "我们曾为 Contoso Trading Ltd 开发联名款。"])
        make_xlsx(self.package / "B-待清理/Acme报价单.xlsx", [
            ["Prepared for", "Acme"], [], ["ITEM NO", "DESCRIPTION", "FABRIC", "UNIT PRICE"],
            ["AB-100", "Sherpa jacket", "100% POLYESTER 350GSM SHERPA", "USD7.30"],
        ])
        (self.package / "E-排除/交易往来/2024-01-01--order.md").write_text(
            "From: Pat Doe <pat@contoso.example>\nContoso Trading Ltd order confirmation", encoding="utf-8")
        assets = []
        for relative, (item_id, decision, resource) in files.items():
            assets.append({"item_id": item_id, "display_name": Path(relative).name, "decision": decision,
                           "resource_type": resource, "asset_kind": "document", "path": relative, "copy_status": "copied",
                           "source_hash": sha(self.package / relative), "usable_as_fact": decision == "A",
                           "pending_client_review": False, "pending_units": []})
        self.manifest = self.package / "审计记录/package-manifest.json"
        self.manifest.write_text(json.dumps({"assets": assets}, ensure_ascii=False), encoding="utf-8")
        (self.package / "审计记录/triage-results.json").write_text(json.dumps({"items": [
            {"item_id": "b1", "actions": [{"locators": ["Quote!第 1–2 行（客户信息）"]}]}]}), encoding="utf-8")
        (self.package / "C-知识点/knowledge-points.jsonl").write_text("", encoding="utf-8")
        self.config = root / "kb-config.json"
        self.config.write_text(json.dumps({"own_terms": ["AB"], "vocabularies": ["apparel"]}), encoding="utf-8")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def pipeline(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "run_pipeline.py"), "--package", str(self.package), "--out", str(self.out),
             "--config", str(self.config), "--images", "none", *extra],
            capture_output=True, text=True, encoding="utf-8")

    def published(self) -> str:
        return "\n".join(p.read_text(encoding="utf-8") for p in (self.out / "03_净化md").rglob("*.md"))

    def test_package_alone_builds_without_a_work_folder(self) -> None:
        result = self.pipeline("--tiers", "T1,T2,T3", "--skip-t3-gate")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.published()
        self.assertIn("公司专注户外服装与摇粒绒夹克开发", text)
        self.assertIn("100% POLYESTER 350GSM SHERPA", text)
        for secret in ("USD7.30", "Acme", "Prepared for"):
            self.assertNotIn(secret, text)

    def test_names_are_still_learned_from_excluded_emails(self) -> None:
        self.pipeline()
        self.assertNotIn("Contoso", self.published())

    def test_a_copy_that_changed_after_screening_is_not_read(self) -> None:
        docx = self.package / "A-原文保留/营销宣传/公司知识库.docx"
        make_docx(docx, ["1 公司介绍", "被人改过的内容"])
        self.pipeline()
        status = json.loads((self.out / "04_知识库构建/package-text/extract-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["items"]["a1"]["status"], "hash_mismatch")
        self.assertNotIn("被人改过的内容", self.published())

    def test_changed_or_missing_package_copy_invalidates_cached_text(self) -> None:
        for problem in ("hash_mismatch", "missing_file"):
            with self.subTest(problem=problem):
                docx = self.package / "A-原文保留/营销宣传/公司知识库.docx"
                original = docx.read_bytes()
                first = self.pipeline()
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                cached = self.out / "04_知识库构建/package-text/a1.json"
                self.assertTrue(cached.is_file())
                if problem == "hash_mismatch":
                    make_docx(docx, ["被人改过的内容"])
                else:
                    docx.unlink()
                result = self.pipeline()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                build = self.out / "04_知识库构建"
                status = json.loads((build / "package-text/extract-status.json").read_text(encoding="utf-8"))
                self.assertEqual(status["items"]["a1"]["status"], problem)
                self.assertFalse(cached.exists())
                report = json.loads((build / "校验结果.json").read_text(encoding="utf-8"))
                self.assertFalse(report["verified"])
                self.assertTrue(any(f"package input {problem}: a1" in e for e in report["errors"]))
                self.assertNotIn("被人改过的内容", self.published())
                docx.write_bytes(original)

    def test_reading_almost_nothing_fails_verification(self) -> None:
        for name in ("A-原文保留/营销宣传/公司知识库.docx", "B-待清理/Acme报价单.xlsx"):
            (self.package / name).write_bytes(b"not an office file")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        for asset in manifest["assets"]:
            asset["source_hash"] = sha(self.package / asset["path"])
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.pipeline()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("读不到文字", result.stdout)

    def test_second_run_reads_nothing_again(self) -> None:
        self.pipeline()
        second = subprocess.run([sys.executable, str(SCRIPTS / "extract_package_text.py"), "--package", str(self.package),
                                 "--out", str(self.out)], capture_output=True, text=True, encoding="utf-8")
        self.assertIn('"read_now": 0', second.stdout)


if __name__ == "__main__":
    unittest.main()
