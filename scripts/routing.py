#!/usr/bin/env python3
"""Package item routing, document classification, format pairing, drawing PDFs, and media collection.

Extracted from extract_blocks.py so routing and media rules live apart from block/table extraction.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from kb_common import (
    file_extension,
    long_path,
    normalize_file_decisions,
    normalize_space,
    source_stem,
)


THIRD_PARTY_TYPES = {"competitor"}
OPERATIONS_TYPES = {"seo_ops"}
EXCLUDED_COPY_STATUS = {"not_copied_restricted", "withheld_by_client", "copy_failed", "missing_decision"}

TITLE_NOISE = [re.compile(p) for p in (r"\s*\(\d+\)$", r"[_\s-]*\d{8,}$", r"[\s_-]*副本$", r"^复制[_\s-]*")]
FOLDERS = {"T3": "产品表", "T4": "业务要点"}
GENERIC_STEM = re.compile(r"(?i)^(?:页面文本|页面|文本|新建文本文档|新建\s*microsoft\s*\w+\s*文档|未命名|untitled|text|index|readme|content|page)$")
SOURCE_FORMATS = {".docx", ".doc", ".pptx", ".ppt"}
PDF_FOLDER = re.compile(r"(?i)^(?:pdf|pdf版|pdf格式|pdf文件)$")

COMPANY_NAME = re.compile(r"(?i)公司|简介|profile|about")
DOC_CATEGORIES = [
    ("内部资料", re.compile(r"(?i)培训|考试|验货|模板|training|\bexam\b|inspection")),
    ("说明书", re.compile(r"(?i)说明书|使用手册|用户手册|安装说明|安装指南|安装手册|manual|user\s*guide|instructions?\b")),
    ("规格书", re.compile(r"(?i)规格|参数|spec|data\s*sheet|(?<![大小])尺寸|重量")),
    ("解决方案", re.compile(r"(?i)solutions?\b|解决方案|\bOEM\b|\bODM\b|定制服务")),
    ("产品画册", re.compile(r"(?i)画册|catalog(?:ue)?\b|brochure|宣传册")),
]
COMPANY_DOC = re.compile(r"(?i)公司|简介|profile|about|关于我们|品牌故事|brand\s*story|contact|联系我们|news|新闻|^首页$|^home$|^why")
WEBSITE_FOLDER = re.compile(r"(?i)网页|官网|website|web\s*page")
PRODUCT_FOLDER = re.compile(r"(?i)^(?:产品|产品资料|products?)$")

PICTURE_FILES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DESIGN_FILES = {".psd", ".heic", ".heif", ".tif", ".tiff", ".svg", ".eps"}
VIDEO_FILES = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".flv", ".webm"}
AUDIO_FILES = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

DRAWING_NAME = re.compile(r"(?i)尺寸|六视图|三视图|视图|装配|图纸|效果图|外观图|结构图|\bcad\b|dimension|drawing|render")
NOT_DRAWING = re.compile(r"(?i)说明书|手册|技术参数|参数表|manual|guide|datasheet|brochure|画册|catalog")
DRAWING_PAGES = 3
DRAWING_MAX_PAGES = 6
DRAWING_PREVIEW_EDGE = 3000


def extension_of(name: str) -> str:
    return file_extension(name)


def media_kind(asset: dict[str, Any]) -> str | None:
    """image / video / audio for a file that is material to look at, None for a document to read."""
    extension = extension_of(asset.get("display_name") or asset.get("path") or "")
    if asset.get("asset_kind") in {"video", "audio"}:
        return asset["asset_kind"]
    if extension in VIDEO_FILES:
        return "video"
    if extension in AUDIO_FILES:
        return "audio"
    if asset.get("asset_kind") == "image" or extension in PICTURE_FILES | DESIGN_FILES:
        return "image"
    return None


def route_asset(asset: dict[str, Any], answers: dict[str, str], confirmed_client: bool = False) -> dict[str, str]:
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
    if resource in OPERATIONS_TYPES and not (confirmed_client and answer == "keep" and decision == "D"):
        return {"route": "skip", "reason": "writing_operations_material"}
    if decision == "E":
        return {"route": "skip", "reason": "excluded"}
    if decision == "D":
        if answer in {"keep", "keep_internal"}:
            kept_by = "client" if answer == "keep" else "internal"
            if media_kind(asset):
                return {"route": "media", "reason": "media_kept", "kept_by": kept_by}
            official = resource == "marketing" and extension in {".pdf", ".pptx"}
            reason = "resolved_by_answer" if answer == "keep" else "kept_internally"
            return {"route": "kb", "reason": reason, "tier": "T1" if official else "T2"}
        if answer == "drop_internal":
            return {"route": "skip", "reason": "dropped_internally"}
        return {"route": "hold", "reason": "unresolved_item"}
    if decision == "B":
        official = resource == "marketing" and extension in {".pdf", ".pptx"}
        return {"route": "kb", "reason": "cleaned_from_b", "tier": "T1" if official else "T2"}
    if decision == "C":
        return {"route": "points", "reason": "knowledge_points", "tier": "T4"}
    if decision == "A":
        kind = asset.get("asset_kind")
        if kind in {"video", "audio"} or (kind == "image" and asset.get("folder_rule")):
            if not asset.get("usable_as_fact") and answer != "keep":
                return {"route": "media", "reason": "media_awaiting_client"}
            return {"route": "media", "reason": "media_kept"}
        if kind == "image":
            return {"route": "hold", "reason": "image_needs_review"}
        official = resource == "marketing" and extension in {".pdf", ".pptx"}
        tier = "T1" if official else "T2"
        if not asset.get("usable_as_fact") and answer != "keep":
            return {"route": "kb", "reason": "awaiting_client", "tier": tier, "pending_all": True}
        return {"route": "kb", "reason": "retained", "tier": tier}
    return {"route": "hold", "reason": "unknown_decision"}


def ask_client_route(asset: dict[str, Any], why: Any) -> dict[str, Any]:
    """A file we send to the client: pictures and videos wait as material, documents as 🕒 pages."""
    reason = why if isinstance(why, str) and why.strip() else "内部看过后，决定请客户确认能否使用"
    if media_kind(asset):
        return {"route": "media", "reason": "media_awaiting_client", "ask_reason": reason}
    extension = extension_of(asset.get("display_name") or "")
    official = asset.get("resource_type") == "marketing" and extension in {".pdf", ".pptx"}
    return {"route": "kb", "reason": "awaiting_client", "tier": "T1" if official else "T2", "pending_all": True,
            "ask_reason": reason}


def configured_route(asset: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Apply explicit user exclusions before cached extraction can route an item back in."""
    normalize_file_decisions(config)
    item_id = asset["item_id"]
    if item_id in config.get("excluded_items", {}):
        return {"route": "skip", "reason": "user_excluded"}
    asked = config.get("ask_client") or {}
    if item_id in asked:
        return ask_client_route(asset, asked[item_id])
    confirmed_media = config.get("confirmed_media") or {}
    confirmed_ids = set(confirmed_media) if isinstance(confirmed_media, (dict, list)) else set()
    if item_id in confirmed_ids and media_kind(asset):
        meta = confirmed_media.get(item_id) if isinstance(confirmed_media, dict) else {}
        return {"route": "media", "reason": "media_kept", **({"confirmed_media": meta} if isinstance(meta, dict) and meta else {})}
    answers = config.get("client_answers", {})
    override = (config.get("route_overrides") or {}).get(item_id)
    if (isinstance(override, dict) and override.get("route")
            and not (override.get("reason") in {"awaiting_client", "media_awaiting_client"} and item_id in answers)):
        result = {k: v for k, v in override.items() if k != "asset"}
        result.setdefault("reason", "explicit_build_override")
        return result
    if asset.get("decision") == "D" and item_id not in answers:
        if item_id in set(config.get("d_drop", [])):
            answers = {**answers, item_id: "drop_internal"}
        elif item_id in set(config.get("d_keep", [])):
            answers = {**answers, item_id: "keep_internal"}
    confirmation = (config.get("client_answer_confirmations") or {}).get(item_id) or {}
    route = route_asset(asset, answers, confirmed_client=confirmation.get("decision") == "keep" and bool(confirmation.get("cus_id")))
    if item_id in config.get("no_ocr_items", {}) and route.get("reason") == "image_needs_review":
        return {"route": "media", "reason": "media_kept"}
    return route


