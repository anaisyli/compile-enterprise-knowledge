#!/usr/bin/env python3
"""Material pictures: named for what they show, sorted by what they are for.

A picture's name comes from the words beside it in the source: the row's product name, the
catalog page's heading and the company's own style code, the website's alt text. Nothing is
guessed. A buyer's style number, a customer's name, or a passage carrying ⚠ never goes into a
name; such a picture keeps a plain numbered name until a person or an AI look gives it one.

export   lists the pictures that still have only a numbered name, for an AI to look at.
collect  reads the AI's answers; they are used on the next build, marked 待核对.
restore-names restores selected published automatic names after checking image hashes.

A name, once given, stays: links in the vault and on the website must not break.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from kb_common import (BUILD_DIRECTORY, infer_model_from_title, load_json, load_jsonl, long_path, path_uri, run_lock,
                       safe_name, sha256_file, short_hash, write_json, write_jsonl, write_text)
from guard_blocks import HARD_PATTERNS
from legacy_migrations import repair_legacy_picture_name
from products import clean_levels, parts_of, product_folders


ASSETS = "03_正式知识/90_素材"
INDEX = f"{ASSETS}/00_素材索引.md"
LIST_CSV = f"{ASSETS}/00_素材清单.csv"
STATE = "picture-names.json"
# Which naming rules the names in STATE follow. Layout 2 files pictures by where they came from
# (文档提取图/<来源文档>/第N页, 资料包素材/<the package's folders>, ...). relayout switches an existing
# project over once; the next publish moves the pictures and writes the old-to-new list.
LAYOUT = "picture-layout.json"
CURRENT_LAYOUT = 2
UNSEEN_FOLDER = "旧版未归类"
RENAME_LIST = f"{ASSETS}/00_旧名新名对照.csv"
AI_FOLDER = "picture-names"
AI_ANSWERS = "picture-names/answers.json"
WEBSITE_PICTURES = "website/pictures.json"
# A picture on more pages than this is a logo or a background, not material.
REPEATED = 3
LABEL_MAX = 40
PUBLIC, CHECK, INTERNAL = "可用于官网", "待核对", "内部"
NUMBERED, FROM_SOURCE, FROM_AI = "编号", "原文", "AI看图"
# Read on a document's title or a folder's name only, never on page text: a page that mentions
# ISO 5211 (a valve flange standard) is not a certificate. ISO counts only as a certification number.
CERTIFICATE = re.compile(r"(?i)certif|证书|认证|资质|许可证|\blicense\b|\bISO\s?(?:9001|14001|20000|27000|27001|45001|13485|22000|50001)\b|\bBSCI\b|OEKO|"
                         r"\bSGS\b|\bGRS\b|test report|检测报告")
FACTORY = re.compile(r"(?i)factory|workshop|warehouse|production line|工厂|车间|厂房|生产线|仓库")
MEANINGLESS_ALT = re.compile(r"(?i)^(?:img|image|picture|photo|pic|dsc|banner|logo|icon)?[\s_\-]*\d*$|\.(?:jpe?g|png|webp|gif)$")
FOLDERS = ("资料包素材", "产品", "文档提取图", "官网图片", "证书", "工厂与设备", "页面图", "报价单参考图", "产品表图片", "其他",
           "旧版未归类")
EXTRACTED, WEBSITE_FOLDER, PAGES = "文档提取图", "官网图片", "页面图"
# Pictures screening kept by folder sit under their original folders, the last few levels of them:
# enough to tell one product's 白底图 from another's without paths too long for Windows.
MEDIA_ROOT = "资料包素材"
MEDIA_DEPTH = 3
MEDIA_LIST = "media.json"
KIND_LABELS = {"image": "图片", "video": "视频", "audio": "音频"}


def own_code_pattern(prefixes: list[str]) -> re.Pattern[str] | None:
    if not prefixes:
        return None
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")[\s-]?[A-Z]?-?\d{2,4}(?:-\d+)?\b")


class Namer:
    """Turns the words beside a picture into a safe label, or nothing."""

    def __init__(self, config: dict[str, Any], forbidden: re.Pattern[str] | None) -> None:
        self.codes = own_code_pattern(config.get("style_prefixes", []))
        self.forbidden = forbidden
        # Website categories are English slugs in the config; the English file name can use them.
        self.english = {cn: slug for slug, cn in config.get("categories", {}).items()}

    def code(self, text: str) -> str:
        found = sorted({re.sub(r"\s+", "-", m.group(0).upper()) for m in self.codes.finditer(text)}) if self.codes else []
        return found[0] if len(found) == 1 else ""

    def label(self, text: str) -> str:
        """Words only: a token with digits is a buyer's number unless it is the company's own code."""
        if not text or (self.forbidden and self.forbidden.search(text)):
            return ""
        own = {m.group(0) for m in self.codes.finditer(text)} if self.codes else set()
        words = [w for w in re.split(r"\s+", re.sub(r"[|/\\:：;；,，()（）\[\]{}]+", " ", text)) if w]
        kept = [w for w in words if w in own or not re.search(r"\d", w)]
        label = " ".join(kept).strip(" -_.")
        return label[:LABEL_MAX].rstrip(" -_.") if re.search(r"[A-Za-z一-鿿]{2}", label) else ""


CONFIG_FREE_NAMER = Namer({}, None)  # For callers with no project config, such as tests.


def english_name(entry: dict[str, Any], namer: Namer, suffix: str) -> str:
    """For the website team only: internal pictures do not go online, so they get none."""
    if entry["use"] == INTERNAL:
        return ""
    words = re.findall(r"[A-Za-z0-9]+", entry.get("english_source") or entry["alt"])
    if not any(re.search(r"[A-Za-z]", w) for w in words):
        words = []
    slug = "-".join(w.casefold() for w in words)[:60].strip("-") or namer.english.get(entry.get("line", ""), "")
    return f"{slug}-{entry.get('short') or entry['id'][4:12]}{suffix}" if slug else ""


def first_line(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        for line in block["text"].splitlines():
            line = line.strip(" |#*-")
            if 2 <= len(line) <= LABEL_MAX:
                return line
    return ""


def named_kind(*names: str) -> str | None:
    """证书 or 工厂与设备 when a title or folder name says so plainly; None otherwise."""
    text = " ".join(n for n in names if n)
    if CERTIFICATE.search(text):
        return "证书"
    if FACTORY.search(text):
        return "工厂与设备"
    return None


def doc_folder(doc: dict[str, Any], root: str) -> str:
    """<root>/<来源文档>, or 证书/工厂与设备 when the document's title or its folders say so."""
    name = doc["title"].split("-", 1)[-1] if "-" in doc["title"] else doc["title"]
    folders = [p for p in (doc.get("package_path") or "").replace("\\", "/").split("/")[:-1] if p]
    kind = named_kind(doc["title"], *folders[-3:])
    return f"{kind or root}/{safe_name(name, '未命名资料')}"


