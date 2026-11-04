#!/usr/bin/env python3
"""Decide what enters the knowledge base and turn it into source-located blocks.

Nothing here rewrites content. Text is carried over as extracted, and spreadsheets
keep only columns that are recognised as product fields, so a column nobody
recognised is dropped rather than published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from fetch_website import module_for as web_module, page_kind as web_kind
from office import convert_office
from collections import Counter, defaultdict
from kb_common import (
    RESOURCE_LABELS,
    BUILD_DIRECTORY,
    MANIFEST_NAME,
    MODULES,
    POINTS_NAME,
    ROLE_ORDER,
    TIERS,
    TRIAGE_NAME,
    load_config,
    load_json,
    load_jsonl,
    load_units,
    long_path,
    normalize_space,
    safe_name,
    short_hash,
    source_stem,
    write_json,
    write_jsonl,
)


SPREADSHEET = {".xlsx", ".xls", ".csv", ".tsv"}
THIRD_PARTY_TYPES = {"competitor"}
OPERATIONS_TYPES = {"seo_ops"}
EXCLUDED_COPY_STATUS = {"not_copied_restricted", "withheld_by_client", "copy_failed", "missing_decision"}

CELL = re.compile(r"(?:^|(?<=\n)|(?<=; ))([A-Z]{1,3})(\d+)=")
LOCATOR = re.compile(
    r"^(?:(?P<sheet>[^!]+)!)?(?:"
    r"(?P<col1>[A-Z]{1,3}):(?P<col2>[A-Z]{1,3})"
    r"|第\s*(?P<row1>\d+)\s*[–\-~至]\s*(?P<row2>\d+)\s*行"
    r"|(?P<cell1>[A-Z]{1,3})(?P<cellrow1>\d+)(?::(?P<cell2>[A-Z]{1,3})(?P<cellrow2>\d+))?"
    r")"
)
SHEET_UNIT = re.compile(r"^sheet (?P<name>.+) \((?P<state>[^)]+)\)$")
HEADING = re.compile(r"^(?:第[一二三四五六七八九十\d]+[章节部分]|[一二三四五六七八九十]+[、.]|\d+(?:\.\d+)*[、.\s])")
PAGE_RANGE = re.compile(r"第\s*(\d+)(?:\s*[–\-~至]\s*(\d+))?\s*页")

# Order matters: "面料报价" must count as a price before it can count as fabric.
DENY_HEADER = re.compile(
    r"(?i)price|cost|usd|rmb|fob|cif|exw|qty|quantity|moq|amount|total|金额|价|数量|起订|"
    r"备注|remark|note|comment|customer|client|buyer|客户|date|日期|业务|sales|supplier|vendor|"
    r"供应商|factory|工厂|target|payment|delivery|交期|lead\s*time|sample|样品|序号|^no\.?$|^s/?n$|"
    r"公司|company|联系|contact|country|国家|邮箱|e-?mail|背调|人民币|含税|时间|"
    r"包装|packing|外箱|carton|\bctn\b|单位|\bunit\b"
)
CUSTOMER_HEADER = re.compile(r"(?i)customer|client|buyer|客户|supplier|vendor|供应商|factory|工厂|公司|company|联系人|contact")
ROLE_HEADERS = [
    ("picture", re.compile(r"(?i)picture|photo|image|图片|款式图|^图$|^pic")),
    ("fabric", re.compile(r"(?i)fabric|material|composition|面料|面辅料|用料|成分|克重|gsm")),
    ("size", re.compile(r"(?i)size|尺寸|尺码|规格")),
    ("description", re.compile(r"(?i)descri|decri|描述|说明|特点|工艺")),
    ("style", re.compile(r"(?i)item\s*(no|#|code)|style|款号|款式编号|货号|art(icle)?\s*(no|#)|model|型号")),
    ("name", re.compile(r"(?i)product|品名|产品|款式|名称|name|garment|item")),
]
COMPANY_NAME = re.compile(r"(?i)公司|简介|profile|about")
MAX_HEADER_SCAN = 80
# Stricter than these notes already: rows above the header and unrecognised columns never pass.
COVERED_BY_ALLOWLIST = re.compile(r"表头|全部价格|逐表确认")
CELL_ERROR = re.compile(r"^(?:#[A-Z/0!?]+!?|=DISPIMG\(.*)$")
SHORT_TITLE = 20
SAMPLE_ROWS = 4
SERIAL = re.compile(r"\d{1,3}")
# Phrases a compiled reference document uses about itself.
REFERENCE_MARK = re.compile(
    r"(?i)内部审核版|审核稿|草稿|公开参考|参考记录|参考款|参考供应商|参考数据|同类产品参考|仅供参考|"
    r"internal review draft|\bdraft\b|public reference|reference (?:listing|data|record)s?|for reference only"
)
REFERENCE_MIN = 3
# Columns a price list has and a product list does not.
QUOTE_HEADER = re.compile(r"(?i)price|报价|单价|价格|qty|quantity|数量|amount|金额|total|moq|起订")
OCR_SUFFIX = " ocr"

SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELATION_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
BLIP_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def extension_of(name: str) -> str:
    return Path(name).suffix.casefold()


def route_asset(asset: dict[str, Any], answers: dict[str, str]) -> dict[str, str]:
    """Default deny: an item enters only through an explicit branch below."""
    answer = answers.get(asset["item_id"])
    extension = extension_of(asset.get("display_name") or "")
    resource = asset.get("resource_type")
    decision = asset.get("decision")
    if answer == "drop":
        return {"route": "skip", "reason": "client_said_no"}
    if asset.get("copy_status") == "duplicate":
        return {"route": "skip", "reason": "duplicate"}
    if asset.get("copy_status") in EXCLUDED_COPY_STATUS:
        return {"route": "skip", "reason": asset.get("copy_status") or "not_copied"}
    if resource in THIRD_PARTY_TYPES:
        return {"route": "skip", "reason": "third_party_material"}
    if resource in OPERATIONS_TYPES:
        return {"route": "skip", "reason": "writing_operations_material"}
    if decision == "E":
        return {"route": "skip", "reason": "excluded"}
    if decision == "D":
        if answer == "keep":
            official = resource == "marketing" and extension in {".pdf", ".pptx"}
            return {"route": "kb", "reason": "resolved_by_answer", "tier": "T1" if official else "T2"}
        return {"route": "hold", "reason": "unresolved_item"}
    if decision == "B":
        return {"route": "kb", "reason": "cleaned_from_b", "tier": "T3"}
    if decision == "C":
        return {"route": "points", "reason": "knowledge_points", "tier": "T4"}
    if decision == "A":
        if asset.get("asset_kind") == "image":
            return {"route": "hold", "reason": "image_needs_review"}
        official = resource == "marketing" and extension in {".pdf", ".pptx"}
        tier = "T1" if official else "T2"
        if not asset.get("usable_as_fact") and answer != "keep":
            return {"route": "kb", "reason": "awaiting_client", "tier": tier, "pending_all": True}
        return {"route": "kb", "reason": "retained", "tier": tier}
    return {"route": "hold", "reason": "unknown_decision"}


TITLE_NOISE = [re.compile(p) for p in (r"\s*\(\d+\)$", r"[_\s-]*\d{8,}$", r"[\s_-]*副本$", r"^复制[_\s-]*")]
FOLDERS = {"T3": "产品表", "T4": "业务要点"}


def clean_name(name: str) -> str:
    for pattern in TITLE_NOISE:
        name = pattern.sub("", name)
    return normalize_space(name.replace("_", " ")) or name


def package_title(asset: dict[str, Any], tier: str, overrides: dict[str, str]) -> tuple[str, str]:
    """Return (title, folder). Titles read 类别-名称 so a reader knows what a file is."""
    if asset["item_id"] in overrides:
        title = overrides[asset["item_id"]]
        return title, title.split("-", 1)[0] if "-" in title else "公司资料"
    if tier == "T3":
        return f"产品表-{asset['source_hash'][:8]}", FOLDERS["T3"]
    name = clean_name(source_stem(asset.get("display_name") or asset["item_id"]))
    if COMPANY_NAME.search(name):
        return f"公司资料-{name}", "公司资料"
    if tier == "T1":
        return f"产品画册-{name}", "产品画册"
    return f"公司资料-{name}", "公司资料"


def module_for(asset: dict[str, Any], tier: str) -> str:
    name = asset.get("display_name") or ""
    if tier == "T3":
        return "product"
    if asset.get("resource_type") == "business_scenario":
        return "solution"
    if COMPANY_NAME.search(name):
        return "company"
    return "product"


def header_role(text: str, header_map: dict[str, str]) -> str:
    """Return a kept role, or `drop`. An unrecognised header is `unknown`, and also dropped."""
    key = normalize_space(text)
    if key in header_map:
        return header_map[key]
    if not key or len(key) > 40:
        return "unknown"
    if DENY_HEADER.search(key):
        return "drop"
    for role, pattern in ROLE_HEADERS:
        if pattern.search(key):
            return role
    return "unknown"


def column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number


def parse_sheet_rows(text: str) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    matches = list(CELL.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].rstrip().removesuffix(";").strip()
        if value:
            rows.setdefault(int(match.group(2)), {})[match.group(1)] = value
    return rows


def parse_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rules, unreadable = [], []
    for action in actions:
        for locator in action.get("locators", []):
            match = LOCATOR.match(locator.strip())
            if COVERED_BY_ALLOWLIST.search(locator):
                continue
            if not match:
                unreadable.append(locator)
                continue
            parts = match.groupdict()
            rule: dict[str, Any] = {"sheet": parts["sheet"]}
            if parts["col1"]:
                # A column without a sheet was read off one sheet only, and the same
                # letter is a product column elsewhere. Headers already drop price columns.
                if not parts["sheet"]:
                    continue
                rule["columns"] = (column_number(parts["col1"]), column_number(parts["col2"]))
            elif parts["row1"]:
                rule["rows"] = (int(parts["row1"]), int(parts["row2"]))
            else:
                last_column = parts["cell2"] or parts["cell1"]
                rule["columns"] = (column_number(parts["cell1"]), column_number(last_column))
                rule["rows"] = (int(parts["cellrow1"]), int(parts["cellrow2"] or parts["cellrow1"]))
            rules.append(rule)
    return rules, unreadable


def removed(rules: list[dict[str, Any]], sheet: str, column: str | None, row: int | None) -> bool:
    for rule in rules:
        if rule["sheet"] not in (None, sheet):
            continue
        in_columns = "columns" not in rule or (
            column is not None and rule["columns"][0] <= column_number(column) <= rule["columns"][1]
        )
        in_rows = "rows" not in rule or (
            row is not None and rule["rows"][0] <= row <= rule["rows"][1]
        )
        if in_columns and in_rows and (column is not None or "columns" not in rule):
            return True
    return False


def find_header(
    rows: dict[int, dict[str, str]],
    rules: list[dict[str, Any]],
    sheet: str,
    header_map: dict[str, str],
) -> tuple[int, dict[str, str]] | None:
    for number in sorted(rows)[:MAX_HEADER_SCAN]:
        if removed(rules, sheet, None, number):
            continue
        roles = {column: header_role(value, header_map) for column, value in rows[number].items()}
        kept = [role for role in roles.values() if role in ROLE_ORDER]
        denied = [role for role in roles.values() if role == "drop"]
        if len(kept) >= 2 or (kept and denied):
            return number, roles
    return None


SITE_COPY = re.compile(r"(?i)(?:-\d+|-?%e5%a4%8d%e5%88%b6|-?复制)$")
HEADING_ONLY = {"products", "product details", "related products", "product description"}


def segments(url: str) -> list[str]:
    from urllib.parse import unquote, urlparse

    return [unquote(part) for part in urlparse(url).path.split("/") if part] or [""]


def heading_only(text: str) -> bool:
    lines = {normalize_space(line) for line in text.splitlines() if line.strip()}
    return len(lines) <= 1 and (not lines or next(iter(lines)).casefold() in HEADING_ONLY or len(text.splitlines()) > 1)


def strip_site_name(title: str, own_terms: list[str]) -> str:
    for term in sorted(own_terms, key=len, reverse=True):
        title = re.sub(rf"\s*[-–|]\s*{re.escape(term)}\s*$", "", title, flags=re.IGNORECASE)
    return normalize_space(title)


def template_signature(headers: dict[str, str]) -> str:
    """Same headers in the same columns is the same template, whatever the file is called."""
    return short_hash("|".join(f"{column}={normalize_space(value)}" for column, value in sorted(headers.items())))


def sheet_blocks(
    doc_id: str,
    item_id: str,
    sheet: str,
    label: str,
    text: str,
    rules: list[dict[str, Any]],
    header_map: dict[str, str],
    images: dict[int, list[str]],
    stats: dict[str, Any],
    terms: list[dict[str, str]],
    column_overrides: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    rows = parse_sheet_rows(text)
    found = find_header(rows, rules, sheet, header_map)
    if not found:
        stats["sheets_without_header"].append(sheet)
        return []
    header_row, roles = found
    headers = rows[header_row]
    signature = template_signature(headers)
    roles.update(column_overrides.get(signature, {}))
    if signature not in stats["templates"]:
        sample_rows = [rows[n] for n in sorted(rows) if n > header_row][:SAMPLE_ROWS]
        stats["templates"][signature] = {"headers": headers, "roles": roles, "samples": sample_rows,
                                         "reviewed": signature in column_overrides}
    for column, role in roles.items():
        if role == "unknown":
            stats["unknown_headers"].append(normalize_space(headers[column]))

    # Whatever sits above the table names the customer and the salesperson.
    for number in sorted(rows):
        if number < header_row or removed(rules, sheet, None, number):
            for value in rows[number].values():
                terms.append({"term": normalize_space(value), "item_id": item_id, "origin": "above_table"})
    customer_columns = [c for c, value in headers.items() if CUSTOMER_HEADER.search(value)]

    kept_columns = {
        column: role
        for column, role in roles.items()
        if role in ROLE_ORDER and not removed(rules, sheet, column, header_row)
    }
    blocks = []
    for number in sorted(rows):
        if number <= header_row or removed(rules, sheet, None, number):
            continue
        for column in customer_columns:
            if column in rows[number]:
                terms.append({"term": normalize_space(rows[number][column]), "item_id": item_id, "origin": "customer_column"})
        cells: dict[str, str] = {}
        for column, role in kept_columns.items():
            value = rows[number].get(column)
            if value and not CELL_ERROR.match(value) and not removed(rules, sheet, column, number):
                value = normalize_space(value)
                cells[role] = f"{cells[role]} / {value}" if role in cells else value
        if not ({"description", "fabric", "style"} & set(cells)):
            continue
        # A sheet name can be a customer name, so published labels are neutral.
        blocks.append(
            {
                "block_id": short_hash(f"{item_id}|{sheet}!{number}"),
                "doc_id": doc_id,
                "kind": "row",
                "group": label,
                "locator": f"{label} 第 {number} 行",
                "source_locator": f"{sheet}!{number}",
                "text": " | ".join(cells[role] for role in ROLE_ORDER if role in cells),
                "cells": cells,
                "images": images.get(number, []),
            }
        )
    # Quotations number their lines 1, 2, 3 under ITEM NO; that is a line number, not a style.
    styles = [b["cells"].get("style", "") for b in blocks]
    if styles and all(SERIAL.fullmatch(style) for style in styles if style) and any(styles):
        stats["serial_columns"] = stats.get("serial_columns", 0) + 1
        for block in blocks:
            block["cells"].pop("style", None)
            block["text"] = " | ".join(block["cells"][role] for role in ROLE_ORDER if role in block["cells"])
        blocks = [b for b in blocks if {"description", "fabric"} & set(b["cells"])]
    if any(QUOTE_HEADER.search(value) for value in headers.values()):
        stats.setdefault("quotation_sheets", []).append(sheet)
    stats["rows_kept"] += len(blocks)
    return blocks


def group_for(unit: dict[str, str], heading: str) -> str:
    locator = unit["locator"]
    # Columns of one page ("page 3 col 2") stay under that page's heading.
    match = re.fullmatch(r"page (\d+)(?: col \d+)?(?: ocr)?", locator)
    if match:
        return f"第 {match.group(1)} 页"
    match = re.fullmatch(r"(slide|notes) (\d+)(?: col \d+)?", locator)
    if match:
        return f"第 {match.group(2)} 张幻灯片" + ("备注" if match.group(1) == "notes" else "")
    match = SHEET_UNIT.match(locator)
    if match:
        return f"表 {match.group('name')}"
    if locator.startswith("section "):
        return unit["text"].splitlines()[0][:60]
    return heading


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def ocr_additions(units: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only what OCR found beyond the text layer: usually product names in pictures."""
    def comparable(text: str) -> str:
        # OCR Markdown bullets and full-width punctuation are formatting, not new facts.
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"(?:^|\s)(?:\\?[-*•·])\s+", " ", text)
        return squeezed(text).casefold()

    native: dict[str, str] = defaultdict(str)
    for unit in units:
        if not unit["locator"].endswith(OCR_SUFFIX):
            # Every column of a page counts as what the text layer already has.
            native[re.sub(r" col \d+$", "", unit["locator"])] += comparable(unit["text"])
    result = []
    for unit in units:
        if not unit["locator"].endswith(OCR_SUFFIX):
            result.append(unit)
            continue
        known = native.get(unit["locator"][: -len(OCR_SUFFIX)], "")
        fresh = [line for line in unit["text"].splitlines() if comparable(line) and comparable(line) not in known]
        if fresh:
            result.append({**unit, "text": "\n".join(fresh)})
    return sorted(result, key=lambda u: (unit_order(u["locator"]), u["locator"].endswith(OCR_SUFFIX)))