def clean_name(name: str) -> str:
    for pattern in TITLE_NOISE:
        name = pattern.sub("", name)
    return normalize_space(name.replace("_", " ")) or name


def format_pairs(assets: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """PDF item_id -> the Word or PowerPoint files of the same name in the same folder."""
    by_place: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item_id, asset in assets.items():
        path = Path(asset.get("source_relpath") or asset.get("path") or asset.get("display_name") or "")
        by_place[(path.parent.as_posix().casefold(), normalize_space(path.stem).casefold())].append(item_id)
    by_parent: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item_id, asset in assets.items():
        path = Path(asset.get("source_relpath") or asset.get("path") or asset.get("display_name") or "")
        by_parent[(path.parent.parent.as_posix().casefold(), normalize_space(path.stem).casefold())].append(item_id)
    for members in by_parent.values():
        pdfs = [i for i in members if PDF_FOLDER.match(Path(assets[i].get("source_relpath") or assets[i].get("path") or "").parent.name)]
        if pdfs and len(members) > len(pdfs):
            for pdf in pdfs:
                for other in members:
                    if other not in pdfs:
                        path = Path(assets[other].get("source_relpath") or assets[other].get("path") or "")
                        place = (path.parent.as_posix().casefold(), normalize_space(path.stem).casefold())
                        if pdf not in by_place[place]:
                            by_place[place].append(pdf)
    pairs: dict[str, list[str]] = {}
    for members in by_place.values():
        suffix = {i: extension_of(assets[i].get("display_name") or assets[i].get("path") or "") for i in members}
        sources = sorted(i for i in members if suffix[i] in SOURCE_FORMATS)
        if sources:
            pairs.update({i: sources for i in members if suffix[i] == ".pdf"})
    return pairs


def other_formats(routes: dict[str, dict[str, Any]], readable: Callable[[str], bool]) -> dict[str, str]:
    """PDF item_id -> the Word or PowerPoint file it is a copy of."""
    candidates = {i: r["asset"] for i, r in routes.items() if r["route"] == "kb" or r["reason"] == "awaiting_sample"}
    twins = {}
    for pdf, sources in format_pairs(candidates).items():
        source = next((i for i in sources if routes[i]["route"] == "kb" and readable(i)), None)
        if source:
            twins[pdf] = source
    return twins


def is_catalog(asset: dict[str, Any], tier: str, overrides: dict[str, str]) -> bool:
    """Whether a file is a catalog for samples and picture naming."""
    if asset["item_id"] in overrides:
        return overrides[asset["item_id"]].split("-", 1)[0] == "产品画册"
    if tier != "T1" or asset.get("asset_kind") == "image" or extension_of(asset.get("display_name") or "") in PICTURE_FILES:
        return False
    name = clean_name(source_stem(asset.get("display_name") or asset["item_id"]))
    if GENERIC_STEM.match(name):
        name = f"{name}-{clean_name(Path(asset.get('source_relpath') or asset.get('path') or '').parent.name)}"
    return not COMPANY_NAME.search(name)


def doc_category(name: str, relpath: str, tier: str) -> str:
    """The kind of document, from its name and folders."""
    folders = [p for p in relpath.replace("\\", "/").split("/")[:-1] if p]
    parent = folders[-1] if folders else ""
    for category, pattern in DOC_CATEGORIES:
        if pattern.search(name) or (category == "内部资料" and pattern.search(parent)):
            return category
    if COMPANY_DOC.search(name):
        return "公司介绍"
    if any(WEBSITE_FOLDER.search(p) for p in folders):
        return "官网页面"
    if tier == "T1":
        return "产品画册"
    if any(PRODUCT_FOLDER.match(p) for p in folders):
        return "产品资料"
    return "其他"


def package_title(asset: dict[str, Any], tier: str, overrides: dict[str, str]) -> tuple[str, str]:
    """Return (title, folder). Titles read 类别-名称 so a reader knows what a file is."""
    if asset["item_id"] in overrides:
        title = overrides[asset["item_id"]]
        return title, title.split("-", 1)[0] if "-" in title else "其他"
    if tier == "T3":
        return f"产品表-{asset['source_hash'][:8]}", FOLDERS["T3"]
    relpath = asset.get("source_relpath") or asset.get("path") or ""
    name = clean_name(source_stem(asset.get("display_name") or asset["item_id"]))
    if GENERIC_STEM.match(name):
        folder = clean_name(Path(relpath).parent.name)
        name = folder or name
    if asset.get("asset_kind") == "image" or extension_of(asset.get("display_name") or "") in PICTURE_FILES:
        return f"图片-{name}", "图片资料"
    category = doc_category(name, relpath, tier)
    return f"{category}-{name}", category


def module_for(asset: dict[str, Any], tier: str, folder: str = "") -> str:
    if tier == "T3":
        return "product"
    if asset.get("resource_type") == "business_scenario" or folder == "解决方案":
        return "solution"
    if folder == "公司介绍":
        return "company"
    return "product"


def video_cover(source: Path, assets: Path, key: str) -> tuple[str, str]:
    """(cover picture id, duration m:ss) of a video, when ffmpeg is installed; ("", "") otherwise."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg:
        return "", ""
    name = f"img-{hashlib.sha256(f'{key}|cover'.encode()).hexdigest()[:16]}.jpg"
    duration = ""
    try:
        if not (assets / name).exists():
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", "1", "-i", long_path(source), "-frames:v", "1",
                            "-vf", "scale=480:-2", str(assets / name)], capture_output=True, timeout=60)
        if ffprobe:
            seconds = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", long_path(source)],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
            if seconds:
                duration = f"{int(float(seconds)) // 60}:{int(float(seconds)) % 60:02d}"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return (name if (assets / name).is_file() else ""), duration


def drawing_kind(asset: dict[str, Any], units: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """"尺寸图" or "效果图" for a PDF that is a drawing, "" for one to read."""
    item_id = asset["item_id"]
    path = asset.get("source_relpath") or asset.get("path") or asset.get("display_name") or ""
    if extension_of(asset.get("display_name") or path) != ".pdf" or item_id in set(config.get("not_drawing_items", [])):
        return ""
    named = " ".join(Path(path).parts[-3:])
    kind = "效果图" if re.search(r"效果|render", named, re.I) else "尺寸图"
    if item_id in set(config.get("drawing_items", [])):
        return kind
    if NOT_DRAWING.search(named):
        return ""
    pages = {u["locator"].split()[1] for u in units if u.get("locator", "").startswith("page ")}
    if len(pages) > DRAWING_MAX_PAGES:
        return ""
    if DRAWING_NAME.search(named):
        return kind
    text = "".join(u.get("text", "") for u in units)
    words = len(re.findall(r"[一-鿿]", text)) + len(re.findall(r"[A-Za-z]{3,}", text))
    numbers = len(re.findall(r"\d+(?:\.\d+)?", text))
    return "尺寸图" if pages and len(pages) <= DRAWING_PAGES and numbers >= 10 and words < numbers else ""


def drawing_pictures(source: Path, assets: Path, source_hash: str) -> list[str]:
    """Each page of a drawing as a PNG in the build's assets; [] when the PDF cannot be drawn."""
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        return []
    names = []
    try:
        with pymupdf.open(long_path(source)) as pdf:
            for number, page in enumerate(pdf, start=1):
                if number > DRAWING_MAX_PAGES:
                    break
                name = f"img-{source_hash[:16]}-p{number}.png"
                if not (assets / name).exists():
                    scale = min(150 / 72, (DRAWING_PREVIEW_EDGE - 1) / max(page.rect.width, page.rect.height))
                    page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).save(long_path(assets / name))
                names.append(name)
    except Exception:  # noqa: BLE001 - a damaged PDF stays a file to open, not a crash.
        return []
    return names