def page_label(group: str) -> str:
    """第 3 页 → 第3页; 第 3 张幻灯片 → 第3张幻灯片."""
    return group.replace(" ", "")


def page_group(info: dict[str, Any], page: str) -> str:
    """The heading a page, slide or embedded Word picture has in the source layer."""
    if str(page).startswith("image ") or info.get("page_unit") == "image":
        return f"图片 {str(page).split()[-1]}"
    return f"第 {page} 张幻灯片" if info.get("page_unit") == "slide" else f"第 {page} 页"


def drawing_entries(build: Path) -> dict[str, dict[str, Any]]:
    """Every page, slide or Word picture confirmed as a dimension drawing."""
    entries: dict[str, dict[str, Any]] = {}
    if (build / "page-check/results.json").is_file():
        for key, result in load_json(build / "page-check/results.json").items():
            if result.get("dimension_drawing"):
                entries[key] = result
    folder = build / "mineru-units"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        record = load_json(path)
        item_id = record.get("item_id") or path.stem
        for unit in record.get("units", []):
            match = re.fullmatch(r"(page|slide|image) (\d+) drawing", unit.get("locator", ""))
            if not unit.get("dimension_drawing") or not match:
                continue
            key = f"{item_id}|{match.group(1)} {match.group(2)}"
            if key not in entries:
                model = (unit.get("text") or "").split(" 的", 1)[0].strip() or "型号待确认"
                entries[key] = {"item_id": item_id, "unit": match.group(1), "page": int(match.group(2)),
                                "dimension_drawing": True, "drawing_model": model,
                                "drawing_model_confirmed": bool(unit.get("drawing_model_confirmed"))}
    return entries


def ensure_dimension_previews(build: Path, routes: dict[str, Any], status_info: dict[str, Any]) -> None:
    """Render reviewed drawing pages and unpack reviewed Word drawing pictures, without OCR."""
    checks = drawing_entries(build)
    assets = build / "assets"
    word_cache: dict[str, list[tuple[str, bytes]]] = {}
    for result in checks.values():
        unit = result.get("unit")
        if not result.get("dimension_drawing") or unit not in {"page", "slide", "image"}:
            continue
        item, page = result["item_id"], str(result["page"])
        route = routes.get(item, {})
        if route.get("route") != "kb":
            continue
        info = status_info.setdefault(item, {})
        images = info.setdefault("page_images", {})
        key = f"image {page}" if unit == "image" else page
        existing = images.get(key) or (images.get(page) if unit == "image" else None)
        if existing and (assets / existing).is_file():
            images[key] = existing
            continue
        if unit == "image":
            source = Path(route["package"]) / route["path"]
            if sha256_file(source) != route["source_hash"]:
                raise ValueError(f"Drawing source changed: {item}")
            if item not in word_cache:
                from office import convert_office
                from page_check import word_pictures
                docx = source if source.suffix.casefold() == ".docx" else convert_office(
                    source, "docx", build / "converted", route["source_hash"][:16], 600)
                word_cache[item] = word_pictures(docx)
            index = int(page) - 1
            if index < 0 or index >= len(word_cache[item]):
                raise ValueError(f"Cannot render dimension drawing: {item} image {page}")
            member, data = word_cache[item][index]
            suffix = Path(member).suffix.casefold().replace(".jpeg", ".jpg") or ".png"
            # Use the same content-hash id as other extracted images so existing 90_素材 entries are reused.
            name = f"img-{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
            if not (assets / name).is_file():
                assets.mkdir(parents=True, exist_ok=True)
                (assets / name).write_bytes(data)
            images[key] = name
            continue
        name = "img-" + short_hash(f"{route['source_hash']}|drawing {page}", 16) + ".png"
        if not (assets / name).is_file():
            import pymupdf
            from ai_read import pdf_of
            source = Path(route["package"]) / route["path"]
            if sha256_file(source) != route["source_hash"]:
                raise ValueError(f"Drawing source changed: {item}")
            pdf = pdf_of(source, build, route["source_hash"][:16])
            if pdf is None:
                raise ValueError(f"Cannot render dimension drawing: {item} {page}")
            with pymupdf.open(long_path(pdf)) as document:
                original = document[int(page) - 1]
                scale = min(150 / 72, 3000 / max(original.rect.width, original.rect.height))
                assets.mkdir(parents=True, exist_ok=True)
                original.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).save(str(assets / name))
        images[page] = name


