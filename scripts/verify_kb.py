#!/usr/bin/env python3
"""Acceptance check, written apart from the pipeline so it can disagree with it.

Rereads the extracted source text and the finished files. A run counts only when
this reports "verified": true.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from source_fidelity import reviewed_units
from split_reviews import split_errors
from sheet_layout import excel_date

from guard_blocks import EMAIL, FURNITURE, HARD_PATTERNS, LABELLED_PHONE, MOBILE_CN, ROW_PATTERNS, compile_terms
from kb_common import (
    unapproved_d,
    BUILD_DIRECTORY,
    CLEAN_DIRECTORY,
    is_platform_page_unit,
    load_config,
    load_json,
    load_jsonl,
    load_units,
    normalize_space,
    parse_frontmatter,
    preferred_page_units,
    reducto_figure_units,
    run_lock,
    suppress_drawing_dimensions,
    sha256_file,
    unit_dirs,
    write_json,
    write_text,
)


UNREAD_LIMIT = 0.3
REQUIRED_FIELDS = ("doc_id", "tier", "visibility", "module", "source_item_id")
# Words every clean page may use: labels the pipeline itself writes.
BOILERPLATE = re.compile(r"^(?:>|\s*$|---|[a-z_]+: )")


# Hex image names can look like phone numbers.
IMAGE_LINK = re.compile(r"!\[\[img-[^\]]+\]\]|`?img-[0-9a-f]+\.[a-z]+`?")


SHEET_LOCATOR = re.compile(r"^sheet .+ \(([^)]+)\)$")
SHEET_CELL = re.compile(r"(?:^|;\s*)[A-Z]{1,3}\d+=")
TABLE_RULE = re.compile(r"^:?-{3,}:?$")


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|") if not TABLE_RULE.match(cell.strip())]


def squeeze(text: str) -> str:
    # A protected dimension asterisk is identical source data after Markdown renders.
    return re.sub(r"\s+", "", text.replace(" ", "").replace(r"\*", "*"))


def ocr_format_key(text: str) -> str:
    """Ignore OCR list markup only, never substantive words or style-code hyphens."""
    text = unicodedata.normalize("NFKC", text)
    return squeeze(re.sub(r"(?:^|\s)(?:\\?[-*•·])\s+", " ", text)).casefold()


def ocr_segments(text: str) -> list[str]:
    """Split an OCR line at visual bullet markers before source comparison.

    MinerU sometimes joins several bullet lines around a page heading.  The
    words are still present in the OCR unit, but the joined line is not a
    contiguous substring of that unit.  Comparing the visual segments keeps
    the fidelity check strict while allowing this harmless line-wrap change.
    """
    text = re.sub(r"(?<!^)(?=[©◎☐⚠️])", "\n", text)
    text = re.sub(r"(?<!^)(?=\\-\s*)", "\n", text)
    return [part.strip() for part in text.splitlines() if part.strip()]


def scan_text(
    text: str, terms: re.Pattern[str] | None, own_domains: set[str], own_contacts: list[str], keep_texts: set[str],
    product_rows: bool = True,
) -> list[str]:
    """product_rows: table lines are product-table rows, held to the stricter row rules. A table
    inside a brochure or a Word file is ordinary text laid out in cells."""
    hits = []
    for paragraph in text.split("\n"):
        if squeeze(paragraph) in keep_texts:
            continue
        for name, pattern in HARD_PATTERNS.items():
            if pattern.search(paragraph):
                hits.append(name)
        if paragraph.startswith("|") and product_rows:
            hits.extend(name for name, pattern in ROW_PATTERNS.items() if pattern.search(paragraph))
        for match in EMAIL.finditer(paragraph):
            if match.group(1).casefold() not in own_domains:
                hits.append("email")
        remainder = paragraph
        for contact in own_contacts:
            remainder = remainder.replace(contact, " ")
        if MOBILE_CN.search(remainder) or LABELLED_PHONE.search(remainder):
            hits.append("phone")
        if terms and terms.search(paragraph):
            hits.append("denied_term")
    return hits


PAGE_PICTURE = re.compile(r"!\[\[([^|\]]+)\|360\]\]")
MAX_PAGE_PICTURES = 80


def original_page_count(path: Path) -> int | None:
    """Pages of a PDF or slides of a PPTX, counted from the original itself; None when unknown."""
    suffix = path.suffix.casefold()
    try:
        if suffix == ".pdf":
            import pymupdf  # type: ignore[import-not-found]
            with pymupdf.open(str(path)) as document:
                return document.page_count
        if suffix == ".pptx":
            import zipfile
            with zipfile.ZipFile(path) as archive:
                return sum(1 for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
    except Exception:  # noqa: BLE001 - an unreadable original is reported by the extraction checks.
        return None
    return None


def page_picture_errors(build: Path, clean: Path, docs: dict[str, dict[str, Any]], published: dict[str, str],
                        blocks: list[dict[str, Any]], routes: dict[str, Any],
                        warnings: list[str] | None = None) -> list[str]:
    """Page pictures, counted on the published pages and checked against the originals.

    Built independently of the render: the render and an earlier version of this check read the
    same extraction record, so a build that never got its pictures (text reused with --text-dir)
    passed both. Here the page count comes from the original file and the pictures from the page."""
    try:
        import pymupdf  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return []
    status_path = build / "extract-status.json"
    status = load_json(status_path).get("items", {}) if status_path.is_file() else {}
    named = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
    # Pages link a picture by its path in the vault (03_正式知识/90_素材/<file>), older ones by name alone.
    files = {}
    for picture_id, entry in named.items():
        files[f"03_正式知识/90_素材/{entry['file']}"] = picture_id
        files.setdefault(Path(entry["file"]).name, picture_id)
    held_pages: dict[str, set[str]] = {}
    text_pages: dict[str, set[str]] = {}
    for block in blocks:
        page = re.match(r"^((?:page|slide) \d+)(?: |$)", block.get("locator", ""))
        if not page:
            continue
        number = page.group(1).split(" ")[1]
        if block["status"] in {"removed", "pending_client"}:
            held_pages.setdefault(block["doc_id"], set()).add(number)
        elif block["status"] in {"kept", "flagged"}:
            text_pages.setdefault(block["doc_id"], set()).add(number)
    errors, unrecorded = [], []
    for doc_id, relative in published.items():
        doc, route = docs[doc_id], routes.get(docs[doc_id]["item_id"], {})
        if not route.get("package") or not route.get("path"):
            continue
        original = Path(route["package"]) / route["path"]
        pages = original_page_count(original) if original.suffix.casefold() in {".pdf", ".pptx"} else None
        if not pages or pages > MAX_PAGE_PICTURES or status.get(doc["item_id"], {}).get("page_images_skipped"):
            continue
        text = (clean / relative).read_text(encoding="utf-8")
        shown = PAGE_PICTURE.findall(text)
        for name in shown:
            if name not in files or not (build / "assets" / files[name]).is_file():
                errors.append(f"原页图文件不见了：{doc['title']} {name}")
        # A page with no text has no section to hang a picture on, and a page something was removed
        # from or held on deliberately shows none; any other page with text should have one.
        if not shown and text_pages.get(doc_id, set()) - held_pages.get(doc_id, set()):
            if doc["item_id"] in status:
                errors.append(f"来源页一张原页图都没有（原件 {pages} 页）：{doc['title']}")
            else:
                # No extraction record at all: whether pictures were made cannot be told.
                unrecorded.append(doc["title"])
    if unrecorded and warnings is not None:
        warnings.append(f"{len(unrecorded)} 份 PDF/PPT 没有提取记录，确认不了该不该有原页图（比如 {unrecorded[0]}）："
                        "用 --text-dir 时一并用 --extract-status 传入那个文本目录的 extract-status.json")
    return errors


def drawing_page_errors(build: Path, docs: dict[str, dict[str, Any]], blocks: list[dict[str, Any]],
                        published: dict[str, str], clean: Path | None = None) -> list[str]:
    """Pages confirmed as dimension drawings, checked from the page-check results themselves, not
    through the merge that builds the text: a check that reused it would share its blind spots.

    - The drawing label is still on the page (a restored old transcript must not replace it), and sits
      under its own page/slide/image group (not a neighbouring Word paragraph heading).
    - Where the source page is rendered and nothing on that page/image is held or removed, the original
      drawing image is embedded under that heading.
    - No old full-page transcript sits in the text of a drawing page, unless it was re-reviewed
      as the page's non-drawing part only."""
    from pictures import drawing_entries
    results = drawing_entries(build).values()
    reviewed = load_json(build / "reviewed-pages.json") if (build / "reviewed-pages.json").is_file() else {}
    by_item = {doc["item_id"]: doc for doc in docs.values() if doc["doc_id"] in published}
    by_locator = {(b["doc_id"], b["locator"]): b for b in blocks if b["status"] in {"kept", "flagged", "pending_client"}}
    unsafe = {(b["doc_id"], b["group"]) for b in blocks if b["status"] in {"removed", "pending_client"}}
    errors = []
    for result in results:
        doc = by_item.get(result.get("item_id"))
        if not result.get("dimension_drawing") or not doc:
            continue
        unit, page = result["unit"], result["page"]
        locator = f"{unit} {page}"
        expected_group = {"slide": f"第 {page} 张幻灯片", "image": f"图片 {page}"}.get(unit, f"第 {page} 页")
        label_block = by_locator.get((doc["doc_id"], f"{locator} drawing"))
        if not label_block:
            errors.append(f"尺寸图标签不见了（可能被恢复的旧人工稿覆盖）：{doc['title']} {locator}")
        elif label_block.get("group") and label_block["group"] != expected_group:
            errors.append(f"尺寸图文字块分组错位（在“{label_block['group']}”，应在“{expected_group}”）：{doc['title']} {locator}")
        if clean is not None and (doc["doc_id"], expected_group) not in unsafe:
            md_path = clean / published[doc["doc_id"]]
            if md_path.is_file():
                text = md_path.read_text(encoding="utf-8")
                section = re.search(rf"(?ms)^## {re.escape(expected_group)}\n(.*?)(?=^## |\Z)", text)
                if not section or not PAGE_PICTURE.search(section.group(1)):
                    errors.append(f"尺寸图来源页没有附原图：{doc['title']} {expected_group}")
        split = {r["locator"] for r in reviewed.get(doc["item_id"], []) if r.get("part") == "non_drawing"}
        for block in blocks:
            if (block["doc_id"] == doc["doc_id"] and block["status"] in {"kept", "flagged", "pending_client"}
                    and block.get("reader") == "reviewed" and locator not in split
                    and re.match(rf"^{re.escape(locator)}(?: |$)", block["locator"])):
                errors.append(f"尺寸图页的旧人工整页稿进了正文：{doc['title']} {block['locator']}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a built knowledge base.")
    parser.add_argument("--package", action="append", required=True, help="Preparation package; repeat")
    parser.add_argument("--text-dir", action="append", required=True, help="Extracted text units; repeat")
    parser.add_argument("--ocr-text-dir", help="OCR units; defaults to 04_知识库构建/mineru-units when present")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--sample", type=int, default=30, help="Blocks to list for a human or agent spot check")
    parser.add_argument("--seed", type=int, default=20261102)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not run_lock(Path(args.out).resolve(), "verify_kb.py"):
        return 3
    run = Path(args.out).resolve()
    build = run / BUILD_DIRECTORY
    clean = run / CLEAN_DIRECTORY
    config = load_config(Path(args.config) if args.config else None)
    packages = [Path(raw).resolve() for raw in args.package]
    assets = [asset for package in packages for asset in load_json(package / "审计记录/package-manifest.json")["assets"]]
    routes = load_json(build / "routes.json")
    docs = {doc["doc_id"]: doc for doc in load_jsonl(build / "docs.jsonl")}
    blocks = load_jsonl(build / "blocks.jsonl")
    published: dict[str, str] = load_json(build / "published.json")
    terms = load_json(build / "guard-terms.json")
    ocr_dir = Path(args.ocr_text_dir).resolve() if args.ocr_text_dir else build / "mineru-units"
    text_dirs = [d for d in unit_dirs(build, [Path(raw).resolve() for raw in args.text_dir], ocr_dir)
                 if d.is_dir() or d in [Path(raw).resolve() for raw in args.text_dir]]
    website = build / "website"
    pages = {page["page_id"]: page for page in load_json(website / "pages.json")} if (website / "pages.json").is_file() else {}
    own_domains = {domain.casefold() for domain in config["own_domains"]}
    errors: list[str] = []

    # Every item in the package has a recorded fate.
    asset_ids = {asset["item_id"] for asset in assets}
    status_path = build / "extract-status.json"
    extracted = load_json(status_path).get("items", {}) if status_path.is_file() else {}
    for item_id in sorted(asset_ids):
        status = extracted.get(item_id, {}).get("status")
        if status in {"hash_mismatch", "missing_file"}:
            errors.append(f"package input {status}: {item_id}")
    for item_id in asset_ids - set(routes):
        errors.append(f"no route for {item_id}")
    hashes = {asset["item_id"]: asset["source_hash"] for asset in assets}
    hashes.update({page_id: page["html_sha256"] for page_id, page in pages.items()})

    # Nothing outside the allow path was published.
    for doc_id, relative in published.items():
        doc = docs[doc_id]
        if doc["item_id"] != "knowledge-points" and routes.get(doc["item_id"], {}).get("route") != "kb":
            errors.append(f"published without a kb route: {relative}")
        path = clean / relative
        if not path.is_file():
            errors.append(f"missing file: {relative}")
            continue
        fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        for field in REQUIRED_FIELDS:
            if not fields.get(field):
                errors.append(f"{relative}: missing {field}")
        if doc["source_sha256"] and fields.get("source_sha256") != hashes.get(doc["item_id"]):
            errors.append(f"{relative}: source hash differs from the package")

    # A run that read almost nothing must not look successful.
    unread = [r for r in routes.values() if r.get("route") == "hold" and r.get("reason") == "no_extractable_content"]
    readable = [r for r in routes.values() if r.get("route") == "kb"] + unread
    if readable and len(unread) / len(readable) > UNREAD_LIMIT and (len(unread) >= 3 or len(unread) * 2 > len(readable)):
        errors.append(f"{len(unread)} / {len(readable)} 份该入库的资料读不到文字：检查 PDF 读取依赖（pypdf）、LibreOffice，或提取目录是否给错")

    # Every kept block is found word for word in its source.
    units_by_item: dict[str, str] = {}
    points = {
        record["knowledge_point"]
        for package in packages
        for record in load_jsonl(package / "C-知识点/knowledge-points.jsonl")
    }
    kept = [block for block in blocks if block["status"] in {"kept", "flagged", "pending_client"} and block["doc_id"] in published]
    for block in kept:
        doc = docs[block["doc_id"]]
        if block["kind"] == "point":
            if block["text"] not in points:
                errors.append(f"point not in package: {block['block_id']}")
            continue
        item_id = doc["item_id"]
        if item_id not in units_by_item:
            dirs = [website / "units"] if doc.get("origin") == "website" else text_dirs
            raw_units = load_units(dirs, item_id)
            if doc.get("origin") != "website":
                raw_units.extend(reducto_figure_units(build, item_id, doc.get("original_name") or ""))
            units = reviewed_units(build, item_id, doc['source_sha256'], raw_units)
            units_by_item[item_id] = squeeze(" ".join(u["text"] for u in units))
        source = units_by_item[item_id]
        # Non-product workbook sheets are rendered as Markdown tables.  The pipes,
        # separator row, and <br> tags are layout introduced by the builder; compare
        # each cell's text to the source export instead of treating the whole row as
        # a literal source line.
        if block["kind"] == "text" and block["locator"].startswith("sheet ") and block["text"].lstrip().startswith("|"):
            for line in block["text"].splitlines():
                if not line.strip() or TABLE_RULE.fullmatch(line.strip().strip("| ")):
                    continue
                for piece in table_cells(line):
                    piece = re.sub(r"<br\s*/?>", "", piece)
                    if squeeze(piece) and squeeze(piece) not in source:
                        errors.append(f"text not in source: {doc['title']} {block['locator']}")
                        break
            continue
        # ``above``/``below`` are layout parts generated from spreadsheet cell
        # exports.  Their text is rearranged (and the empty-column note is
        # builder metadata), so it cannot be matched as one literal source
        # line.  Cell-level preservation is checked in the source-line pass.
        if block["kind"] == "text" and block["locator"].startswith("sheet ") and \
                (block["locator"].endswith(" above") or block["locator"].endswith(" below")):
            continue
        values = block["cells"].values() if block["kind"] == "row" else block["text"].splitlines()
        for value in values:
            # A Markdown table is checked cell by cell; its bars and rules are layout, not text.
            pieces = table_cells(value) if str(value).lstrip().startswith("|") else str(value).split(" / ")
            if block["locator"].endswith(" ocr") and not str(value).lstrip().startswith("|"):
                pieces = ocr_segments(str(value))
            for piece in pieces:
                if block["locator"].endswith(" ocr"):
                    piece = re.sub(r"^[©◎☐⚠️]\s*", "", piece)
                if squeeze(piece) and squeeze(piece) not in source:
                    errors.append(f"text not in source: {doc['title']} {block['locator']}")
                    break

    # Each selected source line is kept or held. Only native-text footers may be omitted.
    for doc in docs.values():
        if doc["tier"] not in ("T1", "T2"):
            continue
        # A line break kept inside a table cell is written <br>; it is layout, not text.
        accounted = squeeze("".join(b["text"] for b in blocks if b["doc_id"] == doc["doc_id"]).replace("<br>", ""))
        accounted_ocr = ocr_format_key("\n".join(b["text"] for b in blocks if b["doc_id"] == doc["doc_id"]))
        dirs = [website / "units"] if doc.get("origin") == "website" else text_dirs
        raw_units = load_units(dirs, doc['item_id'])
        if doc.get("origin") != "website":
            raw_units.extend(reducto_figure_units(build, doc['item_id'], doc.get("original_name") or ""))
        units = reviewed_units(build, doc['item_id'], doc['source_sha256'], raw_units)
        units = preferred_page_units(units)
        units = suppress_drawing_dimensions(units)
        for unit in units:
            platform_page = is_platform_page_unit(unit)
            sheet = SHEET_LOCATOR.match(unit["locator"])
            if sheet and sheet.group(1) != "visible":
                continue  # Hidden sheets never enter.
            for line in unit["text"].splitlines():
                if sheet:
                    # A sheet row "A7=x; B7=y" is published as table cells: each value must be there.
                    # Phone/contact rows are deliberately removed by guard_blocks.py.
                    # They therefore cannot be expected to appear in the cleaned source.
                    if LABELLED_PHONE.search(line):
                        continue
                    # An Excel date number (46148) is published as the date it stands for (2026-05-05).
                    missing = [v for v in SHEET_CELL.split(line) if v.strip() and squeeze(v) not in accounted
                               and not (excel_date(v) and squeeze(excel_date(v)) in accounted)]
                    if missing:
                        errors.append(f"source line lost: {doc['title']} {unit['locator']}")
                        break
                    continue
                text = normalize_space(re.sub(r"<br\s*/?>", "", line, flags=re.IGNORECASE))
                if unit["locator"].endswith(" ocr") and ocr_format_key(text) in accounted_ocr:
                    continue  # The same words are present; only OCR list formatting differs.
                if text and squeeze(text) not in accounted and (platform_page or not FURNITURE.search(text)) and doc.get("origin") != "website":
                    errors.append(f"source line lost: {doc['title']} {unit['locator']}")
                    break

    # The finished files carry nothing the guard forbids, in any published page.
    decisions_path = build / "review-decisions.json"
    decisions = load_json(decisions_path) if decisions_path.is_file() else {}
    keep_ids = set(decisions.get("keep_blocks", []))
    keep_texts = {squeeze(line) for b in blocks if b["block_id"] in keep_ids for line in b["text"].split("\n")}
    pattern = compile_terms(terms["hard"] + terms["soft"])
    knowledge = build / "vault" / "03_正式知识"
    # Every page goes through the check, the 建站资料 view too: it lists file and folder names,
    # and site_view.py hides the ones that carry a customer, a price or a quote term.
    pages = list(clean.rglob("*.md")) + list(knowledge.rglob("*.md"))
    for path in pages:
        raw = path.read_text(encoding="utf-8")
        product_rows = parse_frontmatter(raw).get("tier") == "T3"
        body = raw.split("\n---\n", 1)[-1]
        body = "\n".join(line for line in body.splitlines() if not BOILERPLATE.match(line))
        body = IMAGE_LINK.sub(" ", body)
        for hit in sorted(set(scan_text(body, pattern, own_domains, config["own_contacts"], keep_texts, product_rows))):
            errors.append(f"{path.name}: forbidden content ({hit})")

    # File names travel with the content, so they are checked too.
    if pattern:
        for path in pages:
            if pattern.search(path.stem):
                errors.append(f"forbidden term in file name: {path.name}")
        # Picture names are made from the words beside them, so they pass the same test.
        named = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
        for entry in named.values():
            if pattern.search(entry["file"]) or pattern.search(entry.get("alt", "")):
                errors.append(f"forbidden term in picture name: {entry['file']}")

    # Risky passages are published on purpose, so each one must carry its mark.
    for block in kept:
        if block.get("flags"):
            page = clean / published[block["doc_id"]]
            if "⚠" not in page.read_text(encoding="utf-8"):
                errors.append(f"标了风险的内容没有 ⚠：{docs[block['doc_id']]['title']} {block['locator']}")
    errors.extend(drawing_page_errors(build, docs, blocks, published, clean))
    if (build / "reviewed-pages.json").is_file():
        errors.extend(split_errors(load_json(build / "reviewed-pages.json")))
    picture_warnings: list[str] = []
    errors.extend(page_picture_errors(build, clean, docs, published, blocks, routes, picture_warnings))
    curation = build / "curation-report.json"
    for slug, result in (load_json(curation).items() if curation.is_file() else []):
        if not result["ok"]:
            errors.append(f"整理页没通过核对，未发布：{result['title']}（{'；'.join(result['errors'][:2])}）")

    # A knowledge layer left half written must not look finished.
    warnings: list[str] = list(picture_warnings)
    d_path = build / "review-decisions.json"
    # Report the status actually rendered: reference drafts are not pending originals.
    statuses = load_json(build / "statuses.json")
    unapproved_hashes = {routes[i].get("source_hash") or i
                         for i in unapproved_d(routes, load_json(d_path) if d_path.is_file() else {})}
    waiting_d = {docs[d].get("source_sha256") or docs[d]["item_id"] for d in published
                 if statuses.get(d) == "待审"
                 and (docs[d].get("source_sha256") or docs[d]["item_id"]) in unapproved_hashes}
    if waiting_d:
        warnings.append(f"{len(waiting_d)} 份保留的 D 类还没批准，现在是“待审”：看过原文没问题，把资料编号写进 review-decisions.json 的 approve_docs")
    undecided_d = {r.get("source_hash") or i for i, r in routes.items() if r.get("decision") == "D" and r.get("reason") == "unresolved_item"}
    if undecided_d:
        warnings.append(f"{len(undecided_d)} 份 D 类还没决定要不要用：看过后写进 kb-config.json 的 d_keep / d_drop，"
                        "要问客户的写进 ask_client")
    topics_path = build / "curation" / "topics.json"
    topics = load_json(topics_path) if topics_path.is_file() else []
    topics = [t for t in topics if t.get("blocks")]  # A topic with no source text has nothing to write.
    unwritten = [t for t in topics if not (build / "curation" / t["slug"] / "page.md").is_file() and not t.get("skipped")]
    if unwritten and len(unwritten) < len(topics):
        names = "、".join(f"{t['topic']}（{t['slug']}）" for t in unwritten)
        errors.append(f"整理页没写完：缺 {names}。写好 page.md 后重跑；确实不写的，在 kb-config.json 的 skip_topics 里写明理由")
    elif unwritten:
        warnings.append(f"还没有整理页：{len(unwritten)} 个主题已准备好，等 AI 写 page.md（04_知识库构建/curation）")
    usage_path = build / "curation-used.json"
    if usage_path.is_file() and load_json(usage_path):
        cited = {d for page in load_json(usage_path).values() for d in page["sources"]}
        idle = [docs[d]["title"] for d in published if statuses.get(d) == "有效" and docs[d]["tier"] in ("T1", "T2") and d not in cited]
        if idle:
            warnings.append(f"{len(idle)} 份有效资料没有被任何整理页引用：{'、'.join(idle[:8])}{' 等' if len(idle) > 8 else ''}")

    # Pages picked for a look beside their text: started and left half done is not finished.
    plan_path = build / "page-check" / "plan.json"
    check_plan = load_json(plan_path) if plan_path.is_file() else {}
    checked = load_json(build / "page-check" / "results.json") if (build / "page-check" / "results.json").is_file() else {}
    waiting = check_plan.get("to_check", 0)
    skip = config.get("page_check", {}).get("skip") if isinstance(config.get("page_check"), dict) else None
    if check_plan.get("ok") is False:
        warnings.append(f"没做看图核对：{check_plan.get('reason')}")
    elif waiting and not skip:
        if checked:
            errors.append(f"看图核对没做完：还有 {waiting} 页（04_知识库构建/page-check/todo.jsonl）。看完运行 page_check.py collect 再重跑；"
                          "确实不做，在 kb-config.json 的 page_check 里写 {\"skip\": \"理由\"}")
        else:
            warnings.append(f"还没做看图核对：{waiting} 页等 AI 对照原图看（04_知识库构建/page-check/README.md）")

    # A deck without slide pictures still has all its text; say why the pictures are missing.
    no_slides = Counter(item["page_images_skipped"] for item in extracted.values() if item.get("page_images_skipped"))
    if no_slides:
        warnings.append("PPT 没有幻灯片截图：" + "，".join(f"{reason} {count} 份" for reason, count in no_slides.most_common())
                        + "（文字已全部读取；装好 LibreOffice、PyMuPDF 后重跑即可补上）")

    # Kinds of material nobody has looked at an example of yet.
    waiting_path = build / "samples-waiting.json"
    unconfirmed = load_json(waiting_path) if waiting_path.is_file() else []
    if unconfirmed:
        warnings.append(f"{len(unconfirmed)} 类资料还没确认样例（{'、'.join(unconfirmed[:6])}）：看《处理计划与样例》，确认后再统一处理同类")

    # A website that was asked for but not read leaves a hole nobody would notice.
    if config.get("websites") and not any(d.get("origin") == "website" for d in docs.values()):
        fetch = build / "website" / "fetch-report.json"
        failed = load_json(fetch).get("failed", []) if fetch.is_file() else []
        reason = ("还没运行 fetch_website.py" if not fetch.is_file()
                  else "；".join(f"{f['url']}：{f['error']}" for f in failed[:3]) if failed
                  else "读到的页面都没有可用正文，可能要用浏览器读取")
        errors.append(f"kb-config.json 登记了官网，但知识库里没有官网页面（{reason}）。"
                      "重新读取，或用浏览器保存后 --browser-capture 导入；确定不要官网就从 websites 里删掉")

    rng = random.Random(args.seed)
    sample = rng.sample(kept, min(args.sample, len(kept)))
    lines = ["# 抽样核对", "", "把每一条和原件对照，看提取有没有错、有没有不该进库的内容。", ""]
    usage = load_json(usage_path) if usage_path.is_file() else {}
    if usage:
        lines.extend(["## 整理页全文", ""])
        for name, page in usage.items():
            path = build / "vault" / "03_正式知识" / "10_客户知识" / page["module"] / f"{name}.md"
            lines.append(f"- [ ] {page.get('title', name)}｜{path}")
        cited = {d for page in usage.values() for d in page["sources"] if d in published}
        candidates = sorted(cited)
        d_candidates = [d for d in candidates if docs[d].get("decision") == "D"]
        selected = rng.sample(d_candidates, min(8, len(d_candidates)))
        remaining = [d for d in candidates if d not in selected]
        selected += rng.sample(remaining, min(max(0, 15 - len(selected)), len(remaining)))
        if len(selected) < 15:
            remaining = sorted(d for d in published if d not in selected)
            selected += rng.sample(remaining, min(15 - len(selected), len(remaining)))
        lines.extend(["", "## 来源全文对照", ""])
        for doc_id in selected:
            doc = docs[doc_id]
            lines.extend([f"- [ ] {doc['title']}｜{doc_id}",
                          f"  - 净化原文：{clean / published[doc_id]}",
                          f"  - 原件：{doc.get('package_name', '')}/{doc['package_path']}"])
        lines.extend(["", "## 段落抽查", ""])
    for block in sample:
        doc = docs[block["doc_id"]]
        lines.append(f"- [ ] {doc['title']}｜{doc['package_path']}｜{block['locator']}")
        lines.append(f"  - {normalize_space(block['text'])[:300]}")
    write_text(build / "抽样核对.md", "\n".join(lines) + "\n")

    withdrawn = sum(1 for path in clean.rglob("*.md") if parse_frontmatter(path.read_text(encoding="utf-8")).get("status") == "已失效")
    report: dict[str, Any] = {
        "verified": not errors,
        "withdrawn": withdrawn,
        "documents": len(published),
        "blocks_kept": len(kept),
        "error_count": len(errors),
        "errors": errors[:200],
        "warnings": warnings,
        "sample_file": str(build / "抽样核对.md"),
    }
    write_json(build / "校验结果.json", report)
    print(json.dumps({key: report[key] for key in ("verified", "documents", "blocks_kept", "error_count")}, ensure_ascii=False))
    for error in errors[:15]:
        print(error)
    for warning in warnings:
        print("提醒：" + warning)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
