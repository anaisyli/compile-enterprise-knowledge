#!/usr/bin/env python3
"""Shared rules and file helpers for the knowledge-base pipeline."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
from datetime import datetime
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


RUN_LOCK = ".run_pipeline.lock"   # Same file name as the pipeline's old lock; tests rely on it.
LOCK_HOLDER = "CEK_RUN_LOCK"      # Env var naming the pid of the process holding the lock.


def alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def run_lock(run: Path, what: str) -> bool:
    """One script at a time per run folder: two would overwrite each other's files halfway.

    The lock file holds "<pid> <time> <what>". A step spawned by the holder inherits
    LOCK_HOLDER and passes without touching the file; the holder's atexit removes it, and
    only while it still holds our pid. Returns False when a live foreign process holds it."""
    run.mkdir(parents=True, exist_ok=True)
    lock = run / RUN_LOCK
    if lock.is_file():
        try:
            parts = lock.read_text(encoding="utf-8").split()
            pid = int(parts[0])
        except (ValueError, IndexError, OSError):
            parts, pid = [], 0
        if pid and pid != os.getpid() and os.environ.get(LOCK_HOLDER) != str(pid) and alive(pid):
            holder = " ".join(parts[2:]) if len(parts) > 2 else "建库"
            print(f"这个运行目录正在运行 {holder}（进程 {pid}）。等它跑完再来；确定它已经停了，删掉 {lock} 再重跑。")
            return False
        if pid and os.environ.get(LOCK_HOLDER) == str(pid):
            return True  # Inherited from the parent that holds the lock; the parent removes it.
    lock.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')} {what}\n", encoding="utf-8")
    mine = str(os.getpid())

    def release() -> None:
        try:
            if lock.is_file() and lock.read_text(encoding="utf-8").split()[:1] == [mine]:
                lock.unlink()
        except OSError:
            pass

    atexit.register(release)
    return True


SITE_VIEW_ROOT = "03_正式知识/80_建站资料"


def site_view_enabled(config: dict[str, Any]) -> bool:
    """The 80_建站资料 view is made only when asked for: "site_view": {"enabled": true}."""
    setting = config.get("site_view")
    return isinstance(setting, dict) and setting.get("enabled") is True


def site_view_unset(config: dict[str, Any]) -> bool:
    return not isinstance(config.get("site_view"), dict) or "enabled" not in config["site_view"]


def file_extension(name: str) -> str:
    """Lower-case extension. A file called just ".jpg" is a picture with no stem: Path gives it no
    suffix, so the name itself is the extension. ".DS_Store" comes out as ".ds_store"."""
    path = Path(name or "")
    if not path.suffix and path.name.startswith(".") and path.name.count(".") == 1:
        return path.name.casefold()
    return path.suffix.casefold()


def normalize_file_decisions(config: dict[str, Any]) -> dict[str, Any]:
    """Fold unified `file_decisions` entries into the per-purpose config fields.

    Supports writing file-level decisions in one place (`file_decisions: {item_id: ...}`) while
    keeping existing fields (`d_keep`, `d_drop`, `excluded_items`, `ask_client`, `drawing_items`,
    `not_drawing_items`) working for active projects like P-010.
    """
    unified = config.get("file_decisions")
    if not isinstance(unified, dict) or not unified:
        return config
    d_keep = list(config.get("d_keep") or [])
    d_drop = list(config.get("d_drop") or [])
    excluded = dict(config.get("excluded_items") or {}) if isinstance(config.get("excluded_items"), dict) else {
        i: "excluded" for i in (config.get("excluded_items") or [])}
    ask_client = dict(config.get("ask_client") or {})
    drawing = list(config.get("drawing_items") or [])
    not_drawing = list(config.get("not_drawing_items") or [])
    for item_id, spec in unified.items():
        decision = spec.get("decision", "") if isinstance(spec, dict) else str(spec).strip()
        reason = spec.get("reason", "file_decisions") if isinstance(spec, dict) else "file_decisions"
        if decision.startswith("ask_client:"):
            reason = decision.split(":", 1)[1].strip() or reason
            decision = "ask_client"
        if decision == "keep":
            if item_id not in d_keep:
                d_keep.append(item_id)
        elif decision == "drop":
            if item_id not in d_drop:
                d_drop.append(item_id)
        elif decision == "exclude":
            excluded[item_id] = reason
        elif decision == "ask_client":
            ask_client[item_id] = reason
        elif decision == "drawing":
            if item_id not in drawing:
                drawing.append(item_id)
        elif decision == "not_drawing":
            if item_id not in not_drawing:
                not_drawing.append(item_id)
    if d_keep:
        config["d_keep"] = d_keep
    if d_drop:
        config["d_drop"] = d_drop
    if excluded:
        config["excluded_items"] = excluded
    if ask_client:
        config["ask_client"] = ask_client
    if drawing:
        config["drawing_items"] = drawing
    if not_drawing:
        config["not_drawing_items"] = not_drawing
    return config


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
    return normalize_file_decisions(config)


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

    OCR output uses the same file format, so it fills pages the text layer missed. A Reducto record
    marked "primary" (Reducto was chosen for this file) goes on top of everything else: its nonempty
    pages replace MinerU's, which otherwise come later and win.
    """
    merged: dict[str, dict[str, str]] = {}
    on_top: list[dict[str, Any]] = []
    for directory in text_dirs:
        path = directory / f"{item_id}.json"
        if path.is_file():
            record = load_json(path)
            if directory.name == "reducto-units" and record.get("primary"):
                on_top = [u for u in record.get("units", []) if (u.get("text") or "").strip()]
            for unit in record.get("units", []):
                merged[unit["locator"]] = {**unit, "text": clean_text(unit["text"])}
    for unit in on_top:
        merged[unit["locator"]] = {**unit, "text": clean_text(unit["text"])}
    return list(merged.values())


