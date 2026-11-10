#!/usr/bin/env python3
"""Point a vault at the package's new place, after both were copied to another computer.

The vault's 打开原件 / 打开文件夹 / 对照表 links open files in the package, so they carry the
package's full path. Copied elsewhere, the vault reads fine but those links open nothing.
This rewrites them to where the package is now, makes the one-click originals again (hard
links in the package's 审计记录/按知识库名字, which a zip does not carry), and records the new
place in the project page and the build state, so a later rebuild finds the package too.

Run once after unpacking:
    python kb_relink.py --vault <知识库项目目录> --package <初筛包新位置>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from kb_common import MANIFEST_NAME, load_json, write_json
from publish_vault import ARCHIVE, PROJECT_INFO, RECEIPT, normalized_hash
from render_kb import ALIAS_FOLDER, NAME_MAP_CSV

STATE_RUN_INFO = "05_数据与审核/70_构建状态/run-info.json"
FILE_LINK = re.compile(r"file:///[^)\s\]>|]+")


def plain(path: str) -> str:
    """A path for comparing: forward slashes, no trailing slash, case folded (Windows ignores case)."""
    return path.replace("\\", "/").rstrip("/").casefold()


def pair_packages(old: list[str], new: list[Path]) -> dict[str, Path]:
    """Old package path -> new one: by folder name when the names match, else in the order given."""
    pairs: dict[str, Path] = {}
    left = list(new)
    for path in old:
        same = next((p for p in left if p.name.casefold() == Path(path).name.casefold()), None)
        if same:
            pairs[path] = same
            left.remove(same)
    for path in old:
        if path not in pairs and left:
            pairs[path] = left.pop(0)
    return pairs


def link_path(link: str) -> str:
    """The local path a file: link points at, the same on every system: file:///C:/a gives C:/a
    (Windows), file:///Users/a gives /Users/a (Mac, Linux): the leading slash belongs to the path there."""
    path = unquote(urlparse(link).path)
    return path[1:] if re.match(r"^/[A-Za-z]:", path) else path


def moved_link(link: str, pairs: dict[str, Path]) -> str | None:
    """The same file under the package's new place, or None for a link into somewhere else."""
    local = link_path(link)
    for old, new in pairs.items():
        root = plain(old)
        if plain(local) == root or plain(local).startswith(root + "/"):
            rest = local.replace("\\", "/")[len(root):].lstrip("/")
            return (new / rest).resolve().as_uri() if rest else new.resolve().as_uri()
    return None


def relink_pages(vault: Path, pairs: dict[str, Path], dry_run: bool) -> list[str]:
    changed = []
    for page in sorted(vault.rglob("*.md")):
        key = page.relative_to(vault).as_posix()
        if key.startswith(ARCHIVE.as_posix()):
            continue  # Old versions stay as they were archived.
        text = page.read_text(encoding="utf-8")
        new = FILE_LINK.sub(lambda m: moved_link(m.group(0), pairs) or m.group(0), text)
        if new != text:
            changed.append(key)
            if not dry_run:
                page.write_text(new, encoding="utf-8", newline="\n")
    return changed


def remake_aliases(package: Path, dry_run: bool) -> tuple[int, list[str]]:
    """The package's name map says which knowledge-base name each original has."""
    table = package / NAME_MAP_CSV
    if not table.is_file():
        return 0, [f"{package.name}：没有 {NAME_MAP_CSV}，打开原件的链接没法重建"]
    made, problems = 0, []
    with table.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name, location = row.get("知识库名字", ""), row.get("初筛包里的位置（原名）", "")
            if not name or not location:
                continue
            source = package / location
            alias = package / ALIAS_FOLDER / f"{name}{Path(location).suffix}"
            if not source.is_file():
                problems.append(f"原文件不在：{location}")
                continue
            if dry_run:
                made += 1
                continue
            try:
                if alias.exists() and not alias.samefile(source):
                    alias.unlink()
                if not alias.exists():
                    alias.parent.mkdir(parents=True, exist_ok=True)
                    os.link(source, alias)
                made += 1
            except OSError as exc:
                problems.append(f"建不了 {alias.name}（{exc.strerror or type(exc).__name__}）")
    return made, problems


def record_place(vault: Path, pairs: dict[str, Path], dry_run: bool) -> None:
    state = vault / STATE_RUN_INFO
    if state.is_file():
        info = load_json(state)
        info["packages"] = [str(pairs.get(p, Path(p))) for p in info.get("packages", [])]
        if not dry_run:
            write_json(state, info)
    project = vault / PROJECT_INFO
    if project.is_file() and not dry_run:
        text = project.read_text(encoding="utf-8")
        places = "；".join(str(p) for p in pairs.values())
        text = re.sub(r"(?m)^- 客户原始资料路径：.*$", lambda _: f"- 客户原始资料路径：{places}", text)
        project.write_text(text, encoding="utf-8", newline="\n")


def keep_receipt(vault: Path, changed: list[str], dry_run: bool) -> None:
    """Relinked pages are still this skill's own; the receipt follows so the next publish goes ahead."""
    path = vault / RECEIPT
    if dry_run or not path.is_file():
        return
    receipt = load_json(path)
    for key in [*changed, PROJECT_INFO.as_posix()]:
        if key in receipt.get("files", {}) and (vault / key).is_file():
            receipt["files"][key] = normalized_hash(vault / key)
    write_json(path, receipt)


def main() -> int:
    parser = argparse.ArgumentParser(description="Point a copied vault at the package's new place.")
    parser.add_argument("--vault", required=True, help="Knowledge-base project folder")
    parser.add_argument("--package", action="append", required=True, help="Where the package is now; repeat for several")
    parser.add_argument("--dry-run", action="store_true", help="Only say what would change")
    args = parser.parse_args()
    vault = Path(args.vault).resolve()
    packages = [Path(p).resolve() for p in args.package]
    for package in packages:
        if not (package / MANIFEST_NAME).is_file():
            raise SystemExit(f"{package} 不是初筛包（没有 {MANIFEST_NAME}）")
    state = vault / STATE_RUN_INFO
    old = load_json(state).get("packages", []) if state.is_file() else []
    if not old:
        raise SystemExit(f"知识库里没有记录上次用的初筛包（{STATE_RUN_INFO}），不知道旧路径。")
    pairs = pair_packages(old, packages)
    changed = relink_pages(vault, pairs, args.dry_run)
    aliases, problems = 0, []
    for package in packages:
        made, issues = remake_aliases(package, args.dry_run)
        aliases += made
        problems += issues
    record_place(vault, pairs, args.dry_run)
    keep_receipt(vault, changed, args.dry_run)
    print(json.dumps({"packages": {old_path: str(new) for old_path, new in pairs.items()}, "pages_relinked": len(changed),
                      "originals_linked": aliases, "problems": problems[:10], "dry_run": args.dry_run}, ensure_ascii=False, indent=2))
    if problems:
        print("打开原件的链接有建不了的：初筛包要和原文件在同一个磁盘上，不能是网盘或只读位置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