def candidates(build: Path, docs: dict[str, dict[str, Any]], by_doc: dict[str, list[dict[str, Any]]], order: list[str],
               statuses: dict[str, str], unsafe: set[tuple[str, str]], status_info: dict[str, Any], namer: Namer,
               max_row_images: int, held: set[tuple[str, str]], places: dict[str, Any] | None = None,
               match: Callable[..., dict[str, Any] | None] | None = None,
               doc_places: dict[str, tuple[str, str] | None] | None = None,
               excluded_images: set[str] | None = None) -> list[dict[str, Any]]:
    """Every picture the vault will show or keep, with the words that can name it. held: (doc, locator)
    of every passage removed or waiting for the client. doc_places: doc_id -> (大类, 产品) by
    curate.place_docs, for the index's 按产品 section."""
    found: list[dict[str, Any]] = []

    def doc_product(doc_id: str) -> tuple[str, str] | None:
        place = (doc_places or {}).get(doc_id)
        return tuple(place) if place and place[1] else None
    assets = build / "assets"
    website = load_json(build / WEBSITE_PICTURES) if (build / WEBSITE_PICTURES).is_file() else {}
    page_checks = drawing_entries(build)
    pages_of: dict[str, set[str]] = defaultdict(set)
    for doc_id in order:
        for page, names in status_info.get(docs[doc_id]["item_id"], {}).get("page_pictures", {}).items():
            for name in names:
                pages_of[name].add(f"{doc_id}:{page}")
    for page_id, pictures in website.items():
        for picture in pictures:
            pages_of[picture["id"]].add(page_id)

    def add(name: str, doc_id: str, group: str, **fields: Any) -> None:
        if name not in (excluded_images or ()) and (assets / name).is_file():
            found.append({"id": name, "doc_id": doc_id, "group": group, **fields})

    for doc_id in order:
        doc, blocks = docs[doc_id], by_doc[doc_id]
        reference = doc.get("role") == "reference"
        quotation = doc.get("folder", "").startswith("产品表/报价单")
        for block in blocks:
            if block["status"] == "pending_client":
                continue
            if block["kind"] != "row":
                for name in block.get("images", []):
                    add(name, doc_id, block["group"], folder=doc_folder(doc, PAGES), prefix="",
                        label=block["group"], alt=f"{doc['title']} {block['group']}", use=INTERNAL,
                        location=block["locator"], product=doc_product(doc_id))
                continue
            cells = block.get("cells", {})
            clean = not block.get("flags")
            code = namer.code(cells.get("style", "")) if clean else ""
            words = namer.label(cells.get("name") or cells.get("style", "")) if clean else ""
            label = " ".join(dict.fromkeys(p for p in (code, words) if p))
            for name in block.get("images", [])[:max_row_images]:
                add(name, doc_id, block["group"], folder="报价单参考图" if quotation else "产品表图片",
                    prefix="报价单参考图" if quotation else "产品表", label=label, alt=label, use=INTERNAL,
                    english_source=words, location=block["locator"])
        info = status_info.get(doc["item_id"], {})
        page_unit = info.get("page_unit", "page")
        placed = product_folders((places or {}).get(doc["item_id"]), namer.forbidden)

        def dimension_result(page: str) -> dict[str, Any]:
            key = page if str(page).startswith("image ") else f"{page_unit} {page}"
            result = page_checks.get(f"{doc['item_id']}|{key}", {})
            return result if result.get("dimension_drawing") else {}

        for page, name in info.get("page_images", {}).items():
            group = page_group(info, page)
            if (doc_id, group) not in unsafe:
                drawing = dimension_result(page)
                if drawing:
                    raw_model = drawing.get("drawing_model") or "型号待确认"
                    if raw_model == "型号待确认":
                        raw_model = infer_model_from_title(doc.get("title", "")) or "型号待确认"
                    model = safe_name(raw_model, "型号待确认")
                    folder = drawing_folder(model, placed, match, namer.forbidden)
                    add(name, doc_id, group, folder=folder, prefix=model, label="尺寸图",
                        alt="尺寸见原图", use=CHECK, english_source="", location=group, dimension_drawing=True,
                        product=model_product(model, match) or doc_product(doc_id))
                else:
                    # 页面图/<来源文档>/第N页: the name is the place, nothing guessed from the page.
                    folder = doc_folder(doc, PAGES)
                    add(name, doc_id, group, folder=folder, prefix="", label=page_label(group),
                        alt=f"{doc['title']} {group}原图", use=INTERNAL if folder.startswith(PAGES) else CHECK,
                        location=group, product=doc_product(doc_id))
        catalog = doc["catalog"] if "catalog" in doc else doc.get("folder") == "产品画册"
        line = doc["title"].split("-", 1)[-1] if catalog else ""
        for page, names in info.get("page_pictures", {}).items():
            group = page_group(info, page)
            if (doc_id, group) in unsafe or any(b.get("status") == "pending_client" for b in blocks if b["group"] == group):
                continue
            drawing = dimension_result(page)
            if drawing:
                raw_model = drawing.get("drawing_model") or "型号待确认"
                if raw_model == "型号待确认":
                    raw_model = infer_model_from_title(doc.get("title", "")) or "型号待确认"
                model = safe_name(raw_model, "型号待确认")
                folder = drawing_folder(model, placed, match, namer.forbidden)
                drawing_names = drawing.get("drawing_picture_ids", names)
                for name in names:
                    if name not in drawing_names:
                        continue
                    add(name, doc_id, group, folder=folder, prefix=model, label="尺寸图",
                        alt="尺寸见原图", use=CHECK, english_source="", location=group, dimension_drawing=True,
                        product=model_product(model, match) or doc_product(doc_id))
                names = [name for name in names if name not in drawing_names]
                if not names:
                    continue
            on_page = [b for b in blocks if b["group"] == group and not b.get("dimension_drawing")]
            clean = [b for b in on_page if not b.get("flags")]
            # The page's heading describes the picture (alt, English name); the file name is structural:
            # 文档提取图/<来源文档>/[公司款号-]第N页, so half a sentence never becomes a file name.
            heading = namer.label(first_line(clean))
            code = namer.code("\n".join(b["text"] for b in clean))
            folder = doc_folder(doc, EXTRACTED)
            use = PUBLIC if statuses.get(doc_id) == "有效" and not reference and len(clean) == len(on_page) else CHECK
            product = (match(code) if code and match else None)
            for name in names:
                if len(pages_of[name]) <= REPEATED:
                    add(name, doc_id, group, folder=folder, prefix=code, label=page_label(group), line=line,
                        alt=" ".join(p for p in (line, code, heading) if p) or f"{doc['title']} {group}",
                        use=use, english_source=heading, location=group, bare=not (code or heading),
                        product=product_key(product) if product else doc_product(doc_id))
        if website.get(doc["item_id"]):
            category = doc.get("folder", "").split("/", 1)[-1]
            title = namer.label(doc["title"].split("-", 1)[-1])
            for picture in website.get(doc["item_id"], []):
                if len(pages_of[picture["id"]]) > REPEATED:
                    continue
                alt = "" if MEANINGLESS_ALT.search(picture.get("alt", "").strip()) else namer.label(picture.get("alt", ""))
                section = f"section {picture.get('section', 1)}"
                if (doc_id, section) in held:
                    continue  # Its section was removed or waits for the client: so does the picture.
                own = [b for b in blocks if b.get("locator") == section]
                flagged = any(b.get("flags") for b in own)
                company = category in {"公司与服务", ""}
                # 官网图片/<页面名>; a page plainly titled as certificates or the factory goes there instead.
                page = safe_name(doc["title"].split("-", 1)[-1], "官网页面")
                folder = f"{named_kind(doc['title']) or WEBSITE_FOLDER}/{page}"
                product = None if company else (match(category) if match else None)
                add(picture["id"], doc_id, own[0]["group"] if own else blocks[0]["group"], folder=folder,
                    prefix="官网" if company else category, label=alt or title,
                    line="" if company else category, alt=alt or title, use=CHECK if flagged else PUBLIC,
                    english_source=alt or doc.get("original_name", ""), location=section,
                    product=product_key(product) if product else doc_product(doc_id))
    media = load_media(build)
    # Pages of one drawing PDF share the hash in their ids; each gets its own code and a page number.
    drawing_pages = Counter(entry["item_id"] for entry in media if entry.get("drawing"))
    for entry in media:
        # Filed where the client filed it: 资料包素材/<the package's own folders, tidied>. A folder
        # plainly named as certificates or the factory goes to 证书/工厂与设备 from that folder down.
        # Which product it shows is worked out only for the index's 按产品 section.
        place = (places or {}).get(entry["item_id"])
        named_from = named_folders(entry["relpath"], namer)
        kind, folders = named_from if named_from else (None, media_folders(entry["relpath"], namer))
        stem = Path(entry["relpath"]).stem
        label = "" if namer.forbidden and namer.forbidden.search(stem) else safe_name(stem)[:LABEL_MAX]
        prefix = folders[-1] if folders else ""
        use, alt = PUBLIC, ""
        folder = "/".join([kind or MEDIA_ROOT, *folders])
        fields: dict[str, Any] = {"product": product_key(place["product"]) if place and place.get("product") else None}
        if entry.get("status") == "dimension_drawing":
            model = safe_name(entry.get("drawing_model") or "型号待确认", "型号待确认")
            folder = drawing_folder(model, product_folders(place, namer.forbidden), match, namer.forbidden)
            prefix, label = model, "尺寸图"
            use, alt = CHECK, "尺寸见原图"
            fields["dimension_drawing"] = True
            fields["product"] = fields["product"] or model_product(model, match)
        if entry.get("drawing") and drawing_pages[entry["item_id"]] > 1:
            page = entry.get("page") or 1
            label = f"{label}-第{page}页" if label else f"第{page}页"
            if entry.get("id"):
                fields["short"] = short_hash(entry["id"], 8)
        location = "/".join(media_folders(entry["relpath"], namer)) or "根目录"
        if entry.get("id"):
            add(entry["id"], "", "", folder=folder, prefix=prefix, label=label,
                alt=alt or " ".join(p for p in (prefix, label) if p), use=use, english_source=stem, location=location,
                source_relpath=entry["relpath"], **fields)
        if entry.get("cover"):
            add(entry["cover"], "", "", folder=folder, prefix=prefix, label=f"{label}-封面" if label else "视频封面",
                alt=f"视频封面：{' '.join(p for p in (prefix, label) if p)}", use=PUBLIC, english_source=stem, location=location)
    return found


