#!/usr/bin/env python3
"""Sample first, then the batch: each kind of material is shown on one example before the rest.

A rule that is wrong for a kind of file is wrong for every file of that kind. Shown one
processed example per kind, a person catches it on the first file, not after the whole
package. Until a kind is confirmed, the costly work on it (looking at every page) waits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kb_common import load_json, write_json


DECISIONS = "sample-decisions.json"
PLAN_PAGE = "02_源资料/处理计划与样例.md"
CONFIRMED = "已确认"
PENDING = "待确认"
# key -> (name shown, what is done with it)
TYPES = {
    "catalog_pdf": ("产品画册 PDF", "按版面分栏读，款号跟着它那一栏；表格整理成 Markdown 表；附原页图；图多字少、款号对不上的页对照原图核对"),
    "pdf": ("其他 PDF（公司简介等）", "有文字的页照读，表格整理成 Markdown 表；没文字的页走 MinerU 或 AI 读图；图多字少的页对照原图核对"),
    "ppt": ("PPT", "按文本框位置分栏读，演讲者备注单独一节；只有图片的页走 MinerU 或 AI 读图"),
    "word": ("Word 文档", "正文和表格按顺序读，表格整理成 Markdown 表；嵌入的图片对照原图核对"),
    "reference": ("参考整理稿", "入库可检索，状态“参考稿”，整理页引用必须带 ⚠，不当公司事实"),
    "quotation": ("报价单", "只留品名、描述、面料、尺码、图片，删价格、数量、客户信息和报价条件；序号不当款号；合进《产品表汇总》"),
    "product_table": ("产品清单表格", "按表头留产品字段，删价格、数量、客户信息；合进《产品表汇总》；逐格判断后才算有效"),
    "points": ("邮件知识点", "汇总成一份《业务要点》，状态“待审”"),
    "image": ("单张图片", "MinerU 或 AI 读图读出文字后入库，标 ⚠"),
    "website": ("官网页面", "按网址分大类；客户案例页等客户确认；分类列表页、重复页跳过"),
    "other": ("其他文件", "按文字读取"),
}
STRUCTURE_PREFIX = "结构:"


def doc_type(doc: dict[str, Any]) -> str:
    suffix = Path(doc.get("original_name") or "").suffix.casefold()
    if doc.get("origin") == "website":
        return "website"
    if doc.get("origin") == "points":
        return "points"
    if doc.get("role") == "reference":
        return "reference"
    if doc.get("tier") == "T3":
        return "quotation" if doc.get("kind") == "quotation" else "product_table"
    if suffix == ".pdf":
        return "catalog_pdf" if doc.get("folder") == "产品画册" else "pdf"
    if suffix in {".pptx", ".ppt"}:
        return "ppt"
    if suffix in {".docx", ".doc"}:
        return "word"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        return "image"
    return "other"


def choose_samples(docs: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, str]:
    """The fullest document of each kind, unless the config names one: type -> doc_id."""
    chosen: dict[str, tuple[int, str, str]] = {}
    for doc in docs:
        kind = doc_type(doc)
        size = int(doc.get("pages") or 0) * 50 + int(doc.get("units") or 0)
        if kind not in chosen or (size, doc["title"]) > chosen[kind][:2]:
            chosen[kind] = (size, doc["title"], doc["doc_id"])
    picks = {kind: entry[2] for kind, entry in chosen.items()}
    by_item = {d["item_id"]: d["doc_id"] for d in docs}
    for kind, item_id in (config.get("samples") or {}).items():
        if item_id in by_item:
            picks[kind] = by_item[item_id]
    return picks


def load_decisions(build: Path) -> dict[str, dict[str, Any]]:
    path = build / DECISIONS
    return load_json(path) if path.is_file() else {}


def save_decisions(build: Path, decisions: dict[str, dict[str, Any]]) -> None:
    write_json(build / DECISIONS, decisions)


def status_of(key: str, decisions: dict[str, dict[str, Any]]) -> str:
    return (decisions.get(key) or {}).get("status") or PENDING


def is_confirmed(key: str, decisions: dict[str, dict[str, Any]], config: dict[str, Any]) -> bool:
    """With sample review switched off (tests, a rebuild of a known project) everything counts as confirmed."""
    return config.get("sample_review", True) is False or status_of(key, decisions) == CONFIRMED
