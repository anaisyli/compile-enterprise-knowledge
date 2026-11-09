#!/usr/bin/env python3
"""Extract text from the files inside a preparation package, so nothing else is needed.

The screening step keeps its extracted text in a working folder that colleagues rarely
hand over. Every file is in the package anyway, with its hash, so this reads them again.
Locators match prepare-seo-knowledge-materials exactly ("sheet Sheet1 (visible)",
"page 3", "para 12"), because the cleanup locations recorded at screening refer to them.
Each file's hash is checked first: a copy that changed is not read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import threading
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from kb_common import BUILD_DIRECTORY, MANIFEST_NAME, load_json, long_path, sha256_file, short_hash, write_json
from layout import ordered_text
from office import ConversionError, convert_office, disable as disable_office, self_test as office_self_test


WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PRESENTATION = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELATIONSHIP = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml"}
LEGACY_OFFICE = {".xls": "xlsx", ".doc": "docx", ".ppt": "pptx"}
MIN_PAGE_CHARACTERS = 20
MAX_ROWS_PER_SHEET = 2000
TEXT_DIRECTORY = "package-text"
CONVERTED_DIRECTORY = "converted"
# Excluded emails are read only to learn customer names; excluded spreadsheets and PDFs
# are mostly finance and quotations, and are skipped to save time.
EXCLUDED_READ = {".md", ".txt", ".eml"}
# Page pictures let a reader check a catalog page against its text; long files are skipped.
MAX_THUMBNAIL_PAGES = 80
THUMBNAIL_DPI = 45
ASSETS_DIRECTORY = "assets"
# Raised when the way a kind of file is read changes, so only those files are read again:
# a change to PDF reading must not send every old workbook back through LibreOffice.
READER_VERSIONS = {".pdf": 4, ".pptx": 4, ".ppt": 4, ".docx": 3, ".doc": 3}
# Photos printed in a PDF are kept as material when they are big enough to be one.
PICTURE_MIN_SIDE = 300
PICTURE_MIN_BYTES = 15_000
PICTURE_REPEATED = 3
MAX_PICTURES_PER_FILE = 400


def reader_version(name: str) -> int:
    return READER_VERSIONS.get(Path(name).suffix.casefold(), 1)
TABLE_MIN_ROWS = 2
TABLE_FILL = 0.5


def docx_units(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    units = []
    with zipfile.ZipFile(long_path(path)) as archive:
        body = ET.fromstring(archive.read("word/document.xml")).find(f"{WORD}body")
        for index, child in enumerate(body if body is not None else [], start=1):
            if child.tag == f"{WORD}p":
                text = "".join(node.text or "" for node in child.iter(f"{WORD}t")).strip()
                if text:
                    units.append({"locator": f"para {index}", "text": text})
            elif child.tag == f"{WORD}tbl":
                rows = [["".join(node.text or "" for node in cell.iter(f"{WORD}t")).strip() for cell in row.findall(f"{WORD}tc")]
                        for row in child.findall(f"{WORD}tr")]
                rows = [row for row in rows if any(row)]
                if rows:
                    units.append({"locator": f"table at {index}", "text": markdown_table(rows)})
    return units, {}


def slide_boxes(root: ET.Element) -> list[dict[str, Any]]:
    """Text boxes and tables of a slide with where they sit. Boxes without a position go last."""
    boxes = []
    for shape in root.iter():
        if shape.tag not in {f"{PRESENTATION}sp", f"{PRESENTATION}graphicFrame"}:
            continue
        lines = ["".join(node.text or "" for node in paragraph.iter(f"{DRAWING}t")) for paragraph in shape.iter(f"{DRAWING}p")]
        text = "\n".join(line for line in lines if line.strip())
        if not text:
            continue
        offset = next(shape.iter(f"{DRAWING}off"), None)
        extent = next(shape.iter(f"{DRAWING}ext"), None)
        if offset is None or extent is None:
            x, y, cx, cy = 0.0, 1e12, 0.0, 0.0
        else:
            x, y = float(offset.get("x", 0)), float(offset.get("y", 0))
            cx, cy = float(extent.get("cx", 0)), float(extent.get("cy", 0))
        boxes.append({"x0": x, "y0": y, "x1": x + cx, "y1": y + cy, "text": text})
    return boxes


def pptx_units(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    units, image_only = [], []
    with zipfile.ZipFile(long_path(path)) as archive:
        names = set(archive.namelist())
        size = ET.fromstring(archive.read("ppt/presentation.xml")).find(f"{PRESENTATION}sldSz") if "ppt/presentation.xml" in names else None
        width = float(size.get("cx", 0)) if size is not None else 0.0
        slides = []
        for name in names:
            match = re.fullmatch(r"ppt/(slides/slide|notesSlides/notesSlide)(\d+)\.xml", name)
            if match:
                slides.append(("notes" in name, int(match.group(2)), name))
        for is_notes, number, name in sorted(slides):
            root = ET.fromstring(archive.read(name))
            if is_notes:
                # Speaker notes are the presenter's words, kept apart from the slide.
                text = "\n".join(node.text for node in root.iter(f"{DRAWING}t") if node.text and not node.text.strip().isdigit())
                if text.strip():
                    units.append({"locator": f"notes {number}", "text": text})
                continue
            parts = ordered_text(slide_boxes(root), width)
            if len("".join(parts).strip()) < MIN_PAGE_CHARACTERS and any(n.tag.endswith("}pic") for n in root.iter()):
                image_only.append(number)
            units.extend(column_units("slide", number, parts))
    facts: dict[str, Any] = {"pages": len([s for s in slides if not s[0]]), "layout": "columns"}
    if image_only:
        facts["ocr_pages"] = image_only
    return units, facts


RELATIONSHIP = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def slide_order(path: Path) -> list[int]:
    """Slide file numbers in the order the deck shows them, hidden slides left out: the order a
    PDF export prints them in. slide7.xml can be the third slide of a reordered deck."""
    with zipfile.ZipFile(long_path(path)) as archive:
        names = set(archive.namelist())
        if "ppt/presentation.xml" not in names or "ppt/_rels/presentation.xml.rels" not in names:
            return []
        targets = {rel.get("Id"): rel.get("Target", "") for rel in ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))}
        order = []
        for entry in ET.fromstring(archive.read("ppt/presentation.xml")).iter(f"{PRESENTATION}sldId"):
            match = re.search(r"slides/slide(\d+)\.xml$", targets.get(entry.get(f"{RELATIONSHIP}id"), ""))
            if not match:
                continue
            name = f"ppt/slides/slide{match.group(1)}.xml"
            if name in names and ET.fromstring(archive.read(name)).get("show") != "0":
                order.append(int(match.group(1)))
        return order


def slide_pictures(path: Path, cache: Path, key: str, timeout: int) -> dict[str, Any]:
    """A picture of every slide, like a PDF page, so the text can be checked against the slide.
    The deck goes through LibreOffice to a PDF; without it the text is still read in full."""
    order = slide_order(path)
    if not order:
        return {"page_images_skipped": "读不出幻灯片顺序"}
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        return {"page_images_skipped": "本机没装 PyMuPDF"}
    if len(order) > MAX_THUMBNAIL_PAGES:
        return {"page_images_skipped": f"超过 {MAX_THUMBNAIL_PAGES} 张幻灯片"}
    try:
        pdf = convert_office(path, "pdf", cache, f"{key}-slides", timeout)
    except (LookupError, ConversionError, OSError) as exc:
        return {"page_images_skipped": "需要 LibreOffice" if isinstance(exc, LookupError) else f"转 PDF 失败：{str(exc)[:80]}"}
    assets = cache.parent / ASSETS_DIRECTORY
    images: dict[str, str] = {}
    with pymupdf.open(long_path(pdf)) as document:
        if document.page_count != len(order):
            # Pictures beside the wrong slide would mislead; none is better.
            return {"page_images_skipped": f"导出的 {document.page_count} 页和 {len(order)} 张幻灯片对不上"}
        for page, number in zip(document, order):
            name = f"img-{short_hash(f'{key}|slide {number}', 16)}.png"
            if not (assets / name).is_file():
                assets.mkdir(parents=True, exist_ok=True)
                page.get_pixmap(dpi=THUMBNAIL_DPI).save(str(assets / name))
            images[str(number)] = name
        found = page_pictures(document, pymupdf, assets)
    # The PDF numbers pages 1, 2, 3; the text numbers slides by their file.
    pictures = {str(order[int(page) - 1]): names for page, names in found.items()}
    return {"page_images": images, "page_pictures": pictures, "page_unit": "slide"}


def xlsx_units(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    units = []
    with zipfile.ZipFile(long_path(path)) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for entry in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(f"{SHEET}si"):
                shared.append("".join(node.text or "" for node in entry.iter(f"{SHEET}t")))
        targets = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for relation in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels")):
                targets[relation.get("Id")] = relation.get("Target", "").lstrip("/")
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).iter(f"{SHEET}sheet"):
            state = sheet.get("state") or "visible"
            target = targets.get(sheet.get(f"{RELATIONSHIP}id"), "")
            target = target if target.startswith("xl/") else f"xl/{target}"
            if target not in names:
                continue
            rows = []
            for row in ET.fromstring(archive.read(target)).iter(f"{SHEET}row"):
                cells = []
                for cell in row.findall(f"{SHEET}c"):
                    value, inline = cell.find(f"{SHEET}v"), cell.find(f"{SHEET}is")
                    if cell.get("t") == "s" and value is not None and value.text:
                        position = int(value.text)
                        text = shared[position] if position < len(shared) else ""
                    elif inline is not None:
                        text = "".join(node.text or "" for node in inline.iter(f"{SHEET}t"))
                    else:
                        text = value.text if value is not None and value.text else ""
                    if text.strip():
                        cells.append(f"{cell.get('r')}={text.strip()}")
                if cells:
                    rows.append("; ".join(cells))
            if rows:
                units.append({"locator": f"sheet {sheet.get('name')} ({state})", "text": "\n".join(rows[:MAX_ROWS_PER_SHEET])})
    return units, {}


def column_units(prefix: str, number: int, parts: list[str]) -> list[dict[str, str]]:
    """The first column keeps the plain locator the screening step used; the others add "col N"."""
    return [{"locator": f"{prefix} {number}" + (f" col {index}" if index > 1 else ""), "text": part}
            for index, part in enumerate(parts, start=1) if part.strip()]


def markdown_table(rows: list[list[str]]) -> str:
    """Rows of cell text as a Markdown table, the first row as its header. Words are not changed."""
    width = max(len(row) for row in rows)
    cells = [[re.sub(r"\s+", " ", cell or "").strip().replace("|", "／") for cell in row] + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(row) + " |" for row in cells[1:]]
    return "\n".join(lines)


def page_tables(page: Any) -> list[tuple[tuple[float, float, float, float], str]]:
    """Real tables only: ruled layouts on catalog pages are not tables unless most cells hold text."""
    found = []
    try:
        tables = page.find_tables().tables
    except Exception:  # An older PyMuPDF without table finding reads the page as before.
        return []
    for table in tables:
        rows = [row for row in table.extract() if any((cell or "").strip() for cell in row)]
        filled = sum(1 for row in rows for cell in row if (cell or "").strip())
        if len(rows) >= TABLE_MIN_ROWS and table.col_count >= 2 and filled >= TABLE_FILL * len(rows) * table.col_count:
            found.append((tuple(table.bbox), markdown_table(rows)))
    return found


def inside(box: dict[str, Any], area: tuple[float, float, float, float]) -> bool:
    x, y = (box["x0"] + box["x1"]) / 2, (box["y0"] + box["y1"]) / 2
    return area[0] <= x <= area[2] and area[1] <= y <= area[3]


def pdf_units_by_layout(path: Path, pymupdf: Any, thumbnails: Path | None, key: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """With PyMuPDF the boxes keep their positions, so side-by-side products stay apart."""
    units, ocr_pages, images = [], [], {}
    with pymupdf.open(long_path(path)) as document:
        for number, page in enumerate(document, start=1):
            # Lines, not PyMuPDF's blocks: a block can join a product name on the left with a
            # code printed far to the right on the same baseline.
            boxes = [{"x0": line["bbox"][0], "y0": line["bbox"][1], "x1": line["bbox"][2], "y1": line["bbox"][3],
                      "text": "".join(span["text"] for span in line["spans"])}
                     for block in page.get_text("dict")["blocks"] if block.get("type") == 0 for line in block["lines"]]
            tables = page_tables(page)
            # A table is read row by row; read in columns, its rows would fall apart.
            boxes = [b for b in boxes if not any(inside(b, area) for area, _ in tables)]
            parts = ordered_text(boxes, page.rect.width)
            if len("".join(parts).strip()) + sum(len(t) for _, t in tables) < MIN_PAGE_CHARACTERS:
                ocr_pages.append(number)
            units.extend(column_units("page", number, parts))
            units.extend({"locator": f"page {number} table {index}", "text": text} for index, (_, text) in enumerate(tables, start=1))
            if thumbnails is not None and document.page_count <= MAX_THUMBNAIL_PAGES:
                name = f"img-{short_hash(f'{key}|page {number}', 16)}.png"
                if not (thumbnails / name).is_file():
                    thumbnails.mkdir(parents=True, exist_ok=True)
                    page.get_pixmap(dpi=THUMBNAIL_DPI).save(str(thumbnails / name))
                images[str(number)] = name
        pages = document.page_count
        pictures = page_pictures(document, pymupdf, thumbnails) if thumbnails is not None else {}
    return units, {"pages": pages, "ocr_pages": ocr_pages, "layout": "columns", "page_images": images, "page_pictures": pictures}


def page_pictures(document: Any, pymupdf: Any, folder: Path) -> dict[str, list[str]]:
    """The photos printed in a PDF, stored once per picture: the material a website is built from.
    Small pictures are icons, and one on many pages is a logo or a background."""
    pages_of: dict[int, list[int]] = {}
    for number, page in enumerate(document, start=1):
        for info in page.get_images(full=True):
            pages_of.setdefault(info[0], [])
            if number not in pages_of[info[0]]:
                pages_of[info[0]].append(number)
    found: dict[str, list[str]] = {}
    saved = 0
    for xref, numbers in pages_of.items():
        if len(numbers) > PICTURE_REPEATED or saved >= MAX_PICTURES_PER_FILE:
            continue
        try:
            image = document.extract_image(xref)
            if min(image.get("width", 0), image.get("height", 0)) < PICTURE_MIN_SIDE or len(image.get("image", b"")) < PICTURE_MIN_BYTES:
                continue
            data, suffix = image["image"], "." + image.get("ext", "png").casefold().replace("jpeg", "jpg")
            if suffix not in {".jpg", ".png"}:
                pixmap = pymupdf.Pixmap(document, xref)
                if pixmap.n - pixmap.alpha > 3:
                    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)  # CMYK prints, RGB screens.
                data, suffix = pixmap.tobytes("png"), ".png"
        except Exception:  # A damaged picture leaves the text untouched.
            continue
        name = f"img-{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
        if not (folder / name).is_file():
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
        saved += 1
        for number in numbers:
            found.setdefault(str(number), []).append(name)
    return found


def pdf_units(path: Path, thumbnails: Path | None = None, key: str = "") -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Read the text layer. Pages without one are reported for MinerU, not guessed at."""
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        pymupdf = None
    if pymupdf is not None:
        return pdf_units_by_layout(path, pymupdf, thumbnails, key)
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise LookupError("needs_pdf_reader") from exc
    reader = PdfReader(long_path(path))
    units, ocr_pages = [], []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if len(text) < MIN_PAGE_CHARACTERS:
            ocr_pages.append(number)
        if text:
            units.append({"locator": f"page {number}", "text": text})
    # Without PyMuPDF the text comes in stored order; products side by side may run together.
    return units, {"pages": len(reader.pages), "ocr_pages": ocr_pages, "layout": "stored_order"}


