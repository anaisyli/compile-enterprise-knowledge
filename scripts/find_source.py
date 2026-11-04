#!/usr/bin/env python3
"""Find the real file behind a source document, for a person who wants to check it.

The vault uses new names only, so this looks the item up in the package's own audit
records: the name map first (知识库文件对照.csv), then the manifest. The real path is
printed to the terminal and never written into the vault.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from kb_common import MANIFEST_NAME, load_json, parse_frontmatter, sha256_file


SOURCE = Path("02_源资料/10_净化原文")
NAME_MAP = "审计记录/知识库文件对照.csv"


def item_from_vault(vault: Path, wanted: str) -> str | None:
    """Accept a document title, a file name, or a doc_id; return its package item_id."""
    for page in (vault / SOURCE).rglob("*.md"):
        fields = parse_frontmatter(page.read_text(encoding="utf-8"))
        if wanted in {page.stem, page.name, fields.get("doc_id"), fields.get("title")}:
            return fields.get("source_item_id")
    return None


def item_from_map(packages: list[Path], wanted: str) -> str | None:
    """The knowledge-base name as written in the package's name map."""
    for package in packages:
        path = package / NAME_MAP
        if not path.is_file():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if wanted in {row.get("知识库名字"), Path(row.get("知识库位置") or "").name}:
                    return row.get("资料编号")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the real file behind a source document.")
    parser.add_argument("--package", action="append", required=True, help="Preparation package; repeat")
    parser.add_argument("--id", required=True, help="item_id, doc_id, or the document title")
    parser.add_argument("--vault", help="Project vault, to look up titles and doc_ids")
    args = parser.parse_args()

    item_id = args.id
    if not args.id.startswith(("local-", "web-")):
        item_id = item_from_map([Path(p) for p in args.package], args.id) or (
            item_from_vault(Path(args.vault), args.id) if args.vault else None) or args.id
    for raw in args.package:
        package = Path(raw)
        for asset in load_json(package / MANIFEST_NAME)["assets"]:
            if asset["item_id"] != item_id:
                continue
            path = package / asset["path"] if asset.get("path") else None
            report = {"item_id": item_id, "original_name": asset.get("display_name"), "path": str(path) if path else None,
                      "exists": bool(path and path.is_file()),
                      "hash_matches": bool(path and path.is_file() and sha256_file(path) == asset["source_hash"])}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
    if item_id.startswith("web-"):
        print(json.dumps({"item_id": item_id, "note": "这是官网页面，原始位置是来源卡片里的网址"}, ensure_ascii=False))
        return 0
    print(json.dumps({"item_id": item_id, "error": "初筛包里没找到这份资料"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
