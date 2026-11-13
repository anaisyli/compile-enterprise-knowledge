#!/usr/bin/env python3
"""Shared rules and file helpers for the knowledge-base pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable


def _utf8_when_captured() -> None:
    """Chinese output read through a pipe (an agent, a log file) is UTF-8, not the Windows code page.

    A real console window is left alone: Python writes to it in Unicode already."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and (stream.encoding or "").replace("-", "").lower() != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_utf8_when_captured()
# Child processes (run_pipeline's steps, LibreOffice helpers) inherit the same choice.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SKILL_VERSION = "0.1.0"

MODULES = {
    "company": "10_公司概述",
    "product": "20_产品介绍",
    "solution": "30_解决方案",
    "case": "40_合作案例",
    "industry": "50_行业知识与洞察/10_企业提供",
    "faq": "60_FAQ",
    "other": "70_其他",
}

# Tier replaces per-claim evidence: it says how far a source can be trusted, and
# so which downstream use may read it without another check.
TIERS = {
    "T1": {"label": "官方发布物", "visibility": "public", "uses": ["article", "website", "crm", "agent"]},
    "T2": {"label": "公司自建文档", "visibility": "internal", "uses": ["article", "crm", "agent"]},
    "T3": {"label": "产品表字段", "visibility": "internal", "uses": ["crm", "agent"]},
    "T4": {"label": "单次沟通要点", "visibility": "internal", "uses": ["agent"]},
}

# Same labels as prepare-seo-knowledge-materials uses for resource_type.
RESOURCE_LABELS = {
    "marketing": "营销宣传",
    "business_scenario": "业务场景",
    "product": "产品资料",
    "competitor": "竞对资料",
    "seo_ops": "SEO运营资料",
    "transaction": "交易往来",
    "pricing": "价格",
    "finance": "财务",
    "noise": "无知识价值",
    "website": "客户官网",
    "unknown": "未归类",
}

ROLE_LABELS = {
    "style": "款号",
    "name": "品名",
    "description": "描述",
    "fabric": "面料",
    "size": "尺码",
}
ROLE_ORDER = ["style", "name", "description", "fabric", "size"]

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}
MAX_NAME_LENGTH = 90
PACKAGE_PREFIXES = re.compile(r"^(?:E-排除__05-其他__|发送优化师__A类-官方宣传材料__|A-原文保留__)+")

MANIFEST_NAME = "审计记录/package-manifest.json"
TRIAGE_NAME = "审计记录/triage-results.json"
POINTS_NAME = "C-知识点/knowledge-points.jsonl"

CLEAN_DIRECTORY = "03_净化md"
BUILD_DIRECTORY = "04_知识库构建"


def long_path(path: Path | str) -> str:
    """Return a path that survives the Windows 260-character limit."""
    text = os.path.abspath(str(path))
    if os.name == "nt" and len(text) >= 240 and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + text
    return text


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    return sha256_text(text)[:length]


def safe_name(name: str, fallback: str = "file") -> str:
    """Turn an untrusted name into one safe path component."""
    leaf = re.split(r"[\\/]", name)[-1]
    leaf = re.sub(r'[<>:"|?*#^\[\]\x00-\x1f]', "_", leaf).strip(" .")
    leaf = re.sub(r"\s+", " ", leaf) or fallback
    if leaf.split(".")[0].upper() in WINDOWS_RESERVED:
        leaf = f"_{leaf}"
    return leaf[:MAX_NAME_LENGTH].rstrip(" .")


def source_stem(display_name: str) -> str:
    """Drop the packaging prefixes earlier runs left on file names."""
    return Path(PACKAGE_PREFIXES.sub("", display_name)).stem


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remaining_ocr_pages(build: Path, item_id: str, pages: list[int],
                        folders: tuple[str, ...] = ("mineru-units", "reducto-units")) -> list[int]:
    units = []
    # Reducto's reading counts too: a page it read is not sent to MinerU's or the model's queue again.
    for folder in folders:
        path = build / folder / f"{item_id}.json"
        units += load_json(path).get("units", []) if path.is_file() else []
    covered = set()
    for unit in units:
        match = re.search(r"\b(?:page|slide)\s+(\d+)\b", unit.get("locator", ""), re.IGNORECASE)
        # A page someone looked at and found no words on is done too.
        if match and (unit.get("text", "").strip() or unit.get("empty")):
            covered.add(int(match.group(1)))
    return [page for page in pages if page not in covered]


