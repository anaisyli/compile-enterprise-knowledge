#!/usr/bin/env python3
"""Check, before a long run, that the readers this skill uses work on this machine.

Each tool is tried for real: LibreOffice converts a small workbook it writes itself,
so a sandbox that blocks it shows up here, not halfway through a package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from office import convert_office, find_converter


WORKBOOK = {
    "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" sheetId="1" r:id="r1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>check</t></is></c></row></sheetData></worksheet>',
}


def libreoffice() -> dict[str, object]:
    path = find_converter()
    if not path:
        return {"ok": False, "found": None, "fix": "装 LibreOffice；装在别处时设置环境变量 LIBREOFFICE_PATH 指向 soffice.exe"}
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "check.xlsx"
        with zipfile.ZipFile(source, "w") as archive:
            for name, text in WORKBOOK.items():
                archive.writestr(name, text)
        start = time.time()
        try:
            convert_office(source, "ods", Path(folder) / "out", "check", 180)
        except Exception as exc:  # Report what went wrong, whatever it was.
            return {"ok": False, "found": path, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "fix": "在沙盒里运行时，让 Codex 用“不受沙盒限制”的方式运行这一条，或者先在本机终端里跑一次提取；"
                           "也可以先把 .xls/.doc/.ppt 另存为 .xlsx/.docx/.pptx 再放进初筛包"}
    return {"ok": True, "found": path, "seconds": round(time.time() - start, 1)}


def module(name: str, purpose: str) -> dict[str, object]:
    ok = importlib.util.find_spec(name) is not None
    return {"ok": ok, "purpose": purpose, **({} if ok else {"fix": f"python -m pip install {name}"})}


def main() -> int:
    report = {"python": sys.version.split()[0], "libreoffice": libreoffice(),
              "pypdf": module("pypdf", "读 PDF 文字层"), "pymupdf": module("pymupdf", "画册按版面分栏读、附原页图、AI 读图")}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["libreoffice"]["ok"] and report["pypdf"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
