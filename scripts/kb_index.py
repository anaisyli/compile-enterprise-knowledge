#!/usr/bin/env python3
"""Full-text index of a published vault, and a search that downstream tools can call.

Chinese has no spaces, so text is indexed as overlapping two-character pieces; a query is
cut the same way. A search ranks passages by how many of its pieces they hold (BM25), so a
long query still finds the best matches instead of nothing. Results carry layer, tier and
visibility, so a website build can ask for public content only.

The index lives in the vault, often on a network share. It is written to a temporary file and
swapped in, retrying while a reader holds the old one; a search reads a local copy, so readers
never hold the shared file open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from kb_common import parse_frontmatter


LAYERS = [Path("03_正式知识/10_客户知识"), Path("03_正式知识/20_外部公共知识"), Path("02_源资料/10_净化原文"),
          Path("02_源资料/50_外部调研证据")]
INDEX = Path("02_源资料/knowledge-index.sqlite")
CJK = re.compile(r"[一-鿿]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")
SKIP_SECTIONS = {"来源", "另见"}
REPLACE_TRIES = 5


def tokens(text: str) -> str:
    pieces = [word.casefold() for word in WORD.findall(text)]
    for run in CJK.findall(text):
        pieces.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return " ".join(pieces)


def aliases_of(fields: dict[str, str]) -> list[str]:
    raw = fields.get("aliases", "")
    try:
        value = json.loads(raw) if raw.startswith("[") else [raw] if raw else []
    except ValueError:
        value = [raw]
    return [str(a) for a in value if str(a).strip()]


def sections(body: str) -> list[tuple[str, str]]:
    """(heading, text) per ## section and per ### inside it; a ### chunk is named "parent > child"."""
    found, parent, title, lines = [], "正文", "正文", []

    def flush() -> None:
        if lines and parent not in SKIP_SECTIONS:
            found.append((title, "\n".join(lines).strip()))

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            parent = title = line[3:].strip()
            lines = []
        elif line.startswith("### "):
            flush()
            title, lines = f"{parent} > {line[4:].strip()}", []
        elif not line.startswith(("# ", "> ")):
            lines.append(line)
    flush()
    return [(title, text) for title, text in found if text]


def layer_of(relative: str) -> str:
    return {"10_客户知识": "客户知识", "20_外部公共知识": "外部公共知识", "10_净化原文": "原文", "50_外部调研证据": "外部证据"}.get(
        Path(relative).parts[1], "")


def replace_with_retry(source: Path, target: Path) -> None:
    """os.replace fails on Windows while another process has the target open; try a few times."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.3)


def build(project: Path) -> dict[str, Any]:
    path = project / INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(suffix=".sqlite", dir=path.parent)
    os.close(handle)
    temp = Path(temp_name)
    temp.unlink()
    connection = sqlite3.connect(temp)
    connection.execute("create table docs (doc_id text, title text, tier text, visibility text, module text, path text, status text, layer text, aliases text)")
    connection.execute("create virtual table chunks using fts5(doc_id unindexed, section unindexed, text unindexed, tokens)")
    count = 0
    for page in sorted(p for layer in LAYERS for p in (project / layer).rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        fields = parse_frontmatter(text)
        if not fields.get("doc_id"):
            continue  # Hand-written notes are the reader's own; they are not indexed.
        status = fields.get("status", "有效")
        relative = page.relative_to(project).as_posix()
        aliases = aliases_of(fields)
        connection.execute("insert into docs values (?,?,?,?,?,?,?,?,?)",
                           (fields["doc_id"], fields.get("title", page.stem), fields.get("tier"), fields.get("visibility"),
                            fields.get("module"), relative, status, layer_of(relative), "、".join(aliases)))
        if status in {"已失效"}:
            continue
        body = text.split("\n---\n", 1)[-1]
        names = " ".join([fields.get("title", page.stem), *aliases])
        for title, chunk in sections(body):
            connection.execute("insert into chunks values (?,?,?,?)", (fields["doc_id"], title, chunk, tokens(f"{names} {title} {chunk}")))
            count += 1
    connection.commit()
    connection.close()
    replace_with_retry(temp, path)
    return {"index": str(path), "chunks": count}


def local_copy(project: Path) -> Path:
    """A versioned copy of the vault's index on this computer, so concurrent readers never collide."""
    source = project / INDEX
    stat = source.stat()
    key = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    folder = Path(tempfile.gettempdir()) / "cek-index"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{key}.{stat.st_size}.{stat.st_mtime_ns}.sqlite"
    if not target.is_file():
        partial = folder / f"{key}.{os.getpid()}.part"
        shutil.copyfile(source, partial)
        try:
            os.replace(partial, target)
        except OSError:
            partial.unlink(missing_ok=True)
            if not target.is_file():
                raise
        for old in folder.glob(f"{key}.*.sqlite"):
            if old != target:
                try:
                    old.unlink()
                except OSError:
                    pass
    return target


def search(project: Path, query: str, limit: int, visibility: str | None, tier: str | None,
           layers: list[str] | None = None) -> list[dict[str, Any]]:
    terms = sorted({f'"{piece}"' for piece in tokens(query).split()})
    if not terms or not (project / INDEX).is_file():
        return []
    connection = sqlite3.connect(local_copy(project))
    sql = ("select d.title, d.layer, d.tier, d.visibility, d.status, d.path, c.section, c.text "
           "from chunks c join docs d on d.doc_id = c.doc_id where chunks match ?")
    parameters: list[Any] = [" OR ".join(terms)]
    if visibility:
        sql += " and d.visibility = ?"
        parameters.append(visibility)
    if tier:
        sql += " and d.tier = ?"
        parameters.append(tier)
    if layers:
        sql += " and d.layer in (" + ",".join("?" * len(layers)) + ")"
        parameters.extend(layers)
    sql += " order by rank limit ?"
    parameters.append(limit)
    try:
        rows = connection.execute(sql, parameters).fetchall()
    except sqlite3.OperationalError:
        rows = []  # An index from an older version has no layer names to filter on.
    connection.close()
    keys = ("title", "layer", "tier", "visibility", "status", "path", "section", "text")
    return [dict(zip(keys, row)) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Index a published vault, or search it.")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("build")
    one.add_argument("--project", required=True)
    find = sub.add_parser("search")
    find.add_argument("--project", required=True)
    find.add_argument("--query", required=True)
    find.add_argument("--limit", type=int, default=10)
    find.add_argument("--visibility", choices=["public", "internal"])
    find.add_argument("--tier", choices=["T1", "T2", "T3", "T4"])
    find.add_argument("--layer", action="append", choices=["客户知识", "外部公共知识", "原文", "外部证据"],
                      help="Only these layers; repeat")
    args = parser.parse_args()
    project = Path(args.project).resolve()
    if args.command == "build":
        result: Any = build(project)
    else:
        result = search(project, args.query, args.limit, args.visibility, args.tier, args.layer)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
