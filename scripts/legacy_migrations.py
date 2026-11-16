#!/usr/bin/env python3
"""Isolated compatibility and one-time repair rules for earlier projects (P-005, P-010).

Keeping these rules in one module keeps the main pipeline scripts clean while allowing active
legacy projects (such as P-010 while still being edited) to rebuild and publish without breaking.
Run `python scripts/legacy_migrations.py status --out <run_dir> [--vault <vault_dir>]` to check
whether a project still relies on any legacy state.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, safe_name


LEGACY_VAULT_DIRS = {
    "02_源资料/20_MinerU按需提取": "05_数据与审核/70_构建状态/mineru-units",
    "02_源资料/21_Reducto按需提取": "05_数据与审核/70_构建状态/reducto-units",
}


def has_legacy_reviewed_pages(build: Path) -> bool:
    """True only when a project carries pre-dimension-rule human page transcripts (reviewed-pages.json)."""
    return (build / "reviewed-pages.json").is_file()


def move_legacy_vault_dirs(vault: Path, legacy_map: dict[str, str] | None = None) -> list[str]:
    """Move older source-folder MinerU/Reducto caches into 05_数据与审核/70_构建状态/."""
    moved: list[str] = []
    for old, new in (legacy_map or LEGACY_VAULT_DIRS).items():
        source = vault / old
        if not source.is_dir():
            continue
        for item in source.glob("*.json"):
            target = vault / new / item.name
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                moved.append(f"{old}/{item.name} -> {new}/{item.name}")
        shutil.rmtree(source, ignore_errors=True)
    return moved


def repair_legacy_picture_name(
    file: str,
    named_by: str,
    item: dict[str, Any],
    old: dict[str, Any],
    namer: Any,
    proposed_file: str,
    short: str,
    suffix: str,
    from_source_label: str,
    numbered_label: str,
) -> tuple[str, str]:
    """Repair two historical auto-naming bugs from early P-010 builds without touching human/AI names:
    1. Photographs on mixed pages that accidentally took the adjacent dimension drawing's caption.
    2. Pictures in folders named '大尺寸' that were misfiled under '尺寸图'.
    """
    caption = item.get("excluded_drawing_caption")
    if caption and old.get("named_by") == from_source_label:
        bad_labels = {namer.label(caption), namer.label(item.get("excluded_drawing_name", ""))}
        bad_labels.update(label.removeprefix("的").strip() for label in list(bad_labels))
        bad_names = {
            safe_name("-".join(p for p in (item["prefix"], label) if p)) + f"-{short}{suffix}"
            for label in bad_labels if label
        }
        if Path(file).name in bad_names:
            file = proposed_file
            named_by = from_source_label if item.get("label") else numbered_label
    if ("大尺寸" in item.get("source_relpath", "") and Path(file).parent.name == "尺寸图"
            and Path(item["folder"]).name in {"详情图", "效果图"}):
        file = f"{item['folder']}/{Path(file).name}"
    return file, named_by


def status_report(out: Path | None, vault: Path | None) -> dict[str, Any]:
    """Inspect how much legacy state an active project (e.g. P-010) still relies on."""
    build = (out.resolve() / BUILD_DIRECTORY) if out else None
    state = (vault.resolve() / "05_数据与审核/70_构建状态") if vault else None
    root = build if (build and build.is_dir()) else state
    report: dict[str, Any] = {"checked_dir": str(root) if root else None}
    if root and root.is_dir():
        reviewed = load_json(root / "reviewed-pages.json") if (root / "reviewed-pages.json").is_file() else {}
        total_reviews = sum(len(v) for v in reviewed.values())
        unsplit = 0
        if reviewed and (root / "page-check/results.json").is_file():
            checks = load_json(root / "page-check/results.json")
            for key, res in checks.items():
                if not res.get("dimension_drawing"):
                    continue
                loc = f"{res['unit']} {res['page']}"
                for rec in reviewed.get(res["item_id"], []):
                    if rec.get("locator") == loc and not rec.get("part") and not rec.get("exclude_kind") and not rec.get("split"):
                        unsplit += 1
        layout = load_json(root / "picture-layout.json") if (root / "picture-layout.json").is_file() else {}
        config = load_json(root / "kb-config.json") if (root / "kb-config.json").is_file() else {}
        report.update({
            "reviewed_pages_items": len(reviewed),
            "reviewed_pages_total": total_reviews,
            "unsplit_drawing_reviews": unsplit,
            "picture_layout": layout,
            "legacy_route_overrides": len(config.get("route_overrides") or {}),
        })
    if vault and vault.is_dir():
        report["legacy_vault_dirs"] = [d for d in LEGACY_VAULT_DIRS if (vault / d).is_dir()]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or run legacy project migrations (e.g. P-010).")
    parser.add_argument("command", choices=["status"], nargs="?", default="status")
    parser.add_argument("--out", help="Run directory")
    parser.add_argument("--vault", help="Vault directory")
    args = parser.parse_args()
    print(json.dumps(status_report(Path(args.out) if args.out else None, Path(args.vault) if args.vault else None),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
