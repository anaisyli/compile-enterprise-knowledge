#!/usr/bin/env python3
"""Keep what a rebuild needs inside the vault, so only the package and the vault are kept.

Everything that cannot be recomputed from the package lives here: the config, review
decisions, ledger numbers, AI-written knowledge pages, MinerU text, and the website
snapshot. save copies it into the vault at publish; restore copies it back into a run
folder, so a deleted run folder can be rebuilt. When both sides have a copy, settings and
decisions (JSON objects such as kb-config.json) are merged entry by entry — kb-config.json
three-way against the vault-base record, so a key deleted on purpose stays deleted; other
files go by the newer copy, and the other is reported, never silently overwritten.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kb_common import BUILD_DIRECTORY, load_json, write_text
from legacy_migrations import move_legacy_vault_dirs


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
    "page-check/results.json", "page-check/sampling.json", "sample-decisions.json", "reviewed-pages.json",
    # Picture names stay once given, and the AI's picture names are not redone.
    "picture-names.json", "picture-names/answers.json",
    # Which naming rules the picture names follow, and whether the one-time move to them was published.
    "picture-layout.json",
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
        # The page and its coverage ledger are the model's work; both are kept.
        for name in ("page.md", "ledger.jsonl"):
            for page in root.glob(f"*/{name}") if root.is_dir() else []:
                slug = page.parent.name
                found.append((build / "curation" / slug / name, vault / STATE / "curation" / slug / name))
    for directory, target in (("mineru-units", MINERU), ("reducto-units", REDUCTO), ("website/units", f"{WEBSITE}/units")):
        for root in (build / directory, vault / target):
            for item in root.glob("*.json") if root.is_dir() else []:
                found.append((build / directory / item.name, vault / target / item.name))
    # Original HTML is local evidence too: offline imports/reparses must remain reproducible
    # after the expendable run directory is removed.
    for root in (build / "website" / "raw", vault / WEBSITE / "raw"):
        for item in root.glob("*.html") if root.is_dir() else []:
            found.append((build / "website" / "raw" / item.name, vault / WEBSITE / "raw" / item.name))
    found.append((build / "website" / "fetch-report.json", vault / WEBSITE / "fetch-report.json"))
    found.append((build / "website" / "parse-report.json", vault / WEBSITE / "parse-report.json"))
    found.append((build / "website" / "pictures.json", vault / WEBSITE / "pictures.json"))
    return sorted(set(found))


def merge_pages(source: Path, target: Path) -> bool:
    """The website snapshot only grows: a failed or partial crawl must not wipe pages read before."""
    if not source.is_file():
        return False
    source_pages = load_json(source)
    source_ids = {page["page_id"] for page in source_pages}
    pages = {p["page_id"]: p for p in (load_json(target) if target.is_file() else [])}
    for page in source_pages:
        known = pages.get(page["page_id"])
        if not known or page.get("fetched_at", "") >= known.get("fetched_at", ""):
            pages[page["page_id"]] = page
    # Older crawls assigned separate ids to slash aliases. Keep the newest identity
    # (breaking ties in favour of source so merged ids beat stale target aliases).
    by_url: dict[str, dict[str, Any]] = {}
    for page in pages.values():
        parsed = urlsplit(page["url"])
        key = urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                         parsed.path.rstrip("/") or "/", parsed.query, ""))
        known = by_url.get(key)
        rank = (page.get("fetched_at", ""), page["page_id"] in source_ids)
        known_rank = (known.get("fetched_at", ""), known["page_id"] in source_ids) if known else None
        if not known or rank > known_rank:
            by_url[key] = page
    pages = {page["page_id"]: page for page in by_url.values()}
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


def config_base(build: Path) -> Path:
    """The run folder's kb-config.json as it was when last published: the merge's base."""
    return build / "vault-base" / "kb-config.json"


MISSING = object()


def merge_settings(run: dict[str, Any], vault: dict[str, Any], base: dict[str, Any] | None,
                   run_newer: bool) -> tuple[dict[str, Any], list[str], list[str]]:
    """(merged, conflicting keys, removed keys). Without a base: the old union, newer side wins a conflict.

    With a base, each key is merged three-way: a side still holding the base value changed
    nothing, so the other side's change — including a deletion — wins; only when both sides
    changed the key differently is it a conflict, settled by which file is newer."""
    if base is None:
        newer, older = (run, vault) if run_newer else (vault, run)
        merged = {**older, **newer}
        conflicts = sorted(k for k in older if k in newer and older[k] != newer[k])
        return merged, conflicts, []
    merged_dict: dict[str, Any] = {}
    conflicts: list[str] = []
    for key in set(run) | set(vault) | set(base):
        r, v, b = run.get(key, MISSING), vault.get(key, MISSING), base.get(key, MISSING)
        if r == v:
            value = r
        elif v == b:
            value = r  # Only the run side changed: its value, or its deletion.
        elif r == b:
            value = v  # Only the vault side changed.
        else:
            conflicts.append(key)
            value = (run if run_newer else vault).get(key, MISSING)
        if value is not MISSING:
            merged_dict[key] = value
    removed = sorted(k for k in set(run) | set(vault) if k not in merged_dict)
    return merged_dict, sorted(conflicts), removed


def report_removed(gone: list[str], run: dict[str, Any], vault: dict[str, Any], run_is_source: bool,
                   removed: list[str]) -> None:
    """Say which keys the merge dropped, and which side's deletion did it."""
    shown = lambda keys: "、".join(keys[:8]) + (" 等" if len(keys) > 8 else "")
    by_run = [k for k in gone if k not in run]
    by_vault = [k for k in gone if k not in vault]
    if run_is_source:
        if by_run:
            removed.append(f"kb-config.json：{shown(by_run)} 在运行目录删掉了，知识库那份也删掉")
        if by_vault:
            removed.append(f"kb-config.json：{shown(by_vault)} 在知识库那份删掉了，但运行目录这份还留着，下次发布会补回")
    else:
        if by_vault:
            removed.append(f"kb-config.json：{shown(by_vault)} 在知识库那份删掉了，运行目录这份也删掉")
        if by_run:
            removed.append(f"kb-config.json：{shown(by_run)} 运行目录删掉了，知识库那份还留着，下次发布时知识库那份也删掉")