def unit_dirs(build: Path, text_dirs: Iterable[Path] = (), ocr_dir: Path | None = None) -> list[Path]:
    """Canonical directory order for loading page/document units across all pipeline scripts."""
    dirs = [Path(d) for d in text_dirs]
    reducto = build / "reducto-units"
    mineru = Path(ocr_dir) if ocr_dir else (build / "mineru-units")
    for folder in (reducto, mineru):
        if folder not in dirs:
            dirs.append(folder)
    return dirs


def is_platform_page_unit(unit: dict[str, Any]) -> bool:
    """True when a unit is a non-empty full-page OCR reading from MinerU or Reducto (not AI)."""
    return (
        bool(re.fullmatch(r"(?:page|slide) \d+ ocr", unit.get("locator", "")))
        and unit.get("engine") in {None, "", "mineru", "reducto"}
        and bool((unit.get("text") or "").strip())
    )


def mineru_page_units(build: Path, item_id: str) -> dict[int, str]:
    """Page number -> MinerU's own full-page OCR text for an item (excluding AI-read or check units)."""
    path = build / "mineru-units" / f"{item_id}.json"
    units = load_json(path).get("units", []) if path.is_file() else []
    result: dict[int, str] = {}
    for unit in units:
        match = re.fullmatch(r"(?:page|slide) (\d+) ocr", unit.get("locator", ""))
        if match and unit.get("engine") not in {"ai"} and not unit.get("checked") and (unit.get("text") or "").strip():
            result[int(match.group(1))] = unit["text"]
    return result


def preferred_page_units(units: list[dict[str, str]]) -> list[dict[str, str]]:
    """Use nonempty platform page readings instead of the same page's native text."""
    pages = {
        unit["locator"].removesuffix(" ocr")
        for unit in units
        if is_platform_page_unit(unit)
    }
    return [unit for unit in units
            if re.sub(r" (?:col|table) \d+$", "", unit["locator"]) not in pages]


