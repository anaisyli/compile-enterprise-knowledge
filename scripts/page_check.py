#!/usr/bin/env python3
"""Check extracted text against the page itself, so the cleaned text is right from the start.

A text layer can miss what is printed in pictures (style codes, product names), come out
garbled when a font is broken, or leave a catalog page unclear about which code belongs
to which product. Nothing checks that against the page unless something looks at it.

plan     picks the pages worth looking at (pictures with little text, more product photos
         than codes, odd characters, pictures inside Word files, and a sample of the rest),
         draws them, and writes the extracted text beside each. Run by the pipeline.
collect  reads the model's answers. Words seen on the page but missing from the text go in
         beside MinerU's results, marked as the model's reading; broken text and unclear
         code pairings are recorded for the page and the ledgers.

MinerU, when it may be used, reads better; this checks and fills gaps, it does not replace it.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from ai_read import package_files, pdf_of, store_ai_units
from guard_blocks import HARD_PATTERNS
from kb_common import BUILD_DIRECTORY, load_config, load_json, load_jsonl, load_units, long_path, remaining_ocr_pages, write_json, write_text
from office import convert_office


FOLDER = "page-check"
RESULTS = "page-check/results.json"
DPI = 100
CHECKED_TIERS = {"T1", "T2"}
PAGED = {".pdf", ".pptx", ".ppt"}
WORD = {".docx", ".doc"}
# A picture taking this share of the page is content, not a logo.
PICTURE_SHARE = 0.04
FEW_WORDS = 400
SAMPLE_SHARE = 0.1
MIN_PICTURE_BYTES = 15_000
GENERIC_CODE = re.compile(r"\b[A-Z]{2,5}[\s-]?[A-Z]?-?\d{2,5}\b")
ODD = re.compile(r"[�-]|[À-ÿ]{3,}")
RULES = """# 看图核对规则

这些页面或图片已经提取了文字，但可能漏了图上的字、读错了字，或者看不出款号和产品的对应。请逐张看图，
和同一行 `text` 里提取出来的文字对照，回答下面三件事。只写你在图上看到的，不猜、不补充、不翻译。

1. missing：图上有、提取的文字里没有的内容，逐字照抄（款号、产品名、参数、图上的标语），按款分段。
   没有漏的写空字符串。
2. text_errors：提取的文字和图上不一样的地方（乱码、错字、缺字），简单写哪里不对。没有就写空字符串。
3. pairing：这一页有几款产品时，写清楚每个款号对应哪款（比如“左：没写款号；中：没写款号；右：ANS S-002”）。
   原图本身看不出对应关系的，unclear 写 true。只有一款或没有产品的，pairing 写空字符串。

价格、客户名也照抄，后面的规则会删掉，不要自己判断删哪些。

每张图写一行 JSON，存成本目录下的 answer-001.jsonl（可以分几个文件，可以分给几个子 Agent）：

    {"key": "<todo.jsonl 里的 key>", "missing": "", "text_errors": "", "pairing": "", "unclear": false}