def load_media(build: Path) -> list[dict[str, Any]]:
    return load_json(build / MEDIA_LIST) if (build / MEDIA_LIST).is_file() else []


PACKAGE_LEVELS = 6
PACKAGE_PATH_CHARS = 100


def media_folders(relpath: str, namer: Namer) -> list[str]:
    """The folders the file sits in inside the package, as the client filed them: a folder repeated
    inside itself counted once, number-only folders and serial prefixes dropped (clean_levels), and
    the top ones dropped only when the path would be too long for Windows. One naming a customer
    becomes 其他. Nothing here decides which product a picture belongs to."""
    levels = clean_levels(parts_of(relpath), keep=0)
    while levels and (len(levels) > PACKAGE_LEVELS or len("/".join(levels)) > PACKAGE_PATH_CHARS):
        levels = levels[1:]
    return [("其他" if namer.forbidden and namer.forbidden.search(p) else safe_name(p, "其他")) for p in levels]


def named_folders(relpath: str, namer: Namer) -> tuple[str, list[str]] | None:
    """(证书 or 工厂与设备, the folder that says so and those below it), when a folder plainly says so."""
    parts = [p for p in relpath.replace("\\", "/").split("/")[:-1] if p]
    for index in range(len(parts) - 1, -1, -1):
        kind = named_kind(parts[index])
        if kind:
            kept = clean_levels(parts[index:], keep=MEDIA_DEPTH)
            return kind, [("其他" if namer.forbidden and namer.forbidden.search(p) else safe_name(p, "其他")) for p in kept]
    return None


def drawing_folder(model: str, placed: list[str] | None, match: Callable[..., dict[str, Any] | None] | None,
                   forbidden: re.Pattern[str] | None) -> str:
    """Where an AI-classified dimension drawing goes: with its product's drawings, else under its model.
    placed is 大类/产品 (product_folders), when the picture's own place already named the product."""
    if not placed and match and model != "型号待确认":
        product = match(model)
        if product and product.get("kind") in {"product", "solution"}:
            placed = product_folders({"product": product}, forbidden)
    return "/".join([MEDIA_ROOT, *placed, "尺寸图"] if placed else ["产品", model, "尺寸图"])


