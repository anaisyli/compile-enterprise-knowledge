#!/usr/bin/env python3
"""Keep what a rebuild needs inside the vault, so only the package and the vault are kept.

Everything that cannot be recomputed from the package lives here: the config, review
decisions, ledger numbers, AI-written knowledge pages, MinerU text, and the website
snapshot. save copies it into the vault at publish; restore copies it back into a run
folder, so a deleted run folder can be rebuilt. When both sides have a copy, the newer one
wins and the other is reported, never silently overwritten.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, load_jsonl, safe_name, write_text


STATE = "05_数据与审核/70_构建状态"
MINERU = "02_源资料/20_MinerU按需提取"
WEBSITE = "02_源资料/30_官网快照"
FILES = [
    "kb-config.json", "run-info.json", "review-decisions.json", "governance-ids.json", "client-notes.jsonl",
    "cell-review/decisions.jsonl", "cell-review/sheet-columns.json", "cell-review/t3-audit.json",
    "page-check/results.json", "sample-decisions.json", "reviewed-pages.json",
    # Picture names stay once given, and the AI's picture names are not redone.
    "picture-names.json", "picture-names/answers.json",
]


def pairs(build: Path, vault: Path) -> list[tuple[Path, Path]]:
    """(file in the run folder, file in the vault) for every piece of state that exists on either side."""
    found = [(build / name, vault / STATE / name) for name in FILES]
    # The checked transcript, its exact page image and the machine draft travel together.
    for directory in ("reviewed-page-images", "mineru-drafts"):
        for root in (build / directory, vault / STATE / directory):
            for item in root.rglob("*") if root.is_dir() else []:
                if item.is_file():
                    relative = item.relative_to(root)
                    found.append((build / directory / relative, vault / STATE / directory / relative))
    for root in (build / "curation", vault / STATE / "curation"):
        for page in root.glob("*/page.md") if root.is_dir() else []:
            slug = page.parent.name
            found.append((build / "curation" / slug / "page.md", vault / STATE / "curation" / slug / "page.md"))
    for directory, target in (("mineru-units", MINERU), ("website/units", f"{WEBSITE}/units")):
        for root in (build / directory, vault / target):
            for item in root.glob("*.json") if root.is_dir() else []:
                found.append((build / directory / item.name, vault / target / item.name))
    found.append((build / "website" / "fetch-report.json", vault / WEBSITE / "fetch-report.json"))
    found.append((build / "website" / "pictures.json", vault / WEBSITE / "pictures.json"))
    return sorted(set(found))


def merge_pages(source: Path, target: Path) -> bool:
    """The website snapshot only grows: a failed or partial crawl must not wipe pages read before."""
    if not source.is_file():
        return False
    pages = {p["page_id"]: p for p in (load_json(target) if target.is_file() else [])}
    for page in load_json(source):
        known = pages.get(page["page_id"])
        if not known or page.get("fetched_at", "") >= known.get("fetched_at", ""):
            pages[page["page_id"]] = page
    merged = json.dumps(sorted(pages.values(), key=lambda p: p["url"]), ensure_ascii=False, indent=2) + "\n"
    if target.is_file() and target.read_text(encoding="utf-8") == merged:
        return False
    write_text(target, merged)
    return True


def same(left: Path, right: Path) -> bool:
    return left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes()


def copy_newer(source: Path, target: Path, kept: list[str]) -> bool:
    if not source.is_file() or same(source, target):
        return False
    if target.is_file() and target.stat().st_mtime > source.stat().st_mtime:
        kept.append(str(target))  # Someone changed the other copy later; keep theirs.
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def save(run: Path, vault: Path) -> dict[str, Any]:
    build = run / BUILD_DIRECTORY
    kept: list[str] = []
    copied = sum(copy_newer(b, v, kept) for b, v in pairs(build, vault))
    copied += merge_pages(build / "website" / "pages.json", vault / WEBSITE / "pages.json")
    write_mineru_pages(build, vault)
    return {"saved": copied, "kept_newer_in_vault": kept}


def restore(run: Path, vault: Path) -> dict[str, Any]:
    build = run / BUILD_DIRECTORY
    build.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    copied = sum(copy_newer(v, b, kept) for b, v in pairs(build, vault))
    copied += merge_pages(vault / WEBSITE / "pages.json", build / "website" / "pages.json")
    # MinerU results are also the upload cache: restoring them means no second upload.
    cache = build / "mineru-cache"
    for item in (build / "mineru-units").glob("*.json") if (build / "mineru-units").is_dir() else []:
        record = load_json(item)
        # Only MinerU's own reading is a cache; pages the model read must not stop a real upload.
        mineru = [unit for unit in record.get("units", []) if unit.get("engine") != "ai"]
        if record.get("source_hash") and record.get("source") != "ai" and mineru:
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / f"{record['source_hash'][:32]}.json"
            if not target.is_file():
                target.write_text(json.dumps(mineru, ensure_ascii=False), encoding="utf-8")
    return {"restored": copied, "kept_newer_in_run": kept}


def write_mineru_pages(build: Path, vault: Path) -> None:
    """A readable page next to each MinerU data file, named after the source document."""
    titles = {d["item_id"]: d["title"] for d in load_jsonl(build / "docs.jsonl")}
    for item in sorted((vault / MINERU).glob("*.json")) if (vault / MINERU).is_dir() else []:
        record = load_json(item)
        title = titles.get(record.get("item_id", ""), item.stem)
        lines = [f"# MinerU 识别稿：{title}", "", f"- 资料编号：`{record.get('item_id', '')}`",
                 f"- 识别日期：{record.get('extracted_at', '')}", f"- 页数：{len(record.get('units', []))}",
                 "- 说明：这是机器识别的文字，只用来补原文字层没有的内容；对外使用前看原件核对。", ""]
        for unit in record.get("units", []):
            by_ai = "（AI 看图转写，待核对）" if unit.get("engine") == "ai" else ""
            lines.extend([f"## {unit['locator'].replace(' ocr', '').replace('page', '第') + (' 页' if unit['locator'].startswith('page') else '')}{by_ai}",
                          "", unit["text"] or "（这页没有文字）", ""])
        write_text(vault / MINERU / f"{safe_name('MinerU-' + title)}.md", "\n".join(lines))
