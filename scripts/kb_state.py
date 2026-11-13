#!/usr/bin/env python3
"""Keep what a rebuild needs inside the vault, so only the package and the vault are kept.

Everything that cannot be recomputed from the package lives here: the config, review
decisions, ledger numbers, AI-written knowledge pages, MinerU text, and the website
snapshot. save copies it into the vault at publish; restore copies it back into a run
folder, so a deleted run folder can be rebuilt. When both sides have a copy, settings and
decisions (JSON objects such as kb-config.json) are merged entry by entry; other files go by
the newer copy, and the other is reported, never silently overwritten.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, write_text


STATE = "05_数据与审核/70_构建状态"
MINERU = f"{STATE}/mineru-units"
# Reducto's record carries the file hash and the pages it read, so restoring it is the upload cache too.
REDUCTO = f"{STATE}/reducto-units"
# Older vaults kept these two inside the source-material folders; they are moved on the next save or restore.
LEGACY = {"02_源资料/20_MinerU按需提取": MINERU, "02_源资料/21_Reducto按需提取": REDUCTO}
WEBSITE = "02_源资料/30_官网快照"
FILES = [
    "kb-config.json", "run-info.json", "review-decisions.json", "governance-ids.json", "client-notes.jsonl",
    "cell-review/decisions.jsonl", "cell-review/sheet-columns.json", "cell-review/t3-audit.json",
    "page-check/results.json", "sample-decisions.json", "reviewed-pages.json",
    # Picture names stay once given, and the AI's picture names are not redone.
    "picture-names.json", "picture-names/answers.json",
    # Home-page candidates the model picked, and which ones a person confirmed.
    "site-home.json",
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
    for directory, target in (("mineru-units", MINERU), ("reducto-units", REDUCTO), ("website/units", f"{WEBSITE}/units")):
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


def json_object(path: Path) -> dict[str, Any] | None:
    if path.suffix != ".json" or not path.is_file():
        return None
    try:
        value = load_json(path)
    except (ValueError, OSError):
        return None
    return value if isinstance(value, dict) else None


def merge_objects(source: Path, target: Path, kept: list[str]) -> bool:
    """Settings and decisions are merged entry by entry, not file by file.

    A whole-file "newer wins" lost a product list a person wrote into the vault, because a run
    folder's older config was saved later. Now an entry only one side has is kept; where both
    have it and differ, the newer file's value is used and the other is reported. Only the
    target is written: restoring must not touch the published vault, saving must not touch
    the run folder."""
    left, right = json_object(source), json_object(target)
    if left is None or right is None:
        return False
    newer, older = (left, right) if source.stat().st_mtime >= target.stat().st_mtime else (right, left)
    merged = {**older, **newer}
    lost = sorted(k for k in older if k in newer and older[k] != newer[k])
    if lost:
        kept.append(f"{target}（{'、'.join(lost[:5])} 两边不一样，用较新的那份）")
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    if json_object(target) == merged:
        return False
    write_text(target, text)
    return True


def copy_newer(source: Path, target: Path, kept: list[str]) -> bool:
    if not source.is_file() or same(source, target):
        return False
    if json_object(source) is not None and json_object(target) is not None:
        return merge_objects(source, target, kept)
    if target.is_file() and target.stat().st_mtime > source.stat().st_mtime:
        kept.append(str(target))  # Someone changed the other copy later; keep theirs.
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def migrate_legacy(vault: Path) -> None:
    for old, new in LEGACY.items():
        source = vault / old
        if not source.is_dir():
            continue
        for item in source.glob("*.json"):
            target = vault / new / item.name
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), target)
        for item in source.glob("*.md"):
            item.unlink()
        try:
            source.rmdir()
        except OSError:
            pass


def save(run: Path, vault: Path) -> dict[str, Any]:
    migrate_legacy(vault)
    build = run / BUILD_DIRECTORY
    kept: list[str] = []
    copied = sum(copy_newer(b, v, kept) for b, v in pairs(build, vault))
    copied += merge_pages(build / "website" / "pages.json", vault / WEBSITE / "pages.json")
    return {"saved": copied, "kept_newer_in_vault": kept}


def restore(run: Path, vault: Path) -> dict[str, Any]:
    migrate_legacy(vault)
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