def product_key(product: dict[str, Any] | None) -> tuple[str, str] | None:
    """(大类, 产品) for the index's 按产品 section."""
    if not product or product.get("kind") not in {"product", "solution", None}:
        return None
    return (product.get("category") or product["name"], product["name"])


def model_product(model: str, match: Callable[..., dict[str, Any] | None] | None) -> tuple[str, str] | None:
    return product_key(match(model)) if match and model != "型号待确认" else None


def layout_of(build: Path, saved: dict[str, Any]) -> dict[str, Any]:
    """The naming rules in force. A project with no names yet starts on the current layout; one
    named before keeps its names (layout 1) until relayout switches it over."""
    path = build / LAYOUT
    if path.is_file():
        return load_json(path)
    if not saved:
        layout = {"layout": CURRENT_LAYOUT, "applied": "新项目，一开始就用这一版"}
        write_json(path, layout)
        return layout
    return {"layout": 1}


def relayout(out: Path) -> dict[str, Any]:
    """Switch a project to the current layout: names made by the rules are made again on the next
    build; what a person or the model wrote about a picture (its description) stays."""
    build = out / BUILD_DIRECTORY
    saved = load_json(build / STATE) if (build / STATE).is_file() else {}
    current = layout_of(build, saved)
    if current.get("layout") == CURRENT_LAYOUT and current.get("applied"):
        return {"layout": CURRENT_LAYOUT, "changed": False, "next": "已经是这一版的名字，不用迁移"}
    from datetime import datetime
    write_json(build / LAYOUT, {"layout": CURRENT_LAYOUT, "requested_at": datetime.now().isoformat(timespec="seconds"),
                                "applied": None})
    return {"layout": CURRENT_LAYOUT, "changed": True, "pictures_named_before": len(saved),
            "next": "重跑 run_pipeline.py 按新规则起名，再用 publish_vault.py --dry-run 看会搬哪些图，然后正式发布"}


def assign(found: list[dict[str, Any]], saved: dict[str, Any], answers: dict[str, Any], namer: Namer,
           layout: int = 1) -> dict[str, dict[str, Any]]:
    """id -> the picture's place in 90_素材 and what to say about it. The first use of a picture wins.
    Names given under an older layout do not hold once the project moved to this one."""
    entries: dict[str, dict[str, Any]] = {}
    for item in found:
        if item["id"] in entries:
            entries[item["id"]]["more"] += 1
            continue
        suffix = Path(item["id"]).suffix.casefold()
        short = item.get("short") or item["id"][4:12]
        named_by, label, alt = (FROM_SOURCE, item["label"], item["alt"]) if item["label"] else (NUMBERED, "", item["alt"])
        answer = answers.get(item["id"]) or {}
        if answer.get("drop"):
            continue  # The AI saw a person, a customer's mark or a price: not material.
        if answer.get("name"):
            # What the model saw describes the picture; the file name stays structural, so it never
            # changes because a picture was looked at.
            alt = answer.get("alt") or answer["name"]
            item["english_source"] = answer.get("english") or answer["name"]
            item["use"] = CHECK if item["use"] == PUBLIC else item["use"]
            item["described_by"] = FROM_AI
        stem = safe_name("-".join(p for p in (item["prefix"], label) if p) or "图片")
        file = f"{item['folder']}/{stem}-{short}{suffix}"
        proposed_file = file
        old = saved.get(item["id"])
        if old and layout >= CURRENT_LAYOUT and old.get("layout", 1) < CURRENT_LAYOUT:
            old = None  # Named by the old rules: this project moved to the current layout.
        # A name stays once given; only a plain numbered name gives way to a better one.
        if old and old.get("file") and not (old.get("named_by") == NUMBERED and named_by != NUMBERED):
            file, named_by = old["file"], old.get("named_by", named_by)
            if item.get("dimension_drawing"):
                # An explicit visual classification changes the asset's category;
                # keep its stable basename and let official publish move it.
                file = f"{item['folder']}/{Path(file).name}"
            file, named_by = repair_legacy_picture_name(
                file, named_by, item, old, namer, proposed_file, short, suffix, FROM_SOURCE, NUMBERED
            )
        entries[item["id"]] = {**item, "file": file, "named_by": named_by, "alt": alt or label, "more": 0,
                               "english": english_name({**item, "alt": alt or label}, namer, suffix)}
    taken: dict[str, str] = {}
    for picture_id, entry in sorted(entries.items()):
        key = Path(entry["file"]).name.casefold()
        if key in taken:  # Two ids sharing eight characters: the second gets the whole id.
            entry["file"] = f"{Path(entry['file']).parent.as_posix()}/{Path(entry['file']).stem[:-8]}{picture_id[4:]}"
        taken[Path(entry["file"]).name.casefold()] = picture_id
    return entries


def product_section(entries: dict[str, dict[str, Any]]) -> list[str]:
    """按产品: every picture under the product it belongs to, however it got there — package folders,
    the page's only company code or its document's product, a drawing's model, a website category."""
    by_product: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unplaced: list[dict[str, Any]] = []
    for entry in entries.values():
        product = entry.get("product")
        (by_product[tuple(product)] if product else unplaced).append(entry)
    if not by_product and not unplaced:
        return []
    lines = ["## 按产品", "", "同一张图只列在一个产品下。对不上任何产品的列在最后的“未对应产品”。", ""]
    for (category, name), rows in sorted(by_product.items()):
        title = name if category == name else f"{category} / {name}"
        lines += [f"### {title}（{len(rows)} 张）", ""]
        lines += [f"- [[{Path(e['file']).name}]]" for e in sorted(rows, key=lambda e: e["file"])]
        lines.append("")
    if unplaced:
        lines += [f"### 未对应产品（{len(unplaced)} 张）", "",
                  "按文件夹列出数量；要归到产品，把文件夹名或型号写进 kb-config.json 对应产品的 aliases。", ""]
        folders = Counter(Path(e["file"]).parent.as_posix() for e in unplaced)
        lines += [f"- {folder}：{n} 张" for folder, n in sorted(folders.items())]
        lines.append("")
    return lines


