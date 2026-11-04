#!/usr/bin/env python3
"""Read the client's answers back from 客户确认表.csv into kb-config.json.

Only yes/no answers to "can this be used" questions change the build. Anything else, such
as which fabric is right, is kept as a note for a person, because turning free text into
a fact is exactly what this pipeline refuses to do.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, load_jsonl, write_json, write_jsonl


KEEP = {"保留", "可以", "可用", "同意", "是", "keep", "yes", "y", "ok"}
DROP = {"不用", "不可以", "删除", "不同意", "否", "drop", "no", "n"}
ANSWER_COLUMN = "回答（保留 / 不用 / 文字）"
QUESTION_COLUMN = "要确认的问题"


def normalized(answer: str) -> str | None:
    value = answer.strip().casefold().rstrip("。.!！")
    return "keep" if value in KEEP else "drop" if value in DROP else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply answers from 客户确认表.csv to kb-config.json.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", required=True, help="kb-config.json to update")
    parser.add_argument("--answers", help="Filled CSV; defaults to the build's 客户确认表.csv")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    config_path = Path(args.config).resolve()
    # Each answerable row has its own number (P-007-CUS-002.1), even inside a grouped entry.
    records = {item["sub_id"]: {**item, "id": item["sub_id"]} for r in load_json(build / "governance.json").get("CUS", [])
               for item in r.get("items", [])}
    answers_path = Path(args.answers).resolve() if args.answers else build / "客户确认表.csv"
    with answers_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    config: dict[str, Any] = load_json(config_path)
    config.setdefault("client_answers", {})
    config.setdefault("client_answers_units", {})
    applied, notes, unclear = [], [], []
    for row in rows:
        answer = (row.get(ANSWER_COLUMN) or row.get("回答") or "").strip()
        record = records.get(row.get("编号", ""))
        if not answer or not record:
            continue
        decision = normalized(answer)
        if not record.get("item_id"):
            # Which fabric, which year: a person decides, the build does not.
            notes.append({"id": record["id"], "question": record["question"], "answer": answer,
                          "at": datetime.now().isoformat(timespec="seconds")})
            continue
        if decision is None:
            unclear.append({"id": record["id"], "answer": answer})
            continue
        for item_id in record["item_id"].split(","):
            if record.get("location"):
                config["client_answers_units"].setdefault(item_id, {})[record["location"]] = decision
            else:
                config["client_answers"][item_id] = decision
        applied.append({"id": record["id"], "decision": decision})

    if applied:
        backup = config_path.with_name(f"{config_path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak.json")
        shutil.copy2(config_path, backup)
        write_json(config_path, config)
    if notes:
        write_jsonl(build / "client-notes.jsonl", load_jsonl(build / "client-notes.jsonl") + notes)
    print(json.dumps({"applied": len(applied), "notes_for_people": len(notes), "unclear": unclear,
                      "next": "重跑 run_pipeline.py，再发布" if applied else "没有可执行的回答"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