def convert(path: Path, target: str, cache: Path, key: str, timeout: int) -> Path:
    """Convert an old Office file once; the image step reuses the same copy."""
    return convert_office(path, target, cache, key, timeout)


def read_item(path: Path, extension: str, cache: Path, key: str, timeout: int) -> dict[str, Any]:
    if extension in LEGACY_OFFICE:
        path, extension = convert(path, LEGACY_OFFICE[extension], cache, key, timeout), f".{LEGACY_OFFICE[extension]}"
    if extension == ".docx":
        units, facts = docx_units(path)
    elif extension == ".pptx":
        units, facts = pptx_units(path)
        facts.update(slide_pictures(path, cache, key, timeout))
    elif extension == ".xlsx":
        units, facts = xlsx_units(path)
    elif extension == ".pdf":
        units, facts = pdf_units(path, cache.parent / ASSETS_DIRECTORY, key)
    elif extension in TEXT_EXTENSIONS or extension == ".eml":
        units, facts = [{"locator": "file", "text": path.read_text(encoding="utf-8", errors="replace")}], {}
    else:
        return {"status": "unsupported", "characters": 0}
    characters = sum(len(unit["text"]) for unit in units)
    status = "read_ok"
    if facts.get("ocr_pages"):
        status = "needs_ocr" if characters < MIN_PAGE_CHARACTERS else "partial"
    return {"status": status, "characters": characters, **facts, "units": units}


