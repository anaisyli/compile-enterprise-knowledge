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
from kb_common import (BUILD_DIRECTORY, load_config, load_json, load_jsonl, load_units, long_path, is_drawing_dimension_line,
                       remaining_ocr_pages, sha256_file, suppress_drawing_dimensions, write_json, write_text)
from office import convert_office
from reducto_ocr import CHECK_SHARE, DISAGREE, check_reasons, compare, disagrees, record_of
from samples import (GRADES, IMAGE_KIND, METHODS, PICTURES, TRIAGE, UNREADABLE_KIND, choose_samples, doc_type, held_image,
                     image_method, is_confirmed, load_decisions,
                     method_and_source, unreadable, unreadable_method)


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
STRUCTURES = {"表格", "流程图", "时间轴", "地图", "图表"}
LIST_MARK = re.compile(r"^\s*(?:\d+[.、)）]|[-*•])\s*")
TABLE_RULE = re.compile(r"\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{0,}:?\s*")
ODD = re.compile(r"[�-]|[À-ÿ]{3,}")
RULES = """# 看图核对规则

这些页面或图片已经提取了文字，但可能漏了图上的字、读错了字，或者看不出款号和产品的对应。请逐张看图，
和同一行 `text` 里提取出来的文字对照，回答下面三件事。只写你在图上看到的，不猜、不补充、不翻译。

1. missing：图上有、提取的文字里没有的内容，逐字照抄（款号、产品名、参数、图上的标语），按款分段。
   没有漏的写空字符串。
2. text_errors：提取的文字和图上不一样的地方（乱码、错字、缺字），简单写哪里不对。没有就写空字符串。
   structure 选了“尺寸图”的页，text_errors 会被忽略，不用填写。
3. pairing：这一页有几款产品时，写清楚每个款号对应哪款（比如“左：没写款号；中：没写款号；右：ANS S-002”）。
   原图本身看不出对应关系的，unclear 写 true。只有一款或没有产品的，pairing 写空字符串。
4. structure：这一页（或这张图）主要是什么结构：表格、流程图、时间轴、地图、图表、尺寸图；都不是写空字符串。
5. structured：structure 不为空时，把图上的字按原结构排好：
   - 表格：写成 Markdown 表，第一行是表头，每格照抄图上的字。
   - 流程图：按箭头顺序写成编号列表“1. …”，每步照抄图上的字；有分支的写清楚从哪一步分出去。
   - 时间轴：按时间先后写成编号列表，年份和事件照抄。
   - 地图、图表：列出图上标注的字和它们的关系（比如“柱状图：2023 年 120，2024 年 150”），只写图上有的。
   只能用图上看得到的字，不加说明、不补数字、不翻译；脚本会检查每个词都在这一页的文字里，有不在的就不收。
   - 尺寸图：不要转写任何尺寸、规格、孔位、角度或标注，也不要做款号配对。structured 只写图名，如“外形及安装尺寸图”；
     另填 drawing_model（图上清楚可辨的型号；不能确认就写“型号待确认”）。图名不能确认时写“尺寸图”。
     尺寸以原图为准。若同页还有正文或参数表，只把其中非图纸部分的漏字填入 missing。

价格、客户名也照抄，后面的规则会删掉，不要自己判断删哪些。

不转写的：产品图里屏幕上显示的示例画面（航班信息屏、示例菜单、促销语如“Buy One Get One Free”、
示例广告、桌面截图），以及零部件铭牌、机身标签上的小字。这些是展示用的画面或别家零件的标识，不是这份
资料讲的内容。产品名、型号、参数、卖点和资料自己的标题、说明照常转写。

每张图写一行 JSON，存成本目录下的 answer-001.jsonl（可以分几个文件，可以分给几个子 Agent）：

    {"key": "<todo.jsonl 里的 key>", "missing": "", "text_errors": "", "pairing": "", "unclear": false,
     "structure": "", "structured": "", "drawing_model": ""}

写完运行 `page_check.py collect --out <运行目录>`，再重跑流水线。补进去的字会标 ⚠“AI 看图补充，待核对”。
"""


