#!/usr/bin/env python3
"""Sample first, then the batch: each kind of file is extracted on one example before the rest.

A rule that is wrong for a kind of file is wrong for every file of that kind. So on a first
build only one file of each kind is read: a person looks at how it came out, beside the
original, and confirms the kind. Only confirmed kinds are read in full on the next build.

The gate is decided before anything is read, from the package's manifest: which kind each
file is, which one is its sample, and which files wait. It is written to sample-gate.json;
the text extraction and the block extraction both follow it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kb_common import file_extension, load_json, write_json


DECISIONS = "sample-decisions.json"
GATE = "sample-gate.json"
PLAN_PAGE = "02_源资料/处理计划与样例.md"
CONFIRMED = "已确认"
PENDING = "待确认"
# key -> (name shown, how it is extracted). One row per kind of file on the plan page.
TYPES = {
    "catalog_pdf": ("产品画册 PDF", "PyMuPDF 按版面分栏读文字，款号跟着它那一栏；页里的表格整理成 Markdown 表；每页附原页截图；"
                    "印在图里的字走 MinerU 或 AI 读图；印的产品照片收进素材库"),
    "pdf": ("其他 PDF（公司简介、规格书等）", "有文字的页照读，表格整理成 Markdown 表；每页附原页截图；没文字的页走 MinerU 或 AI 读图"),
    "ppt": ("PPT", "按文本框位置分栏读，演讲者备注单独一节；每张幻灯片附截图（要 LibreOffice）；只有图片的页走 MinerU 或 AI 读图"),
    "word": ("Word 文档", "正文段落和表格按原顺序读，表格整理成 Markdown 表，编号小标题当分节；嵌入的图片对照原图核对"),
    "spreadsheet": ("Excel / CSV 表格", "每张可见工作表读成表：有产品表头（款号、品名、面料……）的按行清理价格、数量、客户信息，"
                    "算产品表或报价单，合进《产品表汇总》；没有产品表头的整张保留成 Markdown 表；隐藏工作表不读"),
    "text": ("文本（txt、md）", "按原文分段读，不改写"),
    "image": ("单张图片", "MinerU 或 AI 读图，读出的字入库并标 ⚠"),
    "points": ("邮件知识点", "初筛整理的知识点汇总成一份《业务要点》，状态“待审”"),
    "website": ("官网页面", "按网址分大类；客户案例页等客户确认；分类列表页、重复页跳过"),
    "other": ("其他文件", "按文字读取"),
}
# Kinds confirmed under the names an earlier version used.
LEGACY = {"spreadsheet": ("quotation", "product_table")}
# How a kind of paged file is read, chosen by a person after seeing its sample.
METHODS = {
    "text": "文字层",
    "mineru": "MinerU 初稿 + AI 复核",
    "ai": "AI 看图复核",
}
METHOD_HELP = {
    "text": "读文件里的文字层，图多字少、款号对不上的页和一成抽查对照原图看",
    "mineru": "整份上传 MinerU 识别出初稿（要批准上传），再把每页有图片的原页和初稿一起交给 AI 对照：补漏字、标错字、写款号对应",
    "ai": "不上传；每页有图片的原页都交给 AI 对照文字层：补漏字、标错字、写款号对应",
}
# Kinds with pages that can be pictured, sent to MinerU, and compared: the methods apply to them.
# A single picture has no text layer; it is always read by MinerU or the model, then looked at.
PAGED_KINDS = {"catalog_pdf", "pdf", "ppt"}
STRUCTURE_PREFIX = "结构:"
PICTURES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SPREADSHEETS = {".xlsx", ".xls", ".csv", ".tsv"}
TEXTS = {".txt", ".md", ".eml"}


def kind_of(name: str, folder: str = "", tier: str = "") -> str:
    """The kind of a file by what it is: its extension, and for a PDF whether it is a catalog."""
    suffix = file_extension(name)
    if suffix == ".pdf":
        return "catalog_pdf" if folder == "产品画册" else "pdf"
    if suffix in {".pptx", ".ppt"}:
        return "ppt"
    if suffix in {".docx", ".doc"}:
        return "word"
    if suffix in SPREADSHEETS or tier == "T3":
        return "spreadsheet"
    if suffix in PICTURES:
        return "image"
    if suffix in TEXTS:
        return "text"
    return "other"


def doc_type(doc: dict[str, Any]) -> str:
    if doc.get("origin") == "website":
        return "website"
    if doc.get("origin") == "points":
        return "points"
    # The catalog flag, not the folder: a kind confirmed once stays confirmed when folders are renamed.
    catalog = doc["catalog"] if "catalog" in doc else doc.get("folder") == "产品画册"
    return kind_of(doc.get("original_name") or "", "产品画册" if catalog else "", doc.get("tier", ""))


def load_gate(build: Path) -> dict[str, Any]:
    path = build / GATE
    return load_json(path) if path.is_file() else {}


def choose_samples(docs: list[dict[str, Any]], config: dict[str, Any], build: Path | None = None) -> dict[str, str]:
    """type -> doc_id: the gate's sample when there is one, else the fullest document; the config can name one."""
    chosen: dict[str, tuple[int, str, str]] = {}
    for doc in docs:
        kind = doc_type(doc)
        size = int(doc.get("pages") or 0) * 50 + int(doc.get("units") or 0)
        if kind not in chosen or (size, doc["title"]) > chosen[kind][:2]:
            chosen[kind] = (size, doc["title"], doc["doc_id"])
    picks = {kind: entry[2] for kind, entry in chosen.items()}
    by_item = {d["item_id"]: d["doc_id"] for d in docs}
    named = {kind: info.get("sample") for kind, info in (load_gate(build).get("kinds", {}) if build else {}).items()}
    for kind, item_id in {**named, **(config.get("samples") or {})}.items():
        if item_id in by_item:
            picks[kind] = by_item[item_id]
    return picks