def record_adopted(base: Path, run_before: dict[str, Any], vault: dict[str, Any], merged: dict[str, Any]) -> bool:
    """Note in the base the vault values the run side just adopted, and forget vault deletions.

    The base then reads "the last value both sides agreed on": a setting taken over from the
    vault can be removed later by deleting it, before or after the next publish. A partial
    base is safe — keys absent from it behave like the old union."""
    current = json_object(base) or {}
    changed = False
    for key, value in merged.items():
        if (value is not MISSING and value != run_before.get(key, MISSING)
                and value == vault.get(key, MISSING) and current.get(key, MISSING) != value):
            current[key] = value
            changed = True
    for key in run_before.keys() - merged.keys() - vault.keys():
        if key in current:
            del current[key]
            changed = True
    if not changed:
        return False
    base.parent.mkdir(parents=True, exist_ok=True)
    write_text(base, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return True


def merge_objects(source: Path, target: Path, kept: list[str],
                  removed: list[str] | None = None, run_is_source: bool = False) -> bool:
    """Settings and decisions are merged entry by entry, not file by file.

    A whole-file "newer wins" lost a product list a person wrote into the vault, because a run
    folder's older config was saved later. Now an entry only one side has is kept; where both
    have it and differ, the newer file's value is used and the other is reported. kb-config.json
    alone merges three-way against the vault-base record, so a key deleted on purpose stays
    deleted. Only the target is written: restoring must not touch the published vault, saving
    must not touch the run folder."""
    left, right = json_object(source), json_object(target)
    if left is None or right is None:
        return False
    if source.name == target.name == "kb-config.json":
        run_path, vault_path = (source, target) if run_is_source else (target, source)
        run_side, vault_side = (left, right) if run_is_source else (right, left)
        merged, lost, gone = merge_settings(run_side, vault_side, json_object(config_base(run_path.parent)),
                                            run_path.stat().st_mtime >= vault_path.stat().st_mtime)
        if gone and removed is not None:
            report_removed(gone, run_side, vault_side, run_is_source, removed)
    else:
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


def copy_newer(source: Path, target: Path, kept: list[str],
               removed: list[str] | None = None, run_is_source: bool = False) -> bool:
    if not source.is_file() or same(source, target):
        return False
    if json_object(source) is not None and json_object(target) is not None:
        return merge_objects(source, target, kept, removed, run_is_source)
    if target.is_file() and target.stat().st_mtime > source.stat().st_mtime:
        kept.append(str(target))  # Someone changed the other copy later; keep theirs.
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def migrate_legacy(vault: Path) -> None:
    move_legacy_vault_dirs(vault, LEGACY)


def save(run: Path, vault: Path) -> dict[str, Any]:
    """Copy build state into the vault at publish.

    The one thing a save writes into the run folder is the vault-base record: the run
    folder's kb-config.json as published. It is the merge's base — what this run folder's
    config looked like when it last went to the vault — so a key only the vault has still
    looks like a vault-side change, not a run-side deletion."""
    migrate_legacy(vault)
    build = run / BUILD_DIRECTORY
    kept: list[str] = []
    removed: list[str] = []
    copied = sum(copy_newer(b, v, kept, removed, run_is_source=True) for b, v in pairs(build, vault))
    copied += merge_pages(build / "website" / "pages.json", vault / WEBSITE / "pages.json")
    base = config_base(build)
    run_config = json_object(build / "kb-config.json")
    if run_config is not None:
        base.parent.mkdir(parents=True, exist_ok=True)
        write_text(base, json.dumps(run_config, ensure_ascii=False, indent=2) + "\n")
    else:
        base.unlink(missing_ok=True)
    return {"saved": copied, "kept_newer_in_vault": kept, "removed": removed}


def restore(run: Path, vault: Path) -> dict[str, Any]:
    migrate_legacy(vault)
    build = run / BUILD_DIRECTORY
    build.mkdir(parents=True, exist_ok=True)
    run_config_before = json_object(build / "kb-config.json") or {}
    kept: list[str] = []
    removed: list[str] = []
    copied = sum(copy_newer(v, b, kept, removed) for b, v in pairs(build, vault))
    copied += merge_pages(vault / WEBSITE / "pages.json", build / "website" / "pages.json")
    vault_config = json_object(vault / STATE / "kb-config.json")
    run_config_after = json_object(build / "kb-config.json")
    if vault_config is not None and run_config_after is not None:
        # Whatever the run side took over from the vault becomes the agreed value in the base,
        # so deleting it later is seen as a deletion, not as a vault-side addition again.
        record_adopted(config_base(build), run_config_before, vault_config, run_config_after)
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
    return {"restored": copied, "kept_newer_in_run": kept, "removed": removed}