写完运行 `page_check.py collect --out <运行目录>`，再重跑流水线。补进去的字会标 ⚠“AI 看图补充，待核对”。
"""


def page_text(units: list[dict[str, str]], unit: str, number: int) -> str:
    pattern = re.compile(rf"^{unit} {number}(?: col \d+)?$")
    return "\n\n".join(u["text"] for u in units if pattern.match(u["locator"]))


def code_pattern(prefixes: list[str]) -> re.Pattern[str]:
    if not prefixes:
        return GENERIC_CODE
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")[\s-]?[A-Z]?-?\d{2,5}\b")


def reasons_for(page: Any, text: str, codes: re.Pattern[str]) -> list[str]:
    area = page.rect.width * page.rect.height or 1
    pictures = [i for i in page.get_image_info() if (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) > area * PICTURE_SHARE]
    found = len(set(codes.findall(text)))
    reasons = []
    if pictures and len(text) < FEW_WORDS:
        reasons.append("图片多、文字少")
    if len(pictures) >= 2 and found < len(pictures):
        reasons.append(f"{len(pictures)} 张图、只找到 {found} 个款号")
    if ODD.search(text):
        reasons.append("文字里有乱码")
    return reasons


def word_pictures(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(long_path(path)) as archive:
        names = sorted(n for n in archive.namelist() if n.startswith("word/media/") and Path(n).suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"})
        return [(n, archive.read(n)) for n in names if archive.getinfo(n).file_size >= MIN_PICTURE_BYTES]


def plan(build: Path, packages: list[str], config: dict[str, Any], text_dirs: list[Path]) -> dict[str, Any]:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        report = {"ok": False, "reason": "要先安装 PyMuPDF：python -m pip install pymupdf"}
        write_json(build / FOLDER / "plan.json", report)
        return report
    routes = load_json(build / "routes.json")
    files = package_files(packages)
    folder = build / FOLDER
    pictures = folder / "pages"
    pictures.mkdir(parents=True, exist_ok=True)
    done = load_json(build / RESULTS) if (build / RESULTS).is_file() else {}
    codes = code_pattern(config.get("style_prefixes", []))
    rng = random.Random(20261105)
    todo, skipped = [], []
    for item_id, route in sorted(routes.items()):
        asset = files.get(item_id)
        if route.get("route") != "kb" or route.get("tier") not in CHECKED_TIERS or not asset or not Path(long_path(asset["file"])).is_file():
            continue
        suffix = asset["file"].suffix.casefold()
        units = load_units(text_dirs, item_id)
        name = route.get("display_name") or asset["file"].name
        if suffix in WORD:
            try:
                source = asset["file"] if suffix == ".docx" else convert_office(asset["file"], "docx", build / "converted", asset["source_hash"][:16], 600)
            except Exception:  # No LibreOffice: an old .doc cannot be opened here.
                skipped.append(f"{name}：没装 LibreOffice，读不了里面的图")
                continue
            for index, (member, data) in enumerate(word_pictures(source), start=1):
                target = pictures / f"{item_id}-i{index}{Path(member).suffix.casefold()}"
                if not target.is_file():
                    target.write_bytes(data)
                todo.append({"key": f"{item_id}|image {index}", "item_id": item_id, "unit": "image", "page": index, "name": name,
                             "image": str(target), "text": "", "reasons": ["Word 里的图片"]})
            continue
        if suffix not in PAGED:
            continue
        source = pdf_of(asset["file"], build, asset["source_hash"][:16])
        if source is None:
            skipped.append(f"{name}：没装 LibreOffice，幻灯片没法画成图")
            continue
        unit = "slide" if suffix in {".pptx", ".ppt"} else "page"
        waiting = set(remaining_ocr_pages(build, item_id, route.get("ocr_pages") or []))
        # A page MinerU or the model already read as a picture has been looked at.
        read_path = build / "mineru-units" / f"{item_id}.json"
        read = {int(m.group(1)) for u in (load_json(read_path).get("units", []) if read_path.is_file() else [])
                if not u.get("checked") and (m := re.match(r"(?:page|slide) (\d+)", u.get("locator", "")))}
        waiting |= read
        with pymupdf.open(long_path(source)) as document:
            chosen = []
            for number, page in enumerate(document, start=1):
                if number in waiting:
                    continue  # No text at all: ai_read.py or MinerU reads these whole.
                text = page_text(units, unit, number)
                reasons = reasons_for(page, text, codes)
                chosen.append((number, text, reasons))
            rest = [c for c in chosen if not c[2]]
            for number, text, _ in rng.sample(rest, max(1, round(len(rest) * SAMPLE_SHARE))) if rest else []:
                chosen[[c[0] for c in chosen].index(number)] = (number, text, ["抽查"])
            for number, text, reasons in chosen:
                if not reasons:
                    continue
                target = pictures / f"{item_id}-p{number}.png"
                if not target.is_file():
                    document[number - 1].get_pixmap(dpi=DPI).save(str(target))
                todo.append({"key": f"{item_id}|{unit} {number}", "item_id": item_id, "unit": unit, "page": number, "name": name,
                             "image": str(target), "text": text, "reasons": reasons})
    todo = [t for t in todo if t["key"] not in done]
    with (folder / "todo.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for entry in todo:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    write_text(folder / "README.md", RULES)
    report = {"ok": True, "to_check": len(todo), "checked": len(done), "files": len({t["item_id"] for t in todo}), "skipped": skipped}
    write_json(folder / "plan.json", report)
    return report


def clean_note(text: str) -> str:
    """A note the model writes about a page must not carry what the rules remove."""
    text = re.sub(r"\s+", " ", text or "").strip()[:300]
    return "" if any(pattern.search(text) for pattern in HARD_PATTERNS.values()) else text


def collect(build: Path) -> dict[str, Any]:
    folder = build / FOLDER
    todo = {entry["key"]: entry for entry in load_jsonl(folder / "todo.jsonl")}
    results: dict[str, Any] = load_json(build / RESULTS) if (build / RESULTS).is_file() else {}
    rejected, by_item = [], {}
    for path in sorted(folder.glob("answer-*.jsonl")):
        for record in load_jsonl(path):
            entry = todo.get(record.get("key"))
            if not entry or not all(isinstance(record.get(field, ""), str) for field in ("missing", "text_errors", "pairing")):
                rejected.append(record.get("key") or "（没有 key）")
                continue
            missing = (record.get("missing") or "").strip()
            results[entry["key"]] = {"item_id": entry["item_id"], "unit": entry["unit"], "page": entry["page"],
                                     "reasons": entry["reasons"], "added": bool(missing),
                                     "text_errors": clean_note(record.get("text_errors", "")),
                                     "pairing": clean_note(record.get("pairing", "")), "unclear": bool(record.get("unclear"))}
            if missing:
                by_item.setdefault(entry["item_id"], []).append(
                    {"locator": f"{entry['unit']} {entry['page']} ocr", "text": missing, "engine": "ai", "checked": True})
    store_ai_units(build, by_item)
    write_json(build / RESULTS, results)
    left = len(set(todo) - set(results))
    return {"collected": len(results), "added_text": sum(len(v) for v in by_item.values()), "rejected": rejected, "left": left}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check extracted text against the pages themselves.")
    parser.add_argument("command", choices=["plan", "collect"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--package", action="append", default=[], help="Preparation package; repeat (plan)")
    parser.add_argument("--text-dir", action="append", default=[], help="Extracted text units; repeat (plan)")
    parser.add_argument("--config", help="kb-config.json")
    args = parser.parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    if args.command == "plan":
        if not args.package:
            parser.error("plan 要指定 --package")
        dirs = [Path(d).resolve() for d in args.text_dir] or [build / "package-text"]
        report = plan(build, args.package, load_config(Path(args.config) if args.config else None), dirs)
    else:
        report = collect(build)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.command == "plan":
        return 0  # Without PyMuPDF the pipeline goes on; verify says the check was not possible.
    return 0 if not report.get("rejected") else 1


if __name__ == "__main__":
    sys.exit(main())
