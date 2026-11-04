#!/usr/bin/env python3
"""Full-text index of a published vault, and a search that downstream tools can call.

Chinese has no spaces, so text is indexed as overlapping two-character pieces; a query is
cut the same way. Results carry tier and visibility, so a website build can ask for public
content only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from kb_common import parse_frontmatter


LAYERS = [Path("03_正式知识/10_客户知识"), Path("02_源资料/10_净化原文")]
INDEX = Path("02_源资料/knowledge-index.sqlite")
CJK = re.compile(r"[一-鿿]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")


def tokens(text: str) -> str:
    pieces = [word.casefold() for word in WORD.findall(text)]
    for run in CJK.findall(text):
        pieces.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return " ".join(pieces)


def sections(body: str) -> list[tuple[str, str]]:
    found, title, lines = [], "正文", []
    for line in body.splitlines():
        if line.startswith("## "):
            if lines:
                found.append((title, "\n".join(lines).strip()))
            title, lines = line[3:].strip(), []
        elif not line.startswith(("# ", "> ")):
            lines.append(line)
    if lines:
        found.append((title, "\n".join(lines).strip()))
    return [(title, text) for title, text in found if text]


def build(project: Path) -> dict[str, Any]:
    path = project / INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.execute("create table docs (doc_id text, title text, tier text, visibility text, module text, path text, status text, layer text)")
    connection.execute("create virtual table chunks using fts5(doc_id unindexed, section unindexed, text unindexed, tokens)")
    count = 0
    for page in sorted(p for layer in LAYERS for p in (project / layer).rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        fields = parse_frontmatter(text)
        if not fields.get("doc_id"):
            continue  # Hand-written notes are the reader's own; they are not indexed.
        status = fields.get("status", "有效")
        relative = page.relative_to(project).as_posix()
        connection.execute("insert into docs values (?,?,?,?,?,?,?,?)",
                           (fields["doc_id"], fields.get("title", page.stem), fields.get("tier"), fields.get("visibility"),
                            fields.get("module"), relative, status, fields.get("layer", "")))
        if status in {"已失效"}:
            continue
        body = text.split("\n---\n", 1)[-1]
        for title, chunk in sections(body):
            connection.execute("insert into chunks values (?,?,?,?)", (fields["doc_id"], title, chunk, tokens(f"{title} {chunk}")))
            count += 1
    connection.commit()
    connection.close()
    return {"index": str(path), "chunks": count}


def search(project: Path, query: str, limit: int, visibility: str | None, tier: str | None) -> list[dict[str, Any]]:
    terms = [f'"{piece}"' for piece in tokens(query).split()]
    if not terms:
        return []
    connection = sqlite3.connect(project / INDEX)
    sql = ("select d.title, d.layer, d.tier, d.visibility, d.status, d.path, c.section, c.text "
           "from chunks c join docs d on d.doc_id = c.doc_id where chunks match ?")
    parameters: list[Any] = [" AND ".join(terms)]
    if visibility:
        sql += " and d.visibility = ?"
        parameters.append(visibility)
    if tier:
        sql += " and d.tier = ?"
        parameters.append(tier)
    sql += " order by rank limit ?"
    parameters.append(limit)
    rows = connection.execute(sql, parameters).fetchall()
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
    args = parser.parse_args()
    project = Path(args.project).resolve()
    if args.command == "build":
        result: Any = build(project)
    else:
        result = search(project, args.query, args.limit, args.visibility, args.tier)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