def is_drawing_dimension_line(text: str) -> bool:
    """Recognize standalone engineering callouts without suppressing prose or table rows."""
    line = normalize_space(text)
    # A parameter table stays a table, even when printed beside a drawing.
    if not line or line.startswith("|") or re.search(r"<\s*(?:table|tr|td|th)\b", line, re.I):
        return False
    line = re.sub(r"</?(?:b|strong|span)(?:\s[^>]*)?>", "", line, flags=re.I).strip()
    if re.fullmatch(r"(?i)[-*•]?\s*(?:D[12]?|N-Th|d)(?:\s*[-–:]\s*|\s+)(?:Flange outer diameter|Bolt circle diameter|"
                    r"Diameter of sealing surface|Bolt size|Bolt hole diameter)", line):
        return True
    line = re.sub(r"^#{1,6}\s+", "", line)
    if re.fullmatch(r"(?i)(?:[ΦφØ⌀]?[ABCDHLMNRSTWXZ]\d?|n\s*[-−]\s*[ΦφØ⌀]?[A-Z]?|"
                    r"[□ΦφØ⌀]?b\s*[×x*]\s*b|[ΦφØ⌀][A-Z]?|N-Th|BXB|n-b|n-d)", line):
        return True
    if re.fullmatch(r"(?i)[●○⚫⚪+]?\s*(?:Blind hole thread|Through hole|Drilling through)", line):
        return True
    if not re.search(r"\d", line):
        return False
    labels = re.compile(r"(?i)\b(?:DN|PN|VESA|PCD|BCD|OD|ID)(?=\s*[\d≤≥<>=:(（])|"
                        r"\b(?:DN|VESA|PCD|BCD|OD|ID|mm|cm|inch|in|deg|kg|lb)\b|"
                        r"[ΦØ⌀]\s*(?:D|L|W|H|R|M)(?=\s*[=<>≤≥±]?\s*\d)|"
                        r"(?<!\w)(?:L|W|H|R|M|D)(?=\s*[=<>≤≥±]?\s*\d)|[ΦØ⌀]")
    remainder = labels.sub("", line)
    if re.fullmatch(r"[\d\s.,+\-–−±~≤≥<>=:：/×xX*()'\"′″°%寸米厘纳]+", remainder):
        return True
    return False


def suppress_drawing_dimensions(units: list[dict[str, str]]) -> list[dict[str, str]]:
    """Exclude dimension callouts only on pages explicitly reviewed as dimension drawings."""
    pages = {unit["locator"].removesuffix(" drawing"): unit
             for unit in units if unit.get("dimension_drawing")}
    if not pages:
        return units
    result = []
    for unit in units:
        locator = unit["locator"]
        # Word documents carry their pictures as "image N"; a drawing there is filtered the same way.
        page = re.match(r"^((?:page|slide|image) \d+)(?: |$)", locator)
        if unit.get("dimension_drawing") or page is None or page.group(1) not in pages:
            result.append(unit)
            continue
        # The drawing is an image asset; its model-generated description must not
        # reintroduce hole counts, dimensions or inferred geometry into prose.
        if unit.get("figure_description"):
            drawing = pages[page.group(1)]
            index = int(locator.rsplit(" ", 1)[-1])
            selected = drawing.get("drawing_figure_indexes")
            if selected is None or index in selected:
                continue
            result.append(unit)
            continue
        # Human full-page supplements are independent evidence, not OCR drafts.
        if unit.get("reviewed_supplement") or unit.get("engine") == "reviewed":
            result.append(unit)
            continue
        original_lines = unit["text"].splitlines()
        page_number = page.group(1).rsplit(" ", 1)[-1]
        lines = [line for index, line in enumerate(original_lines)
                 if (index >= len(original_lines) - 3 and line.strip() == page_number)
                 or not is_drawing_dimension_line(line)]
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


TITLE_CATEGORY_PREFIX = re.compile(r"^(?:产品资料|产品画册|规格书|说明书|参数表|图纸|产品|报价单)-")
GENERIC_MODEL_STEMS = {"目录", "画册", "产品资料", "说明书", "规格书", "参数表", "图纸", "产品"}


def infer_model_from_title(title: str) -> str:
    """Fallback product/model label from a single-product document title when a drawing lacks one."""
    stem = TITLE_CATEGORY_PREFIX.sub("", (title or "").strip()).strip()
    if not stem or stem in GENERIC_MODEL_STEMS:
        return ""
    if re.search(r"[A-Za-z0-9]{2,}|\d+\s*寸", stem):
        return stem
    return ""
