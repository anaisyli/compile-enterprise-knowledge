#!/usr/bin/env python3
"""When MinerU cannot be used, let the model read page pictures, and mark what it read.

export  renders every page that has no text yet (PDF pages, image-only slides) and every
        picture file held for review, and writes the reading rules.
collect checks the answers and stores them beside MinerU's results, one unit per page,
        marked engine "ai". The pipeline publishes them with ⚠, like any unchecked reading.

Nothing leaves the machine here: the pictures are read by the model already doing the work.
MinerU's reading wins wherever both exist.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, MANIFEST_NAME, load_json, load_jsonl, long_path, remaining_ocr_pages, write_json, write_text


FOLDER = "ai-read"
OCR_DIRECTORY = "mineru-units"
PICTURES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DPI = 110
RULES = """# AI 读图规则

这些页面或图片没有可提取的文字，MinerU 也没识别。请你逐张看图，把图里看得到的文字转写下来。

1. 只抄图里的字，逐字照抄，保留原来的大小写、拼写错误和换行；不翻译、不概括、不补充。
2. 一页有好几款产品时，按款分段：款号和它旁边的特点放在同一段，段与段之间空一行。原图没写款号的，不要套用别款的。
3. 看不清的字写 [看不清]，不要猜。
4. 图里没有文字（纯照片），text 写空字符串，note 写“没有文字”。
5. 价格、客户名也照抄，后面的规则会删掉；不要自己判断删哪些。

每张图写一行 JSON，存成本目录下的 answer-001.jsonl（可以分几个文件）：

    {"key": "<todo.jsonl 里的 key>", "text": "转写的文字", "note": ""}

写完运行 `ai_read.py collect --out <运行目录>`，再重跑流水线。转写的内容会标 ⚠“AI 读图，待核对”。
"""


def package_files(packages: list[str]) -> dict[str, dict[str, Any]]:
    found = {}
    for raw in packages:
        root = Path(raw).resolve()
        for asset in load_json(root / MANIFEST_NAME)["assets"]:
            if asset.get("path"):
                found[asset["item_id"]] = {**asset, "file": root / asset["path"]}
    return found


def pdf_of(path: Path, build: Path, key: str) -> Path | None:
    """PDFs are read as they are; slides are converted once with LibreOffice."""
    if path.suffix.casefold() == ".pdf":
        return path
    from extract_package_text import convert
    try:
        return convert(path, "pdf", build / "converted", key, 600)
    except Exception:  # No LibreOffice: the slide cannot be pictured here.
        return None


def export(build: Path, packages: list[str]) -> dict[str, Any]:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        return {"ok": False, "reason": "要先安装 PyMuPDF：python -m pip install pymupdf"}
    routes = load_json(build / "routes.json")
    files = package_files(packages)
    folder = build / FOLDER
    pictures = folder / "pages"
    pictures.mkdir(parents=True, exist_ok=True)
    todo, skipped = [], []
    for item_id, route in sorted(routes.items()):
        asset = files.get(item_id)
        if not asset or not Path(long_path(asset["file"])).is_file():
            continue
        name = route.get("display_name") or asset["file"].name
        if route.get("route") == "hold" and route.get("reason") == "image_needs_review" and asset["file"].suffix.casefold() in PICTURES:
            target = pictures / f"{item_id}-p1{asset['file'].suffix.casefold()}"
            shutil.copyfile(long_path(asset["file"]), target)
            todo.append({"key": f"{item_id}|1", "item_id": item_id, "page": 1, "name": name, "image": str(target)})
            continue
        pages = remaining_ocr_pages(build, item_id, route.get("ocr_pages") or []) if route.get("route") == "kb" else []
        if not pages:
            continue
        source = pdf_of(asset["file"], build, asset["source_hash"][:16])
        if source is None:
            skipped.append(f"{name}：没装 LibreOffice，幻灯片没法画成图")
            continue
        # Slides keep their own locator, so what was read lands under the right slide.
        unit = "slide" if asset["file"].suffix.casefold() in {".pptx", ".ppt"} else "page"
        with pymupdf.open(long_path(source)) as document:
            for page in pages:
                if page > document.page_count:
                    continue
                target = pictures / f"{item_id}-p{page}.png"
                if not target.is_file():
                    document[page - 1].get_pixmap(dpi=DPI).save(str(target))
                todo.append({"key": f"{item_id}|{page}", "item_id": item_id, "page": page, "unit": unit, "name": name, "image": str(target)})
    with (folder / "todo.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for entry in todo:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    write_text(folder / "README.md", RULES)
    return {"ok": True, "pictures": len(todo), "files": len({t["item_id"] for t in todo}), "folder": str(folder), "skipped": skipped}


def collect(build: Path) -> dict[str, Any]:
    folder = build / FOLDER
    todo = {entry["key"]: entry for entry in load_jsonl(folder / "todo.jsonl")}
    answers, rejected = {}, []
    for path in sorted(folder.glob("answer-*.jsonl")):
        for record in load_jsonl(path):
            if record.get("key") not in todo or not isinstance(record.get("text"), str):
                rejected.append(record.get("key") or "（没有 key）")
                continue
            answers[record["key"]] = record
    by_item: dict[str, list[dict[str, Any]]] = {}
    for key, record in answers.items():
        entry = todo[key]
        unit = {"locator": f"{entry.get('unit', 'page')} {entry['page']} ocr", "text": record["text"].strip(), "engine": "ai"}
        if not unit["text"]:
            unit["empty"] = True  # Looked at and found no words: no longer waiting.
        by_item.setdefault(entry["item_id"], []).append(unit)
    store_ai_units(build, by_item)
    return {"collected": len(answers), "files": len(by_item), "rejected": rejected, "left": len(set(todo) - set(answers))}


def store_ai_units(build: Path, by_item: dict[str, list[dict[str, Any]]]) -> None:
    """Put what the model read beside MinerU's results; MinerU's reading of a page wins."""
    routes = load_json(build / "routes.json")
    for item_id, units in by_item.items():
        path = build / OCR_DIRECTORY / f"{item_id}.json"
        record = load_json(path) if path.is_file() else {"item_id": item_id, "source": "ai",
                                                         "source_hash": routes.get(item_id, {}).get("source_hash", "")}
        taken = {u["locator"] for u in record.get("units", []) if u.get("engine") != "ai"}
        kept = [u for u in record.get("units", []) if u.get("engine") != "ai" or u["locator"] not in {n["locator"] for n in units}]
        record["units"] = sorted(kept + [u for u in units if u["locator"] not in taken],
                                 key=lambda u: int(u["locator"].split()[1]) if u["locator"].split()[1].isdigit() else 0)
        record["ai_read_at"] = date.today().isoformat()
        write_json(path, record)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read page pictures with the model when MinerU is not available.")
    parser.add_argument("command", choices=["export", "collect"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--package", action="append", default=[], help="Preparation package; repeat (export)")
    args = parser.parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    if args.command == "export":
        if not args.package:
            parser.error("export 要指定 --package")
        report = export(build, args.package)
    else:
        report = collect(build)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", True) and not report.get("rejected") else 1


if __name__ == "__main__":
    sys.exit(main())