def wanted(asset: dict[str, Any]) -> bool:
    if asset.get("copy_status") != "copied" or not asset.get("path") or asset.get("asset_kind", "document") != "document":
        return False
    if asset.get("decision") == "E":
        return Path(asset.get("display_name") or "").suffix.casefold() in EXCLUDED_READ
    return asset.get("decision") in {"A", "B", "C", "D"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract text from the files inside preparation packages.")
    parser.add_argument("--package", action="append", required=True, help="Preparation package; repeat")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120, help="Seconds per old-format conversion; a hang is killed after this")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    text_dir, cache = build / TEXT_DIRECTORY, build / CONVERTED_DIRECTORY
    text_dir.mkdir(parents=True, exist_ok=True)
    status_path = text_dir / "extract-status.json"
    statuses: dict[str, Any] = load_json(status_path).get("items", {}) if status_path.is_file() else {}

    # Kinds of file nobody has confirmed yet are read on their sample only (sample_gate.py).
    gate_path = build / "sample-gate.json"
    allowed = set(load_json(gate_path)["allowed"]) if gate_path.is_file() else None
    jobs = []
    for raw in args.package:
        package = Path(raw).resolve()
        for asset in load_json(package / MANIFEST_NAME)["assets"]:
            if not wanted(asset):
                continue
            if allowed is not None and asset["item_id"] not in allowed and asset.get("decision") != "E":
                continue
            previous = statuses.get(asset["item_id"], {})
            needed = reader_version(asset.get("display_name") or asset["path"])
            done = previous.get("source_hash") == asset["source_hash"] and int(previous.get("extractor") or 1) >= needed \
                and previous.get("status") in {"read_ok", "partial", "needs_ocr", "unsupported"}
            if done and (previous.get("status") == "unsupported" or (text_dir / f"{asset['item_id']}.json").is_file()):
                continue  # Same bytes, read the same way as last time: nothing to redo.
            jobs.append((asset, package / asset["path"]))

    # Try LibreOffice once on a tiny file before old Office files wait on it one by one.
    legacy = [a for a, _ in jobs if Path(a.get("display_name") or a["path"]).suffix.casefold() in LEGACY_OFFICE
              and not (cache / f"{a['source_hash'][:16]}.{LEGACY_OFFICE[Path(a.get('display_name') or a['path']).suffix.casefold()]}").is_file()]
    if legacy:
        problem = office_self_test(cache / "self-test")
        if problem:
            disable_office(problem)
            print(f"LibreOffice 在这里转换不了（{problem}）：{len(legacy)} 个 .xls/.doc/.ppt 先不读，标为 needs_libreoffice。"
                  "在沙盒里运行时，用不受沙盒限制的方式重跑这一步，或在本机终端运行；也可以把它们另存为 .xlsx/.docx/.pptx。", flush=True)
    progress = {"done": 0}
    progress_lock = threading.Lock()

    def one(job: tuple[dict[str, Any], Path]) -> tuple[str, dict[str, Any]]:
        item_id, status = read_one(job)
        if Path(job[0].get("display_name") or "").suffix.casefold() in LEGACY_OFFICE:
            with progress_lock:
                progress["done"] += 1
                # Old files go through LibreOffice one at a time; a line each shows the run is moving.
                print(f"[{progress['done']}/{len(legacy)}] {status['status']}  {job[0].get('display_name')}", flush=True)
        return item_id, status

    def read_one(job: tuple[dict[str, Any], Path]) -> tuple[str, dict[str, Any]]:
        asset, path = job
        base = {"display_name": asset.get("display_name"), "source_hash": asset["source_hash"],
                "extractor": reader_version(asset.get("display_name") or asset["path"])}
        try:
            if not Path(long_path(path)).is_file():
                return asset["item_id"], {**base, "status": "missing_file", "characters": 0}
            if sha256_file(path) != asset["source_hash"]:
                return asset["item_id"], {**base, "status": "hash_mismatch", "characters": 0}
            result = read_item(path, Path(asset.get("display_name") or path.name).suffix.casefold(), cache, asset["source_hash"][:16], args.timeout)
        except LookupError as exc:
            return asset["item_id"], {**base, "status": str(exc.args[0]), "characters": 0}
        except subprocess.TimeoutExpired:
            return asset["item_id"], {**base, "status": "failed", "error": "conversion_timeout", "characters": 0}
        except ConversionError as exc:
            return asset["item_id"], {**base, "status": "failed", "error": f"conversion_failed: {exc}", "characters": 0}
        except Exception as exc:  # A damaged file is reported, never stops the run.
            return asset["item_id"], {**base, "status": "failed", "error": type(exc).__name__, "characters": 0}
        units = result.pop("units", [])
        if units:
            write_json(text_dir / f"{asset['item_id']}.json", {"item_id": asset["item_id"], "units": units})
        return asset["item_id"], {**base, **result}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for item_id, status in pool.map(one, jobs):
            statuses[item_id] = status
    # Folders an older version left behind when LibreOffice still held them.
    for leftover in cache.glob("lo-*") if cache.is_dir() else []:
        shutil.rmtree(leftover, ignore_errors=True)
    write_json(status_path, {"items": statuses})
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status["status"]] = counts.get(status["status"], 0) + 1
    print(json.dumps({"read_now": len(jobs), "files": len(statuses), "status": counts}, ensure_ascii=False))
    if counts.get("needs_pdf_reader"):
        print("有 PDF 没读：先运行 python -m pip install pypdf 再重跑；不装的话，这些 PDF 只能交给 MinerU。")
    if counts.get("needs_libreoffice"):
        print("有 .xls/.doc/.ppt 没读：要装 LibreOffice，或者 LibreOffice 在这里转换不了（运行 check_tools.py 看原因）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