def collect_media(routes: dict[str, Any], package_of: dict[str, Path], build: Path, max_bytes: int) -> list[dict[str, Any]]:
    """Pictures and videos screening kept by folder.

    Standalone images, videos, audio, and design files stay in the preparation package and are
    indexed in 00_素材索引.md / 00_素材清单.csv instead of being copied into 90_素材; only rendered
    PDF drawings (and extracted document/website images) are stored inside 90_素材.
    """
    assets = build / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    media = []
    for item_id, route in sorted(routes.items()):
        if route["route"] != "media":
            continue
        asset = route["asset"]
        entry = {
            "item_id": item_id,
            "kind": media_kind(asset) or asset.get("asset_kind"),
            "relpath": asset.get("source_relpath") or asset.get("path") or "",
            "package": str(package_of[item_id]),
            "package_path": asset.get("path") or "",
            "bytes": asset.get("byte_size") or 0,
            "status": route["reason"],
            "review_group": asset.get("review_group") or "",
            "resource_type": asset.get("resource_type"),
            "picture_kind": route.get("picture_kind"),
            "drawing_model": route.get("drawing_model"),
            "drawing_model_confirmed": route.get("drawing_model_confirmed"),
        }
        source = package_of[item_id] / (asset.get("path") or "")
        if route["reason"] == "drawing":
            pages = drawing_pictures(source, assets, asset.get("source_hash", "")) if asset.get("path") else []
            media.extend({**entry, "kind": "image", "status": "media_kept", "drawing": True,
                          "picture_kind": route.get("picture_kind", "尺寸图"), "id": name, "page": number}
                         for number, name in enumerate(pages, start=1))
            if not pages:
                media.append({**entry, "kind": "document", "status": "drawing_unrendered"})
            continue
        media.append(entry)
    return media