def write_index(build: Path, entries: dict[str, dict[str, Any]], docs: dict[str, dict[str, Any]], escape: Callable[[str], str],
                forbidden: re.Pattern[str] | None = None) -> None:
    """One page for people, one CSV for the website team; no previews, thousands of pictures make Obsidian crawl."""
    counts = defaultdict(int)
    for entry in entries.values():
        counts[entry["use"]] += 1
    lines = ["# 素材索引", "",
             "图片发布后一直保留，不随资料变化删除。文件放在哪、叫什么，按图片的来处生成，名字一旦定下不再变；"
             "图上是什么写在“说明”一列。买家款号和客户名不进名字。点文件名看图。", "",
             f"共 {len(entries)} 张：" + "，".join(f"{use} {counts[use]}" for use in (PUBLIC, CHECK, INTERNAL) if counts[use]) + "。", "",
             f"- **{PUBLIC}**：来自官网或公司画册，原文没有 ⚠，建站可以直接用；“说明”一列可以当 alt 文字。",
             f"- **{CHECK}**：原文有 ⚠ 或说明是 AI 看图写的，用前看一眼原图和原文。",
             f"- **{INTERNAL}**：报价单、产品表里的图（多是买家询价时发来的款式）和整页原图，不要上官网。",
             f"- **{MEDIA_ROOT}**：初筛按文件夹整批保留的图片（渲染图、白底图等），照搬初筛包里的目录：重复的层只留一层，"
             "纯数字的目录层去掉，带序号的只去掉序号，路径太长时只留最深的几层。视频、音频、较大的图片和设计稿留在初筛包，列在最后一节。",
             f"- **{EXTRACTED}**：资料里印的照片，按来源文档放，名字是“第N页”，页上只有一个公司款号时前面加款号。",
             f"- **{WEBSITE_FOLDER}**：官网页面上的图，按页面放。**{PAGES}**：资料的整页原图，按来源文档放。",
             "- **证书**、**工厂与设备**：只看资料标题或文件夹名，写明是证书、工厂的才放这里。",
             "- 说明还只有“第N页”的，可以让 AI 看图补一句说明（pictures.py export / collect），不改文件名。",
             "- 按产品查图看下面“按产品”一节。",
             f"- 建站用的清单（含英文文件名）：[[00_素材清单.csv]]。", ""]
    lines += product_section(entries)
    by_folder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries.values():
        by_folder[Path(entry["file"]).parent.as_posix()].append(entry)
    order = {name: i for i, name in enumerate(FOLDERS)}
    for folder in sorted(by_folder, key=lambda f: (order.get(f.split("/")[0], len(FOLDERS)), f)):
        rows = sorted(by_folder[folder], key=lambda e: e["file"])
        lines += [f"## {folder}（{len(rows)} 张）", "", "| 图片 | 说明 | 能否用于官网 | 名字来自 | 出现在 | 位置 |", "| --- | --- | --- | --- | --- | --- |"]
        for entry in rows:
            doc = docs.get(entry["doc_id"])
            link = (f"[[{Path(doc['file_name']).stem}#{entry['group']}|{doc['title']}]]" if doc else "资料包原文件夹") \
                + (f" 等 {entry['more'] + 1} 处" if entry["more"] else "")
            lines.append(f"| [[{Path(entry['file']).name}]] | {escape(entry['alt'])} | {entry['use']} | {entry['named_by']} | {link} "
                         f"| {escape(entry['location'])} |")
        lines.append("")
    retained = retained_media(build, entries, forbidden)
    lines += retained_section(retained, escape)
    write_text(build / "vault" / INDEX, "\n".join(lines).rstrip() + "\n")
    target = build / "vault" / LIST_CSV
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["文件", "说明（alt）", "能否用于官网", "英文文件名建议", "名字来自", "出处", "位置", "编号",
                         "类型", "大小", "原位置", "确认状态", "备注"])
        for entry in sorted(entries.values(), key=lambda e: e["file"]):
            source = docs[entry["doc_id"]]["title"] if entry["doc_id"] in docs else "资料包原文件夹"
            asset = build / "assets" / entry["id"]
            origin = Path(entry.get("source_relpath") or "").parent.as_posix() if entry.get("source_relpath") else ""
            writer.writerow([entry["file"], entry["alt"], entry["use"], entry["english"], entry["named_by"],
                             source, entry["location"], entry["id"], "图片", file_size(asset.stat().st_size) if asset.is_file() else "",
                             origin, entry["use"], ""])
        # Originals left in the package are material too: the website team needs to know they exist.
        for record in retained:
            use = PUBLIC if record["status"] == CONFIRMED_MEDIA else record["status"]
            writer.writerow([record["name"], "", use, "", "原文件名", "初筛包原文件（没有复制进知识库）", "", record["entry"]["item_id"],
                             record["kind"], record["size"], record["folder"], record["status"], record["note"]])


CONFIRMED_MEDIA, AWAITING_MEDIA, UNRENDERED_MEDIA = "已确认可用", "待客户确认", "待转图"
SHOWN_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MASKED_NAME = "（名字含客户或报价信息，见对照表）"
RETAINED_COLUMNS = ["名称", "类型", "大小", "原位置", "确认状态", "备注"]


def file_size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(size, 0) / 1024:.0f} KB"