def reference_draft(units: list[dict[str, str]], role: str | None) -> bool:
    """A knowledge list or draft built from public listings says so about itself, again and again."""
    if role in {"reference", "original"}:
        return role == "reference"
    text = "\n".join(unit["text"] for unit in units)
    return len(REFERENCE_MARK.findall(text)) >= REFERENCE_MIN


def unit_order(locator: str) -> int:
    match = re.search(r"\d+", locator)
    return int(match.group(0)) if match else 0


def document_blocks(doc_id: str, item_id: str, units: list[dict[str, str]]) -> list[dict[str, Any]]:
    blocks, heading = [], "正文"
    has_ocr = any(u["locator"].endswith(OCR_SUFFIX) for u in units)
    if has_ocr:
        units = ocr_additions(units)
    for unit in units:
        text = unit["text"].replace("\r\n", "\n").strip()
        if not text:
            continue
        first_line = text.splitlines()[0].strip()
        if unit["locator"].startswith("para ") and len(first_line) <= 40 and HEADING.match(first_line):
            heading = first_line
        blocks.append(
            {
                "block_id": short_hash(f"{item_id}|{unit['locator']}"),
                "doc_id": doc_id,
                "kind": "text",
                "group": group_for(unit, heading),
                "locator": unit["locator"],
                "text": text,
                "ocr": unit["locator"].endswith(OCR_SUFFIX),
                # Read by the model from a picture, not by MinerU: published with a mark.
                "ai_read": unit.get("engine") == "ai",
            }
        )
    return blocks