def page_text(units: list[dict[str, str]], unit: str, number: int) -> str:
    """The page's draft as the build has it: the text layer, then what MinerU, Reducto or the model read."""
    native = re.compile(rf"^{unit} {number}(?: col \d+| table \d+)?$")
    read = re.compile(rf"^{unit} {number} (?:ocr|check)$")
    parts = [u["text"] for u in units if native.match(u["locator"])]
    for u in units:
        if read.match(u["locator"]) and u.get("text", "").strip():
            label = ("AI 看图补充" if u.get("checked") else "AI 读图" if u.get("engine") == "ai"
                     else "Reducto 识别" if u.get("engine") == "reducto" else "MinerU 识别")
            parts.append(f"【{label}】\n{u['text']}")
    return "\n\n".join(parts)


def draft_sources(text: str) -> list[str]:
    return ["文字层"] + [name for name in ("MinerU 识别", "Reducto 识别", "AI 读图") if f"【{name}" in text]


def code_pattern(prefixes: list[str]) -> re.Pattern[str]:
    if not prefixes:
        return GENERIC_CODE
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")[\s-]?[A-Z]?-?\d{2,5}\b")


def content_pictures(page: Any) -> list[dict[str, Any]]:
    """Pictures big enough to be content, not a logo."""
    area = page.rect.width * page.rect.height or 1
    return [i for i in page.get_image_info() if (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]) > area * PICTURE_SHARE]


def has_pictures(page: Any) -> bool:
    return bool(content_pictures(page))


def reasons_for(page: Any, text: str, codes: re.Pattern[str]) -> list[str]:
    pictures = content_pictures(page)
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
    try:
        with zipfile.ZipFile(long_path(path)) as archive:
            names = sorted(n for n in archive.namelist() if n.startswith("word/media/") and Path(n).suffix.casefold() in {".png", ".jpg", ".jpeg", ".gif", ".bmp"})
            return [(n, archive.read(n)) for n in names if archive.getinfo(n).file_size >= MIN_PICTURE_BYTES]
    except (zipfile.BadZipFile, OSError):
        return []  # A damaged or mislabelled file has no pictures to check; its text was read or not on its own.


def unreadable_entry(item_id: str, route: dict[str, Any], asset: dict[str, Any], build: Path,
                     decisions: dict[str, dict[str, Any]], pymupdf: Any) -> dict[str, Any] | str:
    """The page-triage entry of a file nothing was read from, or why it could not be counted."""
    name = route.get("display_name") or asset["file"].name
    source = pdf_of(asset["file"], build, asset["source_hash"][:16])
    if source is None:
        return f"{name}：没装 LibreOffice，幻灯片没法画成图"
    try:
        with pymupdf.open(long_path(source)) as document:
            count = document.page_count
    except Exception as exc:  # A damaged file is reported, it does not stop the run.
        return f"{name}：打不开（{type(exc).__name__}）"
    method, method_from = unreadable_method(decisions, item_id)
    grades = {str(n): {"grade": "C", "reasons": ["整份没有文字层，还没人读过"]} for n in range(1, count + 1)}
    return {"name": name, "kind": UNREADABLE_KIND, "method": method, "method_from": method_from,
            "unit": "slide" if asset["file"].suffix.casefold() in {".pptx", ".ppt"} else "page",
            "counts": {g: count if g == "C" else 0 for g in GRADES}, "pages": grades}


