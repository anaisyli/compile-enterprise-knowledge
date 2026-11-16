"""Split an old full-page transcript on a dimension-drawing page by choosing its lines.

A page reviewed in full before it was classified as a dimension drawing has one transcript
mixing callouts with whatever else is on the page (a parameter table, prose). That transcript is
withheld from the text (source_fidelity.reviewed_units). Here the model only picks which lines
are not the drawing; nothing is rewritten, so the split is always a part of the old transcript,
and the old transcript itself is kept inside the new record.

plan     page-check/split-todo.jsonl: one line per withheld page, with its picture and the
         old transcript line by line, numbered.
collect  page-check/split-answer-*.jsonl: {"key", "keep_lines": [n, ...]} or {"key",
         "unsplittable": true}. Chosen lines become a "part": "non_drawing" transcript in
         reviewed-pages.json; keep_lines [] marks the page as drawing only; unsplittable pages
         stay in the ledger for a person.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from kb_common import load_json, load_jsonl, write_json, write_text

FOLDER = "page-check"
TODO = "split-todo.jsonl"
RESULTS = "split-results.json"
RULES = """# 尺寸图页的旧人工整页稿：挑出非图纸的行

这些页后来确认是尺寸图，但改判之前已经有人把整页逐字转写过，尺寸标注和参数表、正文混在一份稿子里。
现在这份旧稿不进正文。请对照原页图，从旧稿里挑出**不属于尺寸图**的行（参数表的行、正文句子、标题），
只写行号，不改写、不补字、不合并。

- 只有尺寸标注、图名、零件编号：`keep_lines` 写 `[]`。
- 一行里尺寸标注和正文混在一起、分不开：这一页写 `"unsplittable": true`，留给人处理。

每页写一行 JSON，存成本目录下的 split-answer-001.jsonl：

    {"key": "<split-todo.jsonl 里的 key>", "keep_lines": [3, 4, 5]}
    {"key": "<key>", "keep_lines": []}
    {"key": "<key>", "unsplittable": true, "note": "第 2 行尺寸和功耗写在同一行"}

