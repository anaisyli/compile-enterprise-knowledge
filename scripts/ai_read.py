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
import re
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

from kb_common import (BUILD_DIRECTORY, MANIFEST_NAME, load_json, load_jsonl, long_path, remaining_ocr_pages,
                       sha256_file, write_json, write_text)
from samples import image_method, load_decisions, unreadable_method


FOLDER = "ai-read"
OCR_DIRECTORY = "mineru-units"
PICTURES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DPI = 110
MAX_PREVIEW_EDGE = 3000
RULES = """# AI 读图规则

这些页面或图片没有可提取的文字，MinerU 也没识别。hold/no_extractable_content 的 PDF 只在原件哈希一致且样例 gate 放行时导出。请你逐张看图，把图里看得到的文字转写下来。

1. 只抄图里的字，逐字照抄，保留原来的大小写、拼写错误和换行；不翻译、不概括、不补充。
2. 一页有好几款产品时，按款分段：款号和它旁边的特点放在同一段，段与段之间空一行。原图没写款号的，不要套用别款的。
3. 看不清的字写 [看不清]，不要猜。
4. 图里没有文字（纯照片），text 写空字符串，note 写“没有文字”。
5. 价格、客户名也照抄，后面的规则会删掉；不要自己判断删哪些。
6. 产品图里屏幕上显示的示例画面（航班信息屏、示例菜单、促销语、示例广告、桌面截图）和零部件铭牌、
   机身标签上的小字不转写：那是展示用的画面或别家零件的标识，不是这份资料的内容。

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
    # A drawing PDF is intentionally an image asset, not a missing-text document.  Read the
    # current run config (the build copy may belong to an earlier run) so export cannot turn it
    # into an AI transcript before extract_blocks has routed it to media.
    config_path = build.parent / "kb-config.json"
    config = load_json(config_path) if config_path.is_file() else {}
    drawing_items = set(config.get("drawing_items", []))
    files = package_files(packages)
    folder = build / FOLDER
    pictures = folder / "pages"
    pictures.mkdir(parents=True, exist_ok=True)
    todo, skipped = [], []
    decisions = load_decisions(build)
    gate_path = build / "sample-gate.json"
    # Pictures of a kind nobody has confirmed yet: only the sample is read now.
    allowed = set(load_json(gate_path)["allowed"]) if gate_path.is_file() else None
    for item_id, route in sorted(routes.items()):
        asset = files.get(item_id)
        if not asset or not Path(long_path(asset["file"])).is_file():
            continue
        if allowed is not None and item_id not in allowed:
            continue
        name = route.get("display_name") or asset["file"].name
        if item_id in drawing_items:
            continue
        if route.get("route") == "hold" and route.get("reason") == "image_needs_review" and asset["file"].suffix.casefold() in PICTURES:
            if image_method(decisions, item_id)[0] == "reducto":
                continue  # Chosen to be read by Reducto (reducto_ocr.py run).
            target = pictures / f"{item_id}-p1{asset['file'].suffix.casefold()}"
            shutil.copyfile(long_path(asset["file"]), target)
            todo.append({"key": f"{item_id}|1", "item_id": item_id, "page": 1, "name": name, "image": str(target)})
            continue
        held_pdf = (route.get("route") == "hold" and route.get("reason") == "no_extractable_content"
                    and asset["file"].suffix.casefold() == ".pdf")
        if route.get("route") == "hold" and route.get("reason") == "no_extractable_content" and not held_pdf:
            skipped.append(f"{name}：当前只支持无文字层 PDF 的 AI 读图")
            continue
        if held_pdf and unreadable_method(decisions, item_id)[0] == "reducto":
            skipped.append(f"{name}：这份文件指定 Reducto，等 Reducto 读完再抽查")
            continue
        pages = remaining_ocr_pages(build, item_id, route.get("ocr_pages") or []) if route.get("route") == "kb" else []
        if not held_pdf and not pages:
            continue
        if held_pdf:
            expected_hash = route.get("source_hash")
            if not expected_hash or asset.get("source_hash") != expected_hash or sha256_file(asset["file"]) != expected_hash:
                skipped.append(f"{name}：原件哈希不匹配，未导出")
                continue
        source = pdf_of(asset["file"], build, asset["source_hash"][:16])
        if source is None:
            skipped.append(f"{name}：没装 LibreOffice，幻灯片没法画成图")
            continue
        # Slides keep their own locator, so what was read lands under the right slide.
        unit = "slide" if asset["file"].suffix.casefold() in {".pptx", ".ppt"} else "page"
        with pymupdf.open(long_path(source)) as document:
            if held_pdf:
                pages = list(range(1, document.page_count + 1))
            for page in pages:
                if page > document.page_count:
                    continue
                target = pictures / f"{item_id}-p{page}.png"
                page_rect = document[page - 1].rect
                scale = min(DPI / 72, MAX_PREVIEW_EDGE / max(page_rect.width, page_rect.height))
                document[page - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(str(target))
                todo.append({"key": f"{item_id}|{page}", "item_id": item_id, "page": page, "unit": unit, "name": name, "image": str(target)})
    with (folder / "todo.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for entry in todo:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    write_text(folder / "README.md", RULES)
    return {"ok": True, "pictures": len(todo), "files": len({t["item_id"] for t in todo}), "folder": str(folder), "skipped": skipped}


def collect(build: Path) -> dict[str, Any]:
    folder = build / FOLDER
    todo = {entry["key"]: entry for entry in load_jsonl(folder / "todo.jsonl")}
    routes = load_json(build / "routes.json")
    already_collected: set[str] = set()
    for item_id, route in routes.items():
        path = build / OCR_DIRECTORY / f"{item_id}.json"
        if not path.is_file():
            continue
        record = load_json(path)
        if record.get("source_hash") != route.get("source_hash"):
            continue
        for unit in record.get("units", []):
            match = re.fullmatch(r"(?:page|slide) ([1-9]\d*) ocr", unit.get("locator", ""))
            if unit.get("engine") == "ai" and match:
                already_collected.add(f"{item_id}|{match.group(1)}")
    answers, rejected = {}, []
    for path in sorted(folder.glob("answer-*.jsonl")):
        for record in load_jsonl(path):
            if record.get("key") not in todo and record.get("key") in already_collected:
                continue
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