def image_entry(item_id: str, route: dict[str, Any], asset: dict[str, Any], decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A single picture nobody has read: one grade C page, read by the model unless Reducto is chosen."""
    method, method_from = image_method(decisions, item_id)
    return {"name": route.get("display_name") or asset["file"].name, "kind": IMAGE_KIND, "method": method,
            "method_from": method_from, "unit": "page", "counts": {g: int(g == "C") for g in GRADES},
            "pages": {"1": {"grade": "C", "reasons": ["单张图片，还没人读过"]}}}


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
    sampling_path = folder / "sampling.json"
    sampling = load_json(sampling_path) if sampling_path.is_file() else {}
    codes = code_pattern(config.get("style_prefixes", []))
    share = float((config.get("reducto") or {}).get("check_share", CHECK_SHARE))
    rng = random.Random(20261105)
    todo, skipped = [], []
    triage: dict[str, Any] = {}
    # Until a kind of file is confirmed on its example, only the example is looked at page by page.
    docs = load_jsonl(build / "docs.jsonl")
    by_item = {d["item_id"]: d for d in docs}
    samples = set(choose_samples(docs, config, build).values())
    decisions = load_decisions(build)
    deferred: dict[str, int] = {}
    for item_id, route in sorted(routes.items()):
        asset = files.get(item_id)
        if (unreadable(route) and asset and asset["file"].suffix.casefold() in PAGED
                and Path(long_path(asset["file"])).is_file()):
            # Nothing read from it yet: every page is grade C, and its row on the plan page lets a person
            # choose how it is read (MinerU unless chosen). Once read, it is extracted and checked like the rest.
            entry = unreadable_entry(item_id, route, asset, build, decisions, pymupdf)
            if isinstance(entry, str):
                skipped.append(entry)
            else:
                triage[item_id] = entry
            continue
        if held_image(route) and asset and asset["file"].suffix.casefold() in PICTURES and Path(long_path(asset["file"])).is_file():
            triage[item_id] = image_entry(item_id, route, asset, decisions)
            continue
        if route.get("route") != "kb" or route.get("tier") not in CHECKED_TIERS or not asset or not Path(long_path(asset["file"])).is_file():
            continue
        doc = by_item.get(item_id)
        if doc and doc["doc_id"] not in samples and not is_confirmed(doc_type(doc), decisions, config):
            deferred[doc_type(doc)] = deferred.get(doc_type(doc), 0) + int(doc.get("pages") or 1)
            continue
        # How a person chose to read this file (or its kind): it decides what each grade of page gets.
        kind = doc_type(doc) if doc else ""
        method, method_from = method_and_source(kind, decisions, item_id) if doc else ("text", "默认")
        suffix = asset["file"].suffix.casefold()
        # The reviewer sees what the build will publish: text layer plus what MinerU or the model read.
        units = load_units([*text_dirs, build / "reducto-units", build / "mineru-units"], item_id)
        drawing_pages = {f"{entry.get('unit', 'page')} {entry.get('page')}"
                         for key, entry in done.items() if entry.get("dimension_drawing") and entry.get("item_id") == item_id}
        units = suppress_drawing_dimensions(
            units + [{"locator": f"{page} drawing", "text": done[f"{item_id}|{page}"].get("drawing_caption", ""),
                      "dimension_drawing": True, "drawing_model_confirmed": done[f"{item_id}|{page}"].get("drawing_model_confirmed", False)}
                     for page in drawing_pages if f"{item_id}|{page}" in done])
        reducto = compare(build, item_id)
        reducto_record = record_of(build, item_id)
        reducto_read = set(reducto_record.get("pages_read", [])) if reducto_record.get("source_hash") == asset.get("source_hash") else set()
        read_path = build / "mineru-units" / f"{item_id}.json"
        name = route.get("display_name") or asset["file"].name
        hold = ""
        if method == "mineru" and not (read_path.is_file() and load_json(read_path).get("source") == "mineru"):
            hold = f"{name}：按“MinerU 初稿 + AI 复核”处理，等 MinerU 初稿出来再对照"
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
        # Grade C pages nobody has read yet go to MinerU, Reducto or ai_read.py first. A page OCR read is
        # not skipped: OCR alone is not a visual check, it can miss labels or lose layout.
        no_text = set(route.get("ocr_pages") or [])
        unread = set(remaining_ocr_pages(build, item_id, sorted(no_text)))
        # A page already checked against its picture is done, unless MinerU's draft came after that look.
        looked_at: set[int] = set()
        read_units = load_json(read_path).get("units", []) if read_path.is_file() else []
        mineru_pages = {int(m.group(1)) for u in read_units if u.get("engine") not in {"ai"} and u.get("text", "").strip()
                        and (m := re.match(r"(?:page|slide) (\d+) ocr$", u.get("locator", ""))) and not u.get("checked")}
        for u in read_units:
            match = re.match(r"(?:page|slide) (\d+)", u.get("locator", ""))
            if u.get("checked") and match:
                number = int(match.group(1))
                looked = done.get(f"{item_id}|{unit} {number}", {})
                if number not in mineru_pages or "MinerU 识别" in looked.get("draft", []) or not looked:
                    looked_at.add(number)
        try:
            document = pymupdf.open(long_path(source))
        except Exception as exc:  # A damaged file is reported, it does not stop the run.
            skipped.append(f"{name}：打不开（{type(exc).__name__}）")
            continue
        grades: dict[str, dict[str, Any]] = {}
        waiting_reducto = 0
        with document:
            chosen, pool = [], set()
            for number, page in enumerate(document, start=1):
                if number in unread:
                    grades[str(number)] = {"grade": "C", "reasons": ["没有文字层，还没人读过"]}
                    continue
                text = page_text(units, unit, number)
                seen = reducto.get(number)
                found = reasons_for(page, text, codes)
                disagree = []
                dimension_drawing = done.get(f"{item_id}|{unit} {number}", {}).get("dimension_drawing", False)
                if seen is not None and not dimension_drawing:
                    disagree = check_reasons(seen, text)
                    if seen.get("only_mineru") or seen.get("only_reducto"):
                        text += f"\n\n【Reducto 识别（与 MinerU 不一致，以原图为准）】\n{seen['text']}"
                grade = "D" if disagrees(disagree) else "C" if number in no_text else "B" if found else "A"
                why = [r for r in disagree if r.startswith(DISAGREE)] if grade == "D" else found or (
                    ["没有文字层，已读出"] if grade == "C" else [])
                grades[str(number)] = {"grade": grade, "reasons": why}
                if hold:
                    continue
                # What each grade gets depends on how the file is read.
                if (method == "reducto" and seen is None and grade in {"B", "C"} and number not in reducto_read
                        and number not in mineru_pages):
                    waiting_reducto += 1  # Reducto reads it first; then it is compared and sampled.
                    continue
                if method == "reducto" and seen is not None:
                    reasons = disagree + (["文字里有乱码"] if ODD.search(text) else [])
                else:
                    reasons = disagree + [r for r in found if r not in disagree]
                if method in {"mineru", "ai"} and not reasons and has_pictures(page):
                    reasons = [f"按“{METHODS[method]}”处理，有图片的页都对照"]
                if seen is not None and not reasons and not seen["mineru"] and seen["terms"]:
                    pool.add(number)
                chosen.append((number, text, reasons))
            rest = [c for c in chosen if not c[2] and c[0] not in pool]
            sample_key = f"{item_id}:{asset.get('source_hash', '')}:{method}:{share}"
            selected = sampling.setdefault(sample_key, {})
            if rest and "ordinary" not in selected:
                selected["ordinary"] = [c[0] for c in rng.sample(rest, max(1, round(len(rest) * SAMPLE_SHARE)))]
            for number, text, _ in rest:
                if number not in selected.get("ordinary", []):
                    continue
                chosen[[c[0] for c in chosen].index(number)] = (number, text, ["抽查"])
            # Codes and numbers only Reducto read are checked more often than the ordinary sample.
            alone = [c for c in chosen if c[0] in pool]
            if alone and "reducto" not in selected:
                selected["reducto"] = [c[0] for c in rng.sample(alone, max(1, round(len(alone) * share)))]
            for number, text, _ in alone:
                if number not in selected.get("reducto", []):
                    continue
                chosen[[c[0] for c in chosen].index(number)] = (number, text, ["只有 Reducto 识别，含款号或数字，抽查"])
            for number, text, reasons in chosen:
                if not reasons or number in looked_at:
                    continue
                target = pictures / f"{item_id}-p{number}.png"
                if not target.is_file():
                    document[number - 1].get_pixmap(dpi=DPI).save(str(target))
                todo.append({"key": f"{item_id}|{unit} {number}", "item_id": item_id, "unit": unit, "page": number, "name": name,
                             "image": str(target), "text": text, "reasons": reasons, "draft": draft_sources(text),
                             "grade": grades[str(number)]["grade"]})
        if hold:
            skipped.append(hold)
        if waiting_reducto:
            skipped.append(f"{name}：按“{METHODS['reducto']}”处理，{waiting_reducto} 页 B、C 级页等 Reducto 结果再对照")
        counts = {g: sum(1 for p in grades.values() if p["grade"] == g) for g in GRADES}
        triage[item_id] = {"name": name, "kind": kind, "method": method, "method_from": method_from, "unit": unit,
                           "counts": counts, "pages": grades}
    # A page looked at before MinerU's draft came is looked at again with it; so is one Reducto now disagrees on.
    todo = [t for t in todo if t["key"] not in done
            or ("MinerU 识别" in t.get("draft", []) and "MinerU 识别" not in done[t["key"]].get("draft", []))
            or ("Reducto 识别" in t.get("draft", []) and "Reducto 识别" not in done[t["key"]].get("draft", [])
                and disagrees(t["reasons"]))]
    with (folder / "todo.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for entry in todo:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    write_text(folder / "README.md", RULES)
    write_json(sampling_path, sampling)
    write_json(build / TRIAGE, {"grades": {g: f"{label}：{rule}" for g, (label, rule) in GRADES.items()}, "files": triage})
    totals = {g: sum(f["counts"][g] for f in triage.values()) for g in GRADES}
    report = {"ok": True, "to_check": len(todo), "checked": len(done), "files": len({t["item_id"] for t in todo}), "skipped": skipped,
              "deferred": deferred, "grades": totals}
    write_json(folder / "plan.json", report)
    return report


def clean_note(text: str) -> str:
    """A note the model writes about a page must not carry what the rules remove."""
    text = re.sub(r"\s+", " ", text or "").strip()[:300]
    return "" if any(pattern.search(text) for pattern in HARD_PATTERNS.values()) else text


def laid_out(text: str, source: str) -> tuple[str, list[str]]:
    """Accept a table or ordered list only if every word in it is on the page already.

    Returns the text, or "" with the pieces that were not found: laying words out may not add any."""
    if not text.strip():
        return "", []
    known = re.sub(r"\s+", "", source).casefold()
    refused = []
    for line in text.splitlines():
        body = LIST_MARK.sub("", line)
        if TABLE_RULE.fullmatch(body.strip()):
            continue
        for piece in re.split(r"<br\s*/?>|\||→|->|=>|；|;|：|:", body, flags=re.IGNORECASE):
            word = re.sub(r"\s+", "", piece).casefold()
            if word and word not in known:
                refused.append(piece.strip())
    if refused or any(pattern.search(text) for pattern in HARD_PATTERNS.values()):
        return "", refused[:5]
    return text.strip(), []


def checked_entry(build: Path, key: str, previous: dict[str, Any], text_dirs: list[Path],
                  checked_sources: dict[str, bool]) -> dict[str, Any] | None:
    """Reopen an accepted page against its original source and current extraction caches."""
    item_id, unit, number = previous.get("item_id"), previous.get("unit"), previous.get("page")
    if not item_id or unit not in {"page", "slide", "image"} or not isinstance(number, int) or number < 1:
        return None
    if key != f"{item_id}|{unit} {number}":
        return None
    if item_id not in checked_sources:
        route = load_json(build / "routes.json").get(item_id, {})
        source = Path(route.get("package", "")) / route.get("path", "")
        expected = route.get("source_hash")
        checked_sources[item_id] = bool(expected and source.is_file() and sha256_file(source) == expected)
    if not checked_sources[item_id]:
        return None
    units = load_units([*text_dirs, build / "reducto-units", build / "mineru-units"], item_id)
    # A superseded visual addition cannot serve as evidence for its own replacement.
    text = page_text([u for u in units if not u.get("checked")], unit, number)
    return {**previous, "key": key, "text": text}


def collect(build: Path, *, revise_checked: bool = False, answer_files: list[Path] | None = None,
            text_dirs: list[Path] | None = None) -> dict[str, Any]:
    folder = build / FOLDER
    todo = {entry["key"]: entry for entry in load_jsonl(folder / "todo.jsonl")}
    results: dict[str, Any] = load_json(build / RESULTS) if (build / RESULTS).is_file() else {}
    rejected, by_item, answers = [], {}, {}
    reopened, checked_sources = {}, {}
    for path in sorted(answer_files if answer_files is not None else folder.glob("answer-*.jsonl")):
        for record in load_jsonl(path):
            key = record.get("key")
            entry = todo.get(key)
            if entry is None and key in results:
                if not revise_checked:
                    continue  # Revisions outside the current plan must be explicitly requested.
                if key not in reopened:
                    reopened[key] = checked_entry(build, key, results[key], text_dirs or [build / "package-text"], checked_sources)
                entry = reopened[key]
            if not entry or not all(isinstance(record.get(field, ""), str) for field in ("missing", "text_errors", "pairing", "structure", "structured")):
                rejected.append(record.get("key") or "（没有 key）")
                continue
            answers[entry["key"]] = record
    # A later answer revises the same page; do not append both drafts to its source text.
    for key, record in answers.items():
        entry = todo.get(key) or reopened[key]
        missing = (record.get("missing") or "").strip()
        structure = (record.get("structure") or "").strip()
        dimension_drawing = structure == "尺寸图"
        if dimension_drawing:
            missing = "\n".join(line for line in missing.splitlines() if not is_drawing_dimension_line(line)).strip()
        structured, refused = laid_out(record.get("structured") or "", entry.get("text", "") + "\n" + missing)
        drawing_model = clean_note(record.get("drawing_model", "")) or "型号待确认"
        drawing_name = clean_note(record.get("structured", "")) or "尺寸图"
        # A label, not a transcription: no dimension text is copied or validated.
        drawing_caption = f"{drawing_model} 的{drawing_name}" if dimension_drawing else ""
        results[key] = {"item_id": entry["item_id"], "unit": entry["unit"], "page": entry["page"],
                        "reasons": entry["reasons"], "added": bool(missing), "draft": entry.get("draft", ["文字层"]),
                        "text_errors": "" if dimension_drawing else clean_note(record.get("text_errors", "")),
                        "pairing": "" if dimension_drawing else clean_note(record.get("pairing", "")),
                        "unclear": False if dimension_drawing else bool(record.get("unclear")),
                        "structure": structure if structure in STRUCTURES | {"尺寸图"} else "",
                        "structured": structured if structure in STRUCTURES else "", "structured_refused": refused,
                        "dimension_drawing": dimension_drawing, "drawing_model": drawing_model if dimension_drawing else "",
                        "drawing_model_confirmed": dimension_drawing and drawing_model != "型号待确认",
                        "drawing_name": drawing_name if dimension_drawing else "",
                        "drawing_caption": drawing_caption}
        if missing or key in reopened:
            # A locator of its own: on a page MinerU read, MinerU's "page N ocr" must not swallow it.
            by_item.setdefault(entry["item_id"], []).append(
                {"locator": f"{entry['unit']} {entry['page']} check", "text": missing, "engine": "ai", "checked": True})
        if dimension_drawing:
            by_item.setdefault(entry["item_id"], []).append(
                {"locator": f"{entry['unit']} {entry['page']} drawing", "text": drawing_caption,
                 "engine": "ai", "checked": True, "dimension_drawing": True,
                 "drawing_model_confirmed": drawing_model != "型号待确认"})
    store_ai_units(build, by_item)
    write_json(build / RESULTS, results)
    left = len(set(todo) - set(results))
    return {"collected": len(results), "revised": sum(key in reopened for key in answers),
            "added_text": sum(bool(u["text"]) for units in by_item.values() for u in units), "rejected": rejected, "left": left}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check extracted text against the pages themselves.")
    parser.add_argument("command", choices=["plan", "collect"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--package", action="append", default=[], help="Preparation package; repeat (plan)")
    parser.add_argument("--text-dir", action="append", default=[], help="Extracted text units; repeat (plan)")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--revise-checked", action="store_true", help="Explicitly recheck answers for already accepted pages (collect)")
    parser.add_argument("--answer-file", action="append", help="Only collect this answer file; repeat for several files")
    args = parser.parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    if args.command == "plan":
        if not args.package:
            parser.error("plan 要指定 --package")
        dirs = [Path(d).resolve() for d in args.text_dir] or [build / "package-text"]
        report = plan(build, args.package, load_config(Path(args.config) if args.config else None), dirs)
    else:
        report = collect(build, revise_checked=args.revise_checked,
                         answer_files=[Path(p).resolve() for p in args.answer_file] if args.answer_file else None,
                         text_dirs=[Path(d).resolve() for d in args.text_dir] if args.text_dir else None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.command == "plan":
        return 0  # Without PyMuPDF the pipeline goes on; verify says the check was not possible.
    return 0 if not report.get("rejected") else 1


if __name__ == "__main__":
    sys.exit(main())
