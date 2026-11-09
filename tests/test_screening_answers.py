#!/usr/bin/env python3
"""Answers the client gave in the screening package's 客户确认表.xlsx reach the knowledge base."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from apply_answers import read_screening_workbook  # noqa: E402

PHOTOS = "建站资料/产品/X100/实拍图"
HEADERS = ["序号", "内容摘要", "来自哪个文件", "在文件里的位置", "需要您确认什么", "您的选择", "备注", "编号"]


def workbook(path: Path, rows: list[list[str]], shared: bool) -> None:
    """A review sheet laid out like the screening one; shared=True is how Excel saves it."""
    strings: list[str] = []

    def cell(ref: str, text: str) -> str:
        if not text:
            return f'<c r="{ref}"/>'
        if shared:
            strings.append(text)
            return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'

    table = [["X100 资料确认表"], ["请在“您的选择”一列下拉选择"], HEADERS, *rows]
    body = "".join(
        f'<row r="{n}">' + "".join(cell(f"{chr(65 + i)}{n}", v) for i, v in enumerate(values)) + "</row>"
        for n, values in enumerate(table, start=1)
    )
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{body}</sheetData></worksheet>')
        if shared:
            items = "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
            archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}">{items}</sst>')


class ScreeningAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.package = self.root / "package"
        (self.package / "审计记录").mkdir(parents=True)
        (self.package / "C-知识点").mkdir()
        assets = [
            {"item_id": "doc-whole", "pending_client_review": True, "pending_units": [], "source_hash": "h1"},
            # The same file in a second folder: the workbook asked about it only once.
            {"item_id": "doc-whole-copy", "pending_client_review": True, "pending_units": [], "source_hash": "h1"},
            {"item_id": "doc-part", "pending_client_review": True, "pending_units": ["第 3 页", "第 4 页"]},
            {"item_id": "img-1", "pending_client_review": True, "review_group": PHOTOS},
            {"item_id": "img-2", "pending_client_review": True, "review_group": PHOTOS},
        ]
        (self.package / "审计记录/package-manifest.json").write_text(json.dumps({"assets": assets}), encoding="utf-8")
        point = {"knowledge_id": "knowledge-0002", "knowledge_point": "案例", "pending_client_review": True}
        (self.package / "C-知识点/knowledge-points.jsonl").write_text(json.dumps(point) + "\n", encoding="utf-8")
        self.config = self.root / "kb-config.json"
        self.config.write_text(json.dumps({"client_answers": {"older": "keep"}}), encoding="utf-8")
        self.rows = [
            ["1", "公司简介里的客户名", "公司简介.pdf", "整份文件", "可以用吗？", "保留", "", "doc-whole#1"],
            ["2", "北美案例", "案例.docx", "第 3 页", "可以写进官网吗？", "不用", "", "doc-part#1"],
            ["3", "欧洲案例", "案例.docx", "第 4 页", "需要匿名吗？", "需要修改", "去掉公司名", "doc-part#2"],
            ["4", "现场照片", f"{PHOTOS}（文件夹，2 个文件）", "整个文件夹", "能用吗？", "保留", "先都保留着", f"folder:{PHOTOS}"],
            ["5", "案例要点", "mail.md", "邮件3", "能用吗？", "保留", "", "knowledge-0002"],
            ["6", "没回答的", "x.pdf", "整份文件", "能用吗？", "", "", "doc-other#1"],
        ]

    def apply(self, shared: bool) -> dict:
        workbook(self.package / "客户确认表.xlsx", self.rows, shared)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "apply_answers.py"), "--out", str(self.root / "run"),
             "--config", str(self.config), "--answers", str(self.package / "客户确认表.xlsx")],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_answers_from_the_screening_workbook_go_into_the_config(self) -> None:
        for shared in (False, True):
            with self.subTest(saved_by_excel=shared):
                report = self.apply(shared)
                config = json.loads(self.config.read_text(encoding="utf-8"))
                answers = config["client_answers"]
                self.assertEqual(answers["doc-whole"], "keep")
                self.assertEqual(answers["doc-whole-copy"], "keep")
                self.assertEqual({answers["img-1"], answers["img-2"]}, {"keep"})
                self.assertEqual(answers["knowledge-0002"], "keep")
                self.assertEqual(answers["older"], "keep")
                self.assertNotIn("doc-part", answers)
                self.assertEqual(config["client_answers_units"]["doc-part"], {"第 3 页": "drop"})
                self.assertEqual(report["applied"], 4)
                self.assertEqual(report["unclear"], [])

    def test_remarks_and_changes_are_kept_for_a_person(self) -> None:
        self.apply(shared=True)
        notes = [json.loads(line) for line in (self.root / "run/04_知识库构建/client-notes.jsonl")
                 .read_text(encoding="utf-8").splitlines()]
        self.assertEqual({n["id"] for n in notes}, {"doc-part#2", f"folder:{PHOTOS}"})
        self.assertEqual(next(n for n in notes if n["id"] == "doc-part#2")["note"], "去掉公司名")

    def test_a_number_the_package_does_not_know_is_reported(self) -> None:
        self.rows[0][5:8] = ["保留", "", "gone#1"]
        report = self.apply(shared=False)
        self.assertEqual(report["unclear"][0]["id"], "gone#1")

    def test_reader_finds_columns_by_their_headers(self) -> None:
        workbook(self.package / "客户确认表.xlsx", self.rows, shared=True)
        rows = read_screening_workbook(self.package / "客户确认表.xlsx")
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[3]["source"], f"{PHOTOS}（文件夹，2 个文件）")
        self.assertEqual(rows[2]["choice"], "需要修改")


if __name__ == "__main__":
    unittest.main()