def retained_record(entry: dict[str, Any], forbidden: re.Pattern[str] | None = None) -> dict[str, Any]:
    """One original screening kept that stays in the preparation package instead of 90_素材:
    a video or audio, a picture too large or in a format the vault cannot show, a drawing not
    rendered, or a folder still waiting for the client. Kept and not waiting means usable."""
    relpath = entry.get("relpath") or ""
    suffix = Path(relpath).suffix.casefold()
    if entry.get("status") == "drawing_unrendered":
        kind, note = "图纸", "没能转成图片"
    elif entry.get("kind") in {"video", "audio"}:
        kind, note = KIND_LABELS[entry["kind"]], "内容待转写，转写前不出知识点"
    elif suffix == ".psd":
        kind, note = "设计稿", "设计稿已保留，原件见原位置"
    elif suffix in {".dwg", ".dxf", ".step", ".stp", ".iges", ".igs"}:
        kind, note = "图纸", "图纸已保留，原件见原位置"
    elif entry.get("kind") == "image" and (suffix in SHOWN_IMAGES or Path(relpath).name.casefold() in SHOWN_IMAGES):
        kind = "图片"
        note = "大文件，见原位置" if (entry.get("bytes") or 0) > 5 * 1024 * 1024 else "留在初筛包原位，见原位置"
    elif entry.get("kind") == "image" and suffix not in SHOWN_IMAGES:
        kind, note = "图片", f"{suffix or '这种格式'} 知识库里显示不了"
    elif entry.get("kind") == "image":
        kind, note = "图片", "大文件，见原位置"
    else:
        kind, note = "素材", ""
    status = {"media_awaiting_client": AWAITING_MEDIA, "drawing_unrendered": UNRENDERED_MEDIA}.get(entry.get("status"), CONFIRMED_MEDIA)
    folder = Path(relpath).parent.as_posix()
    hidden = bool(relpath) and (bool(forbidden and forbidden.search(relpath)) or any(p.search(relpath) for p in HARD_PATTERNS.values()))
    source = Path(entry["package"]) / entry["package_path"] if entry.get("package") and entry.get("package_path") else None
    return {"name": MASKED_NAME if hidden else Path(relpath).name, "kind": kind, "bytes": entry.get("bytes") or 0,
            "size": file_size(entry.get("bytes") or 0), "folder": MASKED_NAME if hidden else (folder if folder != "." else "根目录"),
            "status": status, "note": note, "open": "" if hidden or not source else path_uri(source),
            "folder_uri": "" if hidden or not source else path_uri(source.parent)}


def retained_media(build: Path, entries: dict[str, dict[str, Any]], forbidden: re.Pattern[str] | None = None) -> list[dict[str, Any]]:
    """Every kept original not copied into 90_素材, with its record, in a stable order."""
    records = [{**retained_record(e, forbidden), "entry": e} for e in load_media(build) if e.get("id") not in entries]
    return sorted(records, key=lambda r: (r["status"] != CONFIRMED_MEDIA, r["folder"], r["name"]))


def retained_section(records: list[dict[str, Any]], escape: Callable[[str], str], heading: str = "##") -> list[str]:
    """Usable originals one row each; what still waits (the client, a rendering) one row per folder,
    because the client answers per folder and such folders can hold hundreds of photos."""
    if not records:
        return []
    usable = [r for r in records if r["status"] == CONFIRMED_MEDIA]
    waiting = [r for r in records if r["status"] != CONFIRMED_MEDIA]
    lines = [f"{heading} 留在初筛包里的素材", "",
             "视频、音频、较大的图片和设计稿不复制进知识库，原件留在初筛包。初筛按文件夹保留、不用等客户确认的，都算已确认可用。", ""]
    if usable:
        lines += [f"{heading}# 已确认可用（{len(usable)} 个）", "",
                  "| " + " | ".join(RETAINED_COLUMNS) + " | 打开 |", "|" + " --- |" * (len(RETAINED_COLUMNS) + 1)]
        for r in usable:
            links = " · ".join(filter(None, [f"[原件]({r['open']})" if r["open"] else "", f"[文件夹]({r['folder_uri']})" if r["folder_uri"] else ""]))
            lines.append(f"| {escape(r['name'])} | {r['kind']} | {r['size']} | {escape(r['folder'])} | {r['status']} | {r['note']} | {links or '见对照表'} |")
        lines.append("")
    if waiting:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in waiting:
            groups[(r["folder"], r["status"], r["folder_uri"])].append(r)
        lines += [f"{heading}# 还没处理（{len(waiting)} 个）", "",
                  "待客户确认的，客户回复“保留”后重跑就算可用，图片会进上面的素材文件夹；没能转成图片的图纸，原件照常可以打开。", "",
                  "| 原位置 | 内容 | 大小 | 确认状态 | 打开 |", "| --- | --- | ---: | --- | --- |"]
        for (folder, status, uri), members in sorted(groups.items()):
            kinds: dict[str, int] = defaultdict(int)
            for r in members:
                kinds[r["kind"]] += 1
            what = "、".join(f"{k} {n} 个" for k, n in kinds.items())
            size = file_size(sum(r["bytes"] for r in members))
            lines.append(f"| {escape(folder)} | {what} | {size} | {status} | {f'[打开文件夹]({uri})' if uri else '见对照表'} |")
        lines.append("")
    return lines