def load_decisions(build: Path) -> dict[str, dict[str, Any]]:
    path = build / DECISIONS
    return load_json(path) if path.is_file() else {}


def save_decisions(build: Path, decisions: dict[str, dict[str, Any]]) -> None:
    write_json(build / DECISIONS, decisions)


def status_of(key: str, decisions: dict[str, dict[str, Any]]) -> str:
    for name in (key, *LEGACY.get(key, ())):
        status = (decisions.get(name) or {}).get("status")
        if status == CONFIRMED:
            return status
    return (decisions.get(key) or {}).get("status") or PENDING


def parse_method(text: str) -> str | None:
    """What a person wrote in the 处理方式 cell, in their own words."""
    folded = (text or "").casefold()
    if "mineru" in folded:
        return "mineru"
    if "ai" in folded or "看图" in folded:
        return "ai"
    if "文字层" in folded:
        return "text"
    return None


def method_of(kind: str, decisions: dict[str, dict[str, Any]]) -> str:
    """The way a kind is read: what a person chose, else the text layer."""
    for name in (kind, *LEGACY.get(kind, ())):
        method = (decisions.get(name) or {}).get("method")
        if method in METHODS:
            return method
    return "text"


def is_confirmed(key: str, decisions: dict[str, dict[str, Any]], config: dict[str, Any]) -> bool:
    """With sample review switched off (tests, a rebuild of a known project) everything counts as confirmed."""
    return config.get("sample_review", True) is False or status_of(key, decisions) == CONFIRMED


def gate(entries: list[dict[str, Any]], config: dict[str, Any], decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """entries: {"item_id", "kind", "bytes", "folder"} for every file the build would read.

    Returns which files may be read now (all of a confirmed kind, the sample of any other) and,
    per kind, the sample and how many wait. The sample is the file of middling size: the
    biggest is slow to look at, the smallest says little. The config can name one."""
    if config.get("sample_review", True) is False:
        return {}
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_kind.setdefault(entry["kind"], []).append(entry)
    allowed: list[str] = []
    kinds: dict[str, Any] = {}
    named = config.get("samples") or {}
    for kind, members in sorted(by_kind.items()):
        members.sort(key=lambda e: (e["bytes"], e["item_id"]))
        pool = [e for e in members if not e.get("twin")] or members  # A PDF copy of a Word file shows little.
        sample = next((e for e in members if e["item_id"] == named.get(kind)), pool[len(pool) // 2])
        confirmed = is_confirmed(kind, decisions, config)
        allowed += [e["item_id"] for e in members] if confirmed else [sample["item_id"]]
        folders: dict[str, int] = {}
        for entry in members:
            folders[entry["folder"]] = folders.get(entry["folder"], 0) + 1
        kinds[kind] = {"total": len(members), "sample": sample["item_id"], "confirmed": confirmed,
                       "waiting": 0 if confirmed else len(members) - 1, "folders": folders}
    return {"allowed": sorted(allowed), "kinds": kinds}


def save_gate(build: Path, value: dict[str, Any]) -> None:
    path = build / GATE
    if value:
        write_json(path, value)
    elif path.is_file():
        path.unlink()  # Review switched off: nothing waits.