写完运行 `page_check.py collect --out <运行目录>`，再重跑流水线。
"""


def withheld_pages(build: Path) -> list[dict[str, Any]]:
    """Old full-page transcripts on pages the page check classified as dimension drawings."""
    results_path, reviewed_path = build / FOLDER / "results.json", build / "reviewed-pages.json"
    if not results_path.is_file() or not reviewed_path.is_file():
        return []
    reviewed = load_json(reviewed_path)
    found = []
    for key, result in sorted(load_json(results_path).items()):
        if not result.get("dimension_drawing"):
            continue
        locator = f"{result['unit']} {result['page']}"
        for record in reviewed.get(result["item_id"], []):
            if (record.get("locator") == locator and not record.get("part") and not record.get("exclude_kind")
                    and not record.get("split")):
                found.append({"key": key, "item_id": result["item_id"], "locator": locator, "record": record,
                              "caption": result.get("drawing_caption", "")})
    return found


def plan(build: Path) -> int:
    folder = build / FOLDER
    pages = withheld_pages(build)
    if not pages and not (folder / TODO).is_file():
        return 0
    folder.mkdir(parents=True, exist_ok=True)
    unsplittable = load_json(folder / RESULTS) if (folder / RESULTS).is_file() else {}
    with (folder / TODO).open("w", encoding="utf-8", newline="\n") as handle:
        for page in pages:
            if unsplittable.get(page["key"], {}).get("unsplittable"):
                continue  # Waiting for a person, not for the model again.
            lines = page["record"]["text"].splitlines()
            handle.write(json.dumps({
                "key": page["key"], "item_id": page["item_id"], "locator": page["locator"],
                "image": str(build / page["record"]["image_file"]), "drawing_caption": page["caption"],
                "lines": [{"n": n, "text": text} for n, text in enumerate(lines, start=1)],
            }, ensure_ascii=False) + "\n")
    write_text(folder / "SPLIT-README.md", RULES)
    return len(pages)


def collect(build: Path) -> dict[str, Any]:
    folder = build / FOLDER
    answers = [row for path in sorted(folder.glob("split-answer-*.jsonl")) for row in load_jsonl(path)]
    if not answers:
        return {"split": 0, "drawing_only": 0, "unsplittable": 0, "rejected": []}
    todo = {entry["key"]: entry for entry in load_jsonl(folder / TODO)} if (folder / TODO).is_file() else {}
    pages = {page["key"]: page for page in withheld_pages(build)}
    rejected, chosen = [], {}
    for answer in answers:
        key = answer.get("key")
        page, entry = pages.get(key), todo.get(key)
        if not page or not entry:
            rejected.append(f"{key}：不在 split-todo.jsonl 里，或这页已经拆过")
            continue
        if answer.get("unsplittable") is True:
            chosen[key] = ("unsplittable", [], str(answer.get("note", ""))[:300])
            continue
        lines = answer.get("keep_lines")
        count = len(entry["lines"])
        if (not isinstance(lines, list) or any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= count for n in lines)
                or len(set(lines)) != len(lines)):
            rejected.append(f"{key}：keep_lines 只能是 1 到 {count} 之间不重复的行号")
            continue
        chosen[key] = ("split" if lines else "drawing_only", sorted(lines), "")
    if rejected:
        raise ValueError("拆分答案有问题，什么都没写：\n- " + "\n- ".join(rejected))

    reviewed_path = build / "reviewed-pages.json"
    reviewed = load_json(reviewed_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    shutil.copy2(reviewed_path, reviewed_path.with_name(f"reviewed-pages.before-split-{stamp}.json.bak"))
    results = load_json(folder / RESULTS) if (folder / RESULTS).is_file() else {}
    counts = {"split": 0, "drawing_only": 0, "unsplittable": 0}
    for key, (kind, lines, note) in chosen.items():
        page = pages[key]
        counts[kind] += 1
        results[key] = {"kind": kind, "keep_lines": lines, "note": note, "at": stamp[:15],
                        **({"unsplittable": True} if kind == "unsplittable" else {})}
        if kind == "unsplittable":
            continue
        records = reviewed[page["item_id"]]
        index = next(i for i, r in enumerate(records) if r is not None and r.get("locator") == page["locator"]
                     and not r.get("part") and not r.get("split"))
        old = records[index]
        if kind == "drawing_only":
            records[index] = {**old, "split": "drawing_only"}
            continue
        original = old["text"].splitlines()
        records[index] = {key_: old[key_] for key_ in ("locator", "reviewed_by", "source_sha256", "image_sha256", "image_file")}
        records[index].update({"text": "\n".join(original[n - 1] for n in lines), "part": "non_drawing",
                               "note": f"从改判尺寸图前的整页稿中逐行选出非图纸部分（第 {'、'.join(map(str, lines))} 行）",
                               "withheld_original": old})
    write_json(reviewed_path, reviewed)
    write_json(folder / RESULTS, results)
    return {**counts, "rejected": []}


def split_errors(reviewed: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Each line of a split transcript is a line of the old one, in the same order."""
    errors = []
    for item_id, records in reviewed.items():
        for record in records:
            original = record.get("withheld_original")
            if record.get("part") != "non_drawing" or not original:
                continue
            old, position = original.get("text", "").splitlines(), 0
            if original.get("source_sha256") != record.get("source_sha256"):
                errors.append(f"拆分稿和旧稿不是同一份原件：{item_id} {record.get('locator')}")
                continue
            for line in record.get("text", "").splitlines():
                while position < len(old) and old[position] != line:
                    position += 1
                if position == len(old):
                    errors.append(f"拆分稿里有旧稿没有、或顺序不对的内容：{item_id} {record.get('locator')}：{line[:40]}")
                    break
                position += 1
    return errors
