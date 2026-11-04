#!/usr/bin/env python3
"""Hand product-table cells to a model one batch at a time, and keep every judgment.

Rules cannot clean free text that salespeople typed into product columns, so each distinct
cell is judged once. Judgments are keyed by the cell's text, so they carry over to reruns
and to the next client in the same trade.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, load_jsonl, normalize_space, short_hash, write_json, write_jsonl, write_text


REVIEW_DIRECTORY = "cell-review"
DECISIONS = "decisions.jsonl"
COLUMNS = "sheet-columns.json"
AUDIT = "t3-audit.json"
VALID = {"keep", "drop", "part"}
ROLES = {"style", "name", "description", "fabric", "size", "picture", "drop"}
MIN_AUDITED_FILES = 20

INSTRUCTIONS = """# 产品表单元格判断

每个批次文件一行一个单元格：`cell_key`、`role`（这一格所在的列）、`text`（原文）。
逐行判断，把结果写到同目录、同编号的 `answer-NNN.jsonl`，一行一个：

```json
{"cell_key": "...", "decision": "keep"}
{"cell_key": "...", "decision": "drop"}
{"cell_key": "...", "decision": "part", "keep_text": "原文里逐字存在的一段"}
```

- keep：整格都是产品信息，比如款号、品名、面料成分、克重、工艺、尺码。
- drop：整格不能进库，比如价格或成本（包括 19/m、5元每件这类写法）、数量、装箱、客户名或客户品牌、合作 IP、人名、日期、内部备注、供应商名。
- part：有用和不能用的混在一起。keep_text 只能是原文中连续的一段，不改写、不拼接、不翻译。

拿不准就 drop。少一格信息可以以后补，混进一个客户名就是事故。

`templates.jsonl` 是表格模板，每行给出表头和几行样本。请确认每一列是什么，写到 `template-answers.jsonl`：

```json
{"signature": "...", "roles": {"B": "style", "D": "description", "F": "drop"}}
```

可用的列：style 款号、name 品名、description 描述、fabric 面料、size 尺码、picture 图片、drop 不要。款号列里如果是序号或 Excel 日期数字，标 drop。
"""


def review_dir(out: Path) -> Path:
    return out / BUILD_DIRECTORY / REVIEW_DIRECTORY


def cell_key(text: str) -> str:
    return short_hash(normalize_space(text), 16)


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def load_decisions(out: Path) -> dict[str, dict[str, Any]]:
    return {record["cell_key"]: record for record in load_jsonl(review_dir(out) / DECISIONS)}


def load_columns(out: Path) -> dict[str, dict[str, str]]:
    path = review_dir(out) / COLUMNS
    return load_json(path) if path.is_file() else {}


def t3_gate(out: Path) -> tuple[bool, str]:
    """T3 opens only when every cell was judged and someone read the result independently."""
    progress = progress_of(out)
    if progress["cells_total"] == 0:
        return False, "还没有导出产品表单元格"
    if progress["cells_decided"] < progress["cells_total"]:
        return False, f"还有 {progress['cells_total'] - progress['cells_decided']} 个单元格没判断"
    audit_path = review_dir(out) / AUDIT
    audit = load_json(audit_path) if audit_path.is_file() else {}
    if audit.get("passed") is not True or int(audit.get("files_read", 0)) < MIN_AUDITED_FILES:
        return False, f"缺少独立通读记录：{AUDIT} 要写明 passed: true、files_read 至少 {MIN_AUDITED_FILES}"
    return True, ""


def all_cells(out: Path) -> dict[str, dict[str, str]]:
    cells: dict[str, dict[str, str]] = {}
    docs = {doc["doc_id"]: doc for doc in load_jsonl(out / BUILD_DIRECTORY / "docs.jsonl")}
    for block in load_jsonl(out / BUILD_DIRECTORY / "blocks-raw.jsonl"):
        if block["kind"] != "row" or docs.get(block["doc_id"], {}).get("tier") != "T3":
            continue
        for role, value in block["cells"].items():
            for piece in value.split(" / "):
                cells.setdefault(cell_key(piece), {"role": role, "text": normalize_space(piece)})
    return cells


def progress_of(out: Path) -> dict[str, int]:
    cells = all_cells(out)
    decided = load_decisions(out)
    return {"cells_total": len(cells), "cells_decided": sum(1 for key in cells if key in decided)}


def export(out: Path, batch_size: int) -> dict[str, Any]:
    folder = review_dir(out)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("batch-*.jsonl"):
        old.unlink()
    decided = load_decisions(out)
    pending = [{"cell_key": key, **cell} for key, cell in all_cells(out).items() if key not in decided]
    for index in range(0, len(pending), batch_size):
        write_jsonl(folder / f"batch-{index // batch_size + 1:03d}.jsonl", pending[index:index + batch_size])
    columns = load_columns(out)
    templates = load_json(out / BUILD_DIRECTORY / "sheet-templates.json") if (out / BUILD_DIRECTORY / "sheet-templates.json").is_file() else {}
    todo = []
    for signature, template in templates.items():
        if signature in columns:
            continue
        samples = [{c: v[:80] for c, v in row.items()} for row in template["samples"]]
        todo.append({"signature": signature, "headers": template["headers"], "current_roles": template["roles"],
                     "samples": samples, "files": len(template["files"])})
    write_jsonl(folder / "templates.jsonl", todo)
    write_text(folder / "README.md", INSTRUCTIONS)
    characters = sum(len(cell["text"]) for cell in pending)
    return {"cells_to_judge": len(pending), "batches": (len(pending) + batch_size - 1) // batch_size,
            "characters": characters, "templates_to_judge": len(todo), "folder": str(folder)}


def collect(out: Path) -> dict[str, Any]:
    """Validate answers and merge them. A part that is not verbatim is rejected, not trusted."""
    folder = review_dir(out)
    cells = all_cells(out)
    decided = load_decisions(out)
    rejected = []
    for path in sorted(folder.glob("answer-*.jsonl")):
        for record in load_jsonl(path):
            key, decision = record.get("cell_key"), record.get("decision")
            if key not in cells or decision not in VALID:
                rejected.append({"file": path.name, "cell_key": key, "why": "未知单元格或无效判断"})
                continue
            if decision == "part":
                keep = normalize_space(record.get("keep_text", ""))
                if not keep or squeezed(keep) not in squeezed(cells[key]["text"]):
                    rejected.append({"file": path.name, "cell_key": key, "why": "keep_text 不是原文中的一段"})
                    continue
                record["keep_text"] = keep
            decided[key] = {"cell_key": key, "decision": decision, **({"keep_text": record["keep_text"]} if decision == "part" else {})}
    write_jsonl(folder / DECISIONS, decided.values())

    columns = load_columns(out)
    for record in load_jsonl(folder / "template-answers.jsonl"):
        roles = {column: role for column, role in (record.get("roles") or {}).items() if role in ROLES}
        if record.get("signature") and roles:
            columns[record["signature"]] = roles
    write_json(folder / COLUMNS, columns)
    return {"decided": len(decided), "rejected": len(rejected), "rejections": rejected[:10], "templates": len(columns),
            **progress_of(out)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Product-table cell review: export batches, collect answers, report progress.")
    parser.add_argument("command", choices=["export", "collect", "status"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--batch-size", type=int, default=150)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.out).resolve()
    if args.command == "export":
        report = export(out, args.batch_size)
    elif args.command == "collect":
        report = collect(out)
    else:
        passed, reason = t3_gate(out)
        report = {**progress_of(out), "t3_gate": passed, "t3_gate_reason": reason}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