def titled_section(blocks: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    """Find an unnumbered subheading and the paragraphs up to the next one."""
    start = next((i for i, b in enumerate(blocks) if title and normalize_space(b["text"]).replace(" ", "") == title), None)
    if start is None:
        return []
    section = [blocks[start]]
    for block in blocks[start + 1:]:
        text = normalize_space(block["text"])
        if len(text) <= SHORT_TITLE and "。" not in text:
            break
        section.append(block)
    return section


def hold_pending_units(
    blocks: list[dict[str, Any]], locations: list[str], answers: dict[str, str] | None = None
) -> tuple[int, list[str]]:
    """Hold back what the client has not answered. Returns the count and unmatched places."""
    held, unmatched = 0, []
    answers = answers or {}
    for location in locations:
        if answers.get(location) == "keep":
            continue
        pages = PAGE_RANGE.search(location)
        wanted: set[str] = set()
        if pages:
            first, last = int(pages.group(1)), int(pages.group(2) or pages.group(1))
            wanted = {f"第 {page} 页" for page in range(first, last + 1)}
        else:
            token = re.sub(r"第\s*\S+\s*章|一节|部分|章节|\s", "", location)
            wanted = {b["group"] for b in blocks if token and token in normalize_space(b["group"]).replace(" ", "")}
        matches = [block for block in blocks if block["group"] in wanted]
        if not matches and not pages:
            matches = titled_section(blocks, token)
        if not matches:
            unmatched.append(location)
        for block in matches:
            block["client_hold"] = location
            if answers.get(location) == "drop":
                block["client_answer"] = "drop"
            held += 1
    return held, unmatched


def drawing_targets(archive: zipfile.ZipFile, source: str, relation_type: str) -> dict[str, str]:
    folder, name = source.rsplit("/", 1)
    rels = f"{folder}/_rels/{name}.rels"
    if rels not in archive.namelist():
        return {}
    targets = {}
    for relation in ET.fromstring(archive.read(rels)).findall(f"{PACKAGE_REL_NS}Relationship"):
        if relation_type not in relation.get("Type", ""):
            continue
        target = relation.get("Target", "")
        if target.startswith("/"):
            targets[relation.get("Id")] = target.lstrip("/")
        else:
            parts = folder.split("/")
            for piece in target.split("/"):
                if piece == "..":
                    parts.pop()
                elif piece != ".":
                    parts.append(piece)
            targets[relation.get("Id")] = "/".join(parts)
    return targets


def workbook_images(path: Path, assets: Path, max_bytes: int) -> dict[str, dict[int, list[str]]]:
    """Map each sheet and row to its embedded images, stored once per image hash."""
    result: dict[str, dict[int, list[str]]] = {}
    with zipfile.ZipFile(long_path(path)) as archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            return result
        sheets = drawing_targets(archive, "xl/workbook.xml", "worksheet")
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).iter(f"{SPREADSHEET_NS}sheet"):
            target = sheets.get(sheet.get(f"{RELATION_NS}id"))
            if not target:
                continue
            rows: dict[int, list[str]] = {}
            for drawing in drawing_targets(archive, target, "/drawing").values():
                if drawing not in names:
                    continue
                media = drawing_targets(archive, drawing, "/image")
                root = ET.fromstring(archive.read(drawing))
                for tag in ("twoCellAnchor", "oneCellAnchor"):
                    for anchor in root.findall(f"{DRAWING_NS}{tag}"):
                        row = anchor.find(f"{DRAWING_NS}from/{DRAWING_NS}row")
                        blip = next(anchor.iter(f"{BLIP_NS}blip"), None)
                        if row is None or blip is None or row.text is None:
                            continue
                        member = media.get(blip.get(f"{RELATION_NS}embed", ""))
                        if not member or member not in names:
                            continue
                        suffix = Path(member).suffix.casefold()
                        if suffix not in IMAGE_TYPES or archive.getinfo(member).file_size > max_bytes:
                            continue
                        data = archive.read(member)
                        name = f"img-{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
                        if not (assets / name).exists():
                            (assets / name).write_bytes(data)
                        rows.setdefault(int(row.text) + 1, []).append(name)
            if rows:
                result[sheet.get("name")] = rows
    return result