PAGED_FILES = {".pdf", ".pptx", ".ppt"}


def read_by(build: Path) -> dict[str, set[str]]:
    """What read each file beyond its text layer: "mineru", "reducto", "ai" (AI 读图), "check" (pages looked at against the picture)."""
    found: dict[str, set[str]] = {}
    folder = build / "mineru-units"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        record = load_json(path)
        item_id = record.get("item_id") or path.stem
        if record.get("source") == "mineru":
            found.setdefault(item_id, set()).add("mineru")
        if any(unit.get("engine") == "ai" and not unit.get("checked") for unit in record.get("units", [])):
            found.setdefault(item_id, set()).add("ai")
    folder = build / "reducto-units"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        found.setdefault(load_json(path).get("item_id") or path.stem, set()).add("reducto")
    results = build / "page-check" / "results.json"
    for entry in (load_json(results) if results.is_file() else {}).values():
        found.setdefault(entry["item_id"], set()).add("check")
    return found


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(long_path(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def file_extension(name: str) -> str:
    """Lower-case extension. A file called just ".jpg" is a picture with no stem: Path gives it no
    suffix, so the name itself is the extension. ".DS_Store" comes out as ".ds_store"."""
    path = Path(name or "")
    if not path.suffix and path.name.startswith(".") and path.name.count(".") == 1:
        return path.name.casefold()
    return path.suffix.casefold()


def load_config(path: Path | None) -> dict[str, Any]:
    config = load_json(path) if path else {}
    if not isinstance(config, dict):
        raise ValueError("kb-config.json must contain a JSON object")
    for key in ("own_terms", "own_domains", "own_contacts", "deny_terms"):
        config.setdefault(key, [])
    config.setdefault("header_map", {})
    config.setdefault("vocabularies", [])
    config.setdefault("client_answers", {})
    config.setdefault("company", "")
    return config


def unapproved_d(routes: dict[str, Any], decisions: dict[str, Any]) -> list[str]:
    """Kept D files nobody has approved yet. Screening could not tell whether they may be used and
    marked nothing inside them, so they stay 待审 until a person or the AI that looked writes their
    item id (or DOC id) into review-decisions.json approve_docs."""
    approved = set(decisions.get("approve_docs", []))
    return sorted(i for i, r in routes.items() if r.get("decision") == "D" and r.get("route") == "kb"
                  and i not in approved and f"DOC-{(r.get('source_hash') or '')[:10]}" not in approved)


EXCEL_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(text: str) -> str:
    """Decode Office's _xHHHH_ escapes and drop control characters."""
    return CONTROL.sub("", EXCEL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text))


def load_units(text_dirs: list[Path], item_id: str) -> list[dict[str, str]]:
    """Read extracted text, later directories replacing earlier ones per locator.

    OCR output uses the same file format, so it fills pages the text layer missed.
    """
    merged: dict[str, dict[str, str]] = {}
    for directory in text_dirs:
        path = directory / f"{item_id}.json"
        if path.is_file():
            for unit in load_json(path).get("units", []):
                merged[unit["locator"]] = {**unit, "text": clean_text(unit["text"])}
    return list(merged.values())


def preferred_page_units(units: list[dict[str, str]]) -> list[dict[str, str]]:
    """Use nonempty platform page readings instead of the same page's native text."""
    # MinerU's cached page records from older runs used ``engine: null``.  The
    # locator is still the platform's full-page OCR unit (``page N ocr``), so
    # treat a missing/empty engine as MinerU for page replacement.  AI page
    # readings remain excluded because their engine is explicitly ``ai``.
    pages = {
        unit["locator"].removesuffix(" ocr")
        for unit in units
        if re.fullmatch(r"(?:page|slide) \d+ ocr", unit["locator"])
        and unit.get("engine") in {None, "", "mineru", "reducto"}
        and unit["text"].strip()
    }
    return [unit for unit in units
            if re.sub(r" (?:col|table) \d+$", "", unit["locator"]) not in pages]


def is_drawing_dimension_line(text: str) -> bool:
    """Recognize standalone engineering callouts without suppressing prose or table rows."""
    line = normalize_space(text)
    # A parameter table stays a table, even when printed beside a drawing.
    if not line or line.startswith("|") or re.search(r"<\s*(?:table|tr|td|th)\b", line, re.I):
        return False
    line = re.sub(r"^#{1,6}\s+", "", line)
    if re.fullmatch(r"(?i)(?:[ΦφØ⌀]?[ABCDHLMNRSTWXZ]\d?|n\s*[-−]\s*[ΦφØ⌀]?[A-Z]?|"
                    r"[□ΦφØ⌀]?b\s*[×x*]\s*b|[ΦφØ⌀][A-Z]?|N-Th|BXB|n-b|n-d)", line):
        return True
    if not re.search(r"\d", line):
        return False
    labels = re.compile(r"(?i)\b(?:DN|VESA|PCD|BCD|OD|ID|mm|cm|inch|in|deg|kg|lb)\b|"
                        r"[ΦØ⌀]\s*(?:D|L|W|H|R|M)(?=\s*[=<>≤≥±]?\s*\d)|"
                        r"(?<!\w)(?:L|W|H|R|M|D)(?=\s*[=<>≤≥±]?\s*\d)|[ΦØ⌀]")
    remainder = labels.sub("", line)
    if re.fullmatch(r"[\d\s.,+\-±~≤≥<>=:：/×xX*()'\"°%寸米厘纳]+", remainder):
        return True
    return False


def suppress_drawing_dimensions(units: list[dict[str, str]]) -> list[dict[str, str]]:
    """Exclude dimension callouts only on pages explicitly reviewed as dimension drawings."""
    pages = {
        unit["locator"].removesuffix(" drawing")
        for unit in units if unit.get("dimension_drawing")
    }
    if not pages:
        return units
    result = []
    for unit in units:
        locator = unit["locator"]
        page = re.match(r"^((?:page|slide) \d+)(?: |$)", locator)
        if unit.get("dimension_drawing") or page is None or page.group(1) not in pages:
            result.append(unit)
            continue
        # The drawing is an image asset; its model-generated description must not
        # reintroduce hole counts, dimensions or inferred geometry into prose.
        if unit.get("figure_description"):
            continue
        # Human full-page supplements are independent evidence, not OCR drafts.
        if unit.get("reviewed_supplement") or unit.get("engine") == "reviewed":
            result.append(unit)
            continue
        lines = [line for line in unit["text"].splitlines() if not is_drawing_dimension_line(line)]
        if lines:
            result.append({**unit, "text": "\n".join(lines), "drawing_filtered": True})
    return result


def reducto_figure_units(build: Path, item_id: str, source_name: str) -> list[dict[str, Any]]:
    """Keep page-linked model descriptions separate from transcribed source words."""
    path = build / "reducto-units" / f"{item_id}.json"
    if not path.is_file():
        return []
    unit = "slide" if file_extension(source_name) in {".ppt", ".pptx"} else "page"
    result = []
    for page, figures in load_json(path).get("figures", {}).items():
        seen: set[str] = set()
        for index, text in enumerate(figures):
            text = clean_text(text).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append({"locator": f"{unit} {page} figure {index + 1}", "text": text,
                           "engine": "reducto", "figure_description": True,
                           "figure_source_locator": f'figures["{page}"][{index}]'})
    return result


def yaml_scalar(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(yaml_scalar(entry) for entry in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {yaml_scalar(value)}" for key, value in fields.items())
    lines.append("---")
    return "\n".join(lines)


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    fields = {}
    for line in text[4:end].splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value.strip().strip('"')
    return fields