def build_pictures(build: Path, config: dict[str, Any], docs: dict[str, dict[str, Any]], by_doc: dict[str, list[dict[str, Any]]],
                   order: list[str], statuses: dict[str, str], unsafe: set[tuple[str, str]], status_info: dict[str, Any],
                   forbidden: re.Pattern[str] | None, max_row_images: int, held: set[tuple[str, str]],
                   places: dict[str, Any] | None = None, match: Callable[..., dict[str, Any] | None] | None = None,
                   products: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    namer = Namer(config, forbidden)
    # Which product a document belongs to, by the rule the curated pages use: deepest folder first.
    from curate import place_docs
    doc_places = place_docs({d: docs[d] for d in order}, [p for p in products or [] if p.get("kind") in {"product", "solution", None}])
    found = candidates(build, docs, by_doc, order, statuses, unsafe, status_info, namer, max_row_images, held, places, match,
                       doc_places, set(config.get("excluded_image_ids", [])))
    saved = load_json(build / STATE) if (build / STATE).is_file() else {}
    answers = load_json(build / AI_ANSWERS) if (build / AI_ANSWERS).is_file() else {}
    layout = layout_of(build, saved).get("layout", 1)
    entries = assign(found, saved, answers, namer, layout)
    # Names given before stay known even when a picture is not shown this time.
    mark = {"layout": layout} if layout >= CURRENT_LAYOUT else {}
    write_json(build / STATE, {**saved, **{i: {"file": e["file"], "named_by": e["named_by"], **mark} for i, e in entries.items()}})
    write_json(build / "pictures.json", {i: {**{k: e[k] for k in ("file", "alt", "use", "named_by", "english", "doc_id", "group")},
                                             **({"described_by": e["described_by"]} if e.get("described_by") else {}),
                                             **({"bare": True} if e.get("bare") else {}),
                                             **({"product": list(e["product"])} if e.get("product") else {})}
                                          for i, e in entries.items()})
    return entries


README = """# 看图写说明

`todo.jsonl` 每行一张图：`id`、图片路径 `path`、它在哪份资料哪一页 `source`。这些图旁边没有能用来描述它的文字。

逐张打开图片看，写进同目录的 `answers.jsonl`，每行一个：
{"id": "img-…", "name": "提花挂毯 大象图案", "alt": "大象图案机织提花挂毯，挂在客厅墙上", "english": "elephant jacquard wall tapestry"}

规则：
1. 只写图里看得见的东西：产品是什么、颜色、图案、场景。看不出的不写，不猜面料、克重、款号、品牌。
2. 图里有人名、客户名、品牌标志、价格的，name 写“不用”，这张图不进素材。
3. name 不超过 20 个字，alt 一句话。english 是给网站文件名用的英文，几个词。
写的内容只进素材索引的“说明”和英文文件名建议，不改图片文件名：文件名按图片所在位置生成，一直不变。
写完运行 `pictures.py collect --out <运行目录>`，再重跑构建。AI 写的说明在索引里标“待核对”。
"""


def export(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    pictures = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    # Pictures nothing describes yet: a numbered name, or a page with no heading and no company code.
    todo = [{"id": i, "path": str(build / "assets" / i),
             "source": f"{docs[e['doc_id']]['title']} {e['group']}" if e["doc_id"] in docs else "资料包原文件夹"}
            for i, e in sorted(pictures.items())
            if e["use"] != INTERNAL and not e.get("described_by") and (e["named_by"] == NUMBERED or e.get("bare"))]
    folder = build / AI_FOLDER
    write_jsonl(folder / "todo.jsonl", todo)
    write_text(folder / "README.md", README)
    return {"todo": len(todo), "folder": str(folder)}


def collect(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    path = build / AI_FOLDER / "answers.jsonl"
    answers = load_json(build / AI_ANSWERS) if (build / AI_ANSWERS).is_file() else {}
    todo = {row["id"] for row in load_jsonl(build / AI_FOLDER / "todo.jsonl")} if (build / AI_FOLDER / "todo.jsonl").is_file() else set()
    taken, skipped = 0, []
    for row in load_jsonl(path) if path.is_file() else []:
        if row.get("id") not in todo:
            skipped.append(row.get("id"))
            continue
        if (row.get("name") or "").strip() == "不用":
            answers[row["id"]] = {"name": "", "drop": True}
        else:
            answers[row["id"]] = {k: (row.get(k) or "").strip()[:80] for k in ("name", "alt", "english")}
        taken += 1
    write_json(build / AI_ANSWERS, answers)
    return {"collected": taken, "not_in_todo": skipped[:10]}


def restore_names(out: Path, vault: Path, picture_ids: list[str]) -> dict[str, Any]:
    """Restore selected automatic names in build state only; publishing moves assets later.

    Validate the entire request before writing, and never replace an AI/human name.
    """
    from kb_state import STATE as VAULT_STATE

    build = out / BUILD_DIRECTORY
    saved = load_json(build / STATE)
    published = load_json(vault / VAULT_STATE / STATE)
    assets = (vault / ASSETS).resolve()
    restored = {}
    for picture_id in dict.fromkeys(picture_ids):
        if Path(picture_id).name != picture_id:
            raise ValueError(f"Invalid picture id: {picture_id}")
        old, current = published.get(picture_id, {}), saved.get(picture_id, {})
        if not old.get("file") or not current.get("file"):
            raise ValueError(f"Missing picture name: {picture_id}")
        if any(entry.get("named_by") not in {NUMBERED, FROM_SOURCE} for entry in (old, current)):
            raise ValueError(f"Protected picture name: {picture_id}")
        original = (assets / old["file"]).resolve()
        if not original.is_relative_to(assets):
            raise ValueError(f"Picture path outside assets: {picture_id}")
        cached = build / "assets" / picture_id
        if not original.is_file() or not cached.is_file() or sha256_file(original) != sha256_file(cached):
            raise ValueError(f"Picture hash mismatch: {picture_id}")
        restored[picture_id] = {**current, "file": old["file"], "named_by": old["named_by"]}
    if restored:
        write_json(build / STATE, {**saved, **restored})
    return {"restored": len(restored), "ids": list(restored), "published_vault_modified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Let an AI name the pictures that have no words beside them.")
    parser.add_argument("command", choices=["export", "collect", "restore-names", "relayout"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--vault", help="Published vault to read when restoring names")
    parser.add_argument("--id", action="append", dest="picture_ids", default=[], help="Picture id to restore (repeatable)")
    args = parser.parse_args()
    if not run_lock(Path(args.out).resolve(), f"pictures.py {args.command}"):
        return 3
    out = Path(args.out).resolve()
    if args.command == "restore-names":
        if not args.vault or not args.picture_ids:
            parser.error("restore-names requires --vault and at least one --id")
        result = restore_names(out, Path(args.vault).resolve(), args.picture_ids)
    elif args.command == "relayout":
        result = relayout(out)
    else:
        result = export(out) if args.command == "export" else collect(out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