def legacy_images(path: Path, assets: Path, max_bytes: int, converted: Path, timeout: int, key: str) -> dict[str, dict[int, list[str]]]:
    # The same converted copy the text step uses, so each old workbook is converted once.
    return workbook_images(convert_office(path, "xlsx", converted, key, timeout), assets, max_bytes)


def collect_images(
    jobs: list[tuple[str, Path]],
    build: Path,
    max_bytes: int,
    convert_legacy: bool,
    workers: int,
    timeout: int,
    hashes: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, dict[int, list[str]]]], dict[str, str]]:
    hashes = hashes or {}
    assets = build / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    work = build / "image-work"
    work.mkdir(parents=True, exist_ok=True)
    failures: dict[str, str] = {}
    # Converting old workbooks takes minutes, and a rerun must stay cheap.
    cache_path = build / "image-cache.json"
    cache: dict[str, Any] = load_json(cache_path) if cache_path.is_file() else {}

    def one(job: tuple[str, Path]) -> tuple[str, dict[str, dict[int, list[str]]]]:
        item_id, path = job
        converted = build / "converted" / f"{hashes.get(item_id, '')[:16]}.xlsx"
        if path.suffix.casefold() != ".xlsx" and converted.is_file():
            path = converted  # The text step already converted this old workbook.
        if item_id in cache:
            return item_id, {s: {int(r): v for r, v in rows.items()} for s, rows in cache[item_id].items()}
        try:
            if path.suffix.casefold() == ".xlsx":
                return item_id, workbook_images(path, assets, max_bytes)
            if convert_legacy:
                return item_id, legacy_images(path, assets, max_bytes, build / "converted", timeout,
                                              hashes.get(item_id, "")[:16] or short_hash(str(path), 16))
            failures[item_id] = "legacy_not_converted"
        except Exception as exc:  # A broken workbook loses its pictures, never the run.
            failures[item_id] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return item_id, {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(pool.map(one, jobs))
    shutil.rmtree(work, ignore_errors=True)
    write_json(cache_path, {i: maps for i, maps in results.items() if i not in failures})
    return results, failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route package items and extract source-located blocks.")
    parser.add_argument("--package", action="append", required=True, help="Preparation package; repeat to merge later packages")
    parser.add_argument("--text-dir", action="append", required=True, help="Extracted text units; repeat")
    parser.add_argument("--extract-status", action="append", default=[], help="extract-status.json; repeat")
    parser.add_argument("--ocr-text-dir", help="OCR units; defaults to 04_知识库构建/mineru-units when present")
    parser.add_argument("--out", required=True, help="Run directory outside the package and the vault")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--images", choices=["none", "xlsx", "all"], default="xlsx")
    parser.add_argument("--max-image-mb", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per legacy conversion")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    build.mkdir(parents=True, exist_ok=True)
    config = load_config(Path(args.config) if args.config else None)
    if args.config and Path(args.config).resolve() != (build / "kb-config.json").resolve():
        shutil.copy2(args.config, build / "kb-config.json")  # Saved into the vault at publish.
    assets, triage, package_of, point_files = [], {}, {}, []
    for raw in args.package:
        package = Path(raw).resolve()
        if not (package / MANIFEST_NAME).is_file():
            raise SystemExit(f"不是初筛包，缺少 {MANIFEST_NAME}：{package}。请先用 prepare-seo-knowledge-materials 筛选。")
        for asset in load_json(package / MANIFEST_NAME)["assets"]:
            if asset["item_id"] in package_of:
                continue  # A later package cannot replace an item already decided.
            assets.append(asset)
            package_of[asset["item_id"]] = package
        triage.update({item["item_id"]: item for item in load_json(package / TRIAGE_NAME)["items"]})
        point_files.append(package / POINTS_NAME)
    status = {}
    for raw in args.extract_status:
        status.update(load_json(Path(raw))["items"])
    text_dirs = [Path(raw).resolve() for raw in args.text_dir]
    ocr_dir = Path(args.ocr_text_dir).resolve() if args.ocr_text_dir else build / "mineru-units"
    if ocr_dir.is_dir():
        text_dirs.append(ocr_dir)

    routes = {
        asset["item_id"]: {**route_asset(asset, config["client_answers"]), "asset": asset}
        for asset in assets
    }
    for item_id, route in routes.items():
        # A picture someone (MinerU or the model) has read enters with what was read in it.
        if route["reason"] == "image_needs_review" and any(u["text"].strip() for u in load_units([ocr_dir], item_id)):
            route.update(route="kb", reason="image_read", tier="T2")
    reviewed_columns = build / "cell-review" / "sheet-columns.json"
    column_overrides = {**config.get("sheet_columns", {}), **(load_json(reviewed_columns) if reviewed_columns.is_file() else {})}

    jobs = []
    if args.images != "none":
        for item_id, route in routes.items():
            path = route["asset"].get("path")
            extension = extension_of(route["asset"].get("display_name") or "")
            wanted = {".xlsx"} if args.images == "xlsx" else {".xlsx", ".xls"}
            if route["route"] == "kb" and path and extension in wanted and extension in SPREADSHEET:
                jobs.append((item_id, package_of[item_id] / path))
    image_maps, image_failures = collect_images(
        jobs, build, int(args.max_image_mb * 1024 * 1024), args.images == "all", args.workers, args.timeout,
        {asset["item_id"]: asset["source_hash"] for asset in assets},
    )

    docs, blocks, terms, sheet_reports = [], [], [], {}
    for item_id, route in routes.items():
        asset = route["asset"]
        if route["route"] != "kb":
            continue
        tier = route["tier"]
        extension = extension_of(asset.get("display_name") or "")
        doc_id = f"DOC-{asset['source_hash'][:10]}"
        units = load_units(text_dirs, item_id)
        title, folder = package_title(asset, tier, config.get("doc_titles", {}))
        doc = {
            "doc_id": doc_id,
            "item_id": item_id,
            "title": title,
            "folder": folder,
            "original_name": asset.get("display_name"),
            "source_label": f"初筛包 {asset.get('decision')} 类（{RESOURCE_LABELS.get(asset.get('resource_type'), '未归类')}）",
            "file_name": safe_name(title) + ".md",
            "tier": tier,
            "module": MODULES[module_for(asset, tier)],
            "resource_type": asset.get("resource_type"),
            "decision": asset.get("decision"),
            "source_sha256": asset["source_hash"],
            "package_path": asset.get("path"),
            "package_name": package_of[item_id].name,
            "origin": "package",
        }
        item_blocks: list[dict[str, Any]] = []
        if asset.get("decision") == "B":
            rules, unreadable = parse_actions(triage.get(item_id, {}).get("actions", []))
            stats: dict[str, Any] = {"sheets_without_header": [], "unknown_headers": [], "rows_kept": 0, "templates": {}}
            if unreadable:
                # A deletion nobody can execute must not be skipped silently.
                route.update(route="hold", reason="unreadable_removal_location")
                doc = None
            else:
                for position, unit in enumerate(units, start=1):
                    match = SHEET_UNIT.match(unit["locator"])
                    if not match:
                        continue
                    if match.group("state") != "visible":
                        stats.setdefault("hidden_sheets", []).append(match.group("name"))
                        continue
                    item_blocks.extend(
                        sheet_blocks(
                            doc_id, item_id, match.group("name"), f"表 {position}", unit["text"], rules,
                            config["header_map"], image_maps.get(item_id, {}).get(match.group("name"), {}),
                            stats, terms, column_overrides,
                        )
                    )
                if stats.get("quotation_sheets") and item_id not in config.get("doc_titles", {}):
                    # A quote sent to one customer, filed apart from the product lists.
                    doc.update(kind="quotation", title=f"报价单-{asset['source_hash'][:8]}", folder="产品表/报价单",
                               file_name=f"报价单-{asset['source_hash'][:8]}.md")
            sheet_reports[item_id] = stats
        else:
            item_blocks = document_blocks(doc_id, item_id, units)
            if reference_draft(units, config.get("doc_roles", {}).get(item_id)):
                # Someone's compilation from other sources, not the company's own material.
                doc["role"] = "reference"
                for block in item_blocks:
                    block["reference"] = True
            locations = list(asset.get("pending_units") or [])
            if asset.get("pending_client_review") and config["client_answers"].get(item_id) != "keep":
                held, unmatched = hold_pending_units(
                    item_blocks, locations, config.get("client_answers_units", {}).get(item_id)
                )
                route["client_held_blocks"] = held
                if unmatched:
                    for block in item_blocks:
                        block.setdefault("client_hold", "整份文件（待确认位置没找到：" + "；".join(unmatched) + "）")
        if doc and route.get("pending_all"):
            for block in item_blocks:
                block["client_hold"] = "整份文件"
        if doc:
            doc["units"] = len(units)
            doc["pages"] = status.get(item_id, {}).get("pages")
        pages = status.get(item_id, {}).get("ocr_pages") or []
        if doc and not item_blocks:
            route.update(route="hold", reason="no_extractable_content")
            doc = None
        if doc:
            route["ocr_pages"] = pages
            docs.append(doc)
            for block in item_blocks:
                block["tier"] = tier
            blocks.extend(item_blocks)

    website = build / "website"
    site_pages = load_json(website / "pages.json") if (website / "pages.json").is_file() else []
    first_segments = Counter(segments(p["url"])[0] for p in site_pages if len(segments(p["url"])) >= 2)
    seen_paths: set[str] = set()
    for page in site_pages:
        parts = segments(page["url"])
        same = SITE_COPY.sub("", "/".join(parts)).casefold()
        if (len(parts) == 1 and first_segments[parts[0]] >= 2) or same in seen_paths:
            # A category listing repeats its products; a saved copy repeats its original.
            reason = "website_listing" if len(parts) == 1 and first_segments[parts[0]] >= 2 else "website_duplicate"
            routes[page["page_id"]] = {"route": "skip", "reason": reason,
                                       "asset": {"display_name": page["url"], "decision": "web", "resource_type": "website",
                                                 "source_hash": page["html_sha256"]}}
            continue
        seen_paths.add(same)
        # Rules can improve after a crawl, so the page is judged again here without refetching.
        page["module"], page["kind"] = web_module(page["url"]), web_kind(page["url"], page.get("characters", 0))
        if page["kind"] == "noise":
            routes[page["page_id"]] = {"route": "skip", "reason": "website_noise",
                                       "asset": {"display_name": page["url"], "decision": "web", "resource_type": "website",
                                                 "source_hash": page["html_sha256"]}}
            continue
        units = [u for u in load_units([website / "units"], page["page_id"]) if not heading_only(u["text"])]
        routes[page["page_id"]] = {"route": "kb" if page["kind"] in {"page", "case"} else "hold",
                                   "reason": {"page": "website", "case": "website_case_page"}.get(page["kind"], "website_needs_browser"),
                                   "tier": "T1", "asset": {"display_name": page["url"], "decision": "web",
                                                           "resource_type": "website", "source_hash": page["html_sha256"]}}
        answer = config["client_answers"].get(page["page_id"])
        if answer == "drop":
            routes[page["page_id"]].update(route="skip", reason="client_said_no")
        if page["kind"] not in {"page", "case"} or not units or answer == "drop":
            continue
        doc_id = f"WEB-{page['page_id'][4:14]}"
        name = strip_site_name(page["title"], config["own_terms"])
        if len(parts) >= 2:
            category = config.get("categories", {}).get(parts[0]) or parts[0].replace("-", " ").title()
            title, folder = f"{category}-{name}", f"官网页面/{safe_name(category)}"
        else:
            title, folder = f"官网-{name}", "官网页面/公司与服务"
        docs.append({"doc_id": doc_id, "item_id": page["page_id"], "title": title, "folder": folder,
                     "original_name": page["title"], "source_label": "客户官网",
                     "file_name": safe_name(title) + ".md", "tier": "T1", "module": page["module"],
                     "resource_type": "website", "decision": "web", "source_sha256": page["html_sha256"],
                     "package_path": page["url"], "origin": "website", "fetched_at": page["fetched_at"]})
        page_blocks = document_blocks(doc_id, page["page_id"], units)
        for block in page_blocks:
            block["tier"] = "T1"
            if page["kind"] == "case" and answer != "keep":
                block["client_hold"] = "官网客户案例页"
        blocks.extend(page_blocks)

    points = []
    for point in (record for path in point_files for record in load_jsonl(path)):
        route = routes.get(point.get("source_id"))
        if not route or route["route"] != "points" or not point.get("usable_as_fact"):
            continue
        points.append(point)
    if points:
        doc_id = "DOC-POINTS"
        docs.append(
            {
                "doc_id": doc_id,
                "item_id": "knowledge-points",
                "title": "业务要点-邮件摘录",
                "folder": "业务要点",
                "original_name": "初筛知识点（knowledge-points.jsonl）",
                "source_label": "初筛包 C 类知识点（初筛时写的摘要）",
                "file_name": "业务要点-邮件摘录.md",
                "tier": "T4",
                "module": MODULES["solution"],
                "resource_type": "business_scenario",
                "decision": "C",
                "source_sha256": "",
                "package_path": POINTS_NAME,
                "origin": "points",
            }
        )
        for point in points:
            blocks.append(
                {
                    "block_id": short_hash(f"point|{point['knowledge_id']}"),
                    "doc_id": doc_id,
                    "kind": "point",
                    "group": "要点",
                    "locator": f"{point['source_id']} / {point.get('locator', '')}",
                    "text": point["knowledge_point"],
                    "scope": point.get("scope", "single_communication"),
                    "origin_item": point["source_id"],
                }
            )

    write_jsonl(build / "docs.jsonl", docs)
    write_json(build / "run-info.json", {"packages": [str(Path(p).resolve()) for p in args.package]})
    write_jsonl(build / "blocks-raw.jsonl", blocks)
    write_jsonl(build / "removed-terms.jsonl", terms)
    write_json(
        build / "routes.json",
        {
            item_id: {
                "display_name": route["asset"].get("display_name"),
                "decision": route["asset"].get("decision"),
                "resource_type": route["asset"].get("resource_type"),
                "source_hash": route["asset"].get("source_hash", ""),
                "route": route["route"],
                "reason": route["reason"],
                "tier": route.get("tier"),
                "ocr_pages": route.get("ocr_pages", []),
                "client_held_blocks": route.get("client_held_blocks", 0),
                "pending_units": route["asset"].get("pending_units", []),
            }
            for item_id, route in routes.items()
        },
    )
    templates = {}
    for item_id, report in sheet_reports.items():
        for signature, template in report.pop("templates", {}).items():
            entry = templates.setdefault(signature, {**template, "files": []})
            entry["files"].append(item_id)
    write_json(build / "sheet-templates.json", templates)
    write_json(build / "sheet-report.json", {"sheets": sheet_reports, "image_failures": image_failures})
    summary: dict[str, int] = {}
    for route in routes.values():
        summary[f"{route['route']}:{route['reason']}"] = summary.get(f"{route['route']}:{route['reason']}", 0) + 1
    print(json.dumps({"documents": len(docs), "blocks": len(blocks), "routes": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
