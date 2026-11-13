#!/usr/bin/env python3
"""Read the client's answers back into kb-config.json.

Two tables carry answers: the build's own 客户确认表.csv, and the screening package's
客户确认表.xlsx, which the client may already have filled in before the knowledge base
was built. Either one is read with --answers, so the client never answers twice.

Only yes/no answers to "can this be used" questions change the build. Anything else, such
as which fabric is right, is kept as a note for a person, because turning free text into
a fact is exactly what this pipeline refuses to do.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from kb_common import BUILD_DIRECTORY, MANIFEST_NAME, POINTS_NAME, load_json, load_jsonl, write_json, write_jsonl


KEEP = {"保留", "可以", "可用", "同意", "是", "keep", "yes", "y", "ok"}
DROP = {"不用", "不可以", "删除", "不同意", "否", "drop", "no", "n"}
ANSWER_COLUMN = "回答（保留 / 不用 / 文字）"
QUESTION_COLUMN = "要确认的问题"
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# The screening workbook's columns, found by their headers.
SCREENING_COLUMNS = {"编号": "id", "您的选择": "choice", "备注": "note", "需要您确认什么": "question",
                     "在文件里的位置": "location", "来自哪个文件": "source"}
WHOLE_FILE = {"", "整份文件", "整个文件", "整个文件夹", "全部", "全文"}


def normalized(answer: str) -> str | None:
    value = answer.strip().casefold().rstrip("。.!！")
    return "keep" if value in KEEP else "drop" if value in DROP else None


def read_screening_workbook(path: Path) -> list[dict[str, str]]:
    """Rows of the screening package's 客户确认表.xlsx, as saved by Excel or WPS."""
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            for entry in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(f"{SHEET_NS}si"):
                shared.append("".join(node.text or "" for node in entry.iter(f"{SHEET_NS}t")))
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    table = []
    for row in sheet.iter(f"{SHEET_NS}row"):
        values = {}
        for cell in row.findall(f"{SHEET_NS}c"):
            inline, value = cell.find(f"{SHEET_NS}is"), cell.find(f"{SHEET_NS}v")
            if inline is not None:
                text = "".join(node.text or "" for node in inline.iter(f"{SHEET_NS}t"))
            elif cell.get("t") == "s" and value is not None and value.text:
                text = shared[int(value.text)]
            else:
                text = value.text if value is not None and value.text else ""
            values[re.sub(r"\d", "", cell.get("r", ""))] = text
        table.append(values)
    header = next((i for i, values in enumerate(table) if "编号" in values.values()), None)
    if header is None:
        raise SystemExit(f"不是初筛包的客户确认表，找不到“编号”一列：{path}")
    columns = {letter: SCREENING_COLUMNS[name] for letter, name in table[header].items() if name in SCREENING_COLUMNS}
    rows = []
    for values in table[header + 1:]:
        row = {field: values.get(letter, "").strip() for letter, field in columns.items()}
        if row.get("id"):
            rows.append(row)
    return rows


def screening_targets(package: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], set[str]]:
    """What each workbook number points at: assets by id, folders by group, knowledge points."""
    assets = {a["item_id"]: a for a in load_json(package / MANIFEST_NAME).get("assets", [])}
    groups: dict[str, list[str]] = {}
    for asset in assets.values():
        if asset.get("review_group"):
            groups.setdefault(asset["review_group"], []).append(asset["item_id"])
    points_file = package / POINTS_NAME
    points = {str(p.get("knowledge_id")) for p in load_jsonl(points_file)} if points_file.is_file() else set()
    return assets, groups, points


def apply_screening(path: Path, config: dict[str, Any]) -> tuple[list, list, list]:
    """Turn the screening workbook's answers into the same config entries as the CSV."""
    assets, groups, points = screening_targets(path.parent)
    applied, notes, unclear = [], [], []
    for row in read_screening_workbook(path):
        number, answer, note = row["id"], row.get("choice", ""), row.get("note", "")
        if not answer and not note:
            continue
        decision = normalized(answer) if answer else None
        if note or (answer and decision is None):
            # 需要修改 and free remarks are for a person to read, not facts for the build.
            notes.append({"id": number, "question": row.get("question", ""), "answer": answer, "note": note,
                          "source": row.get("source", ""), "from": "初筛客户确认表",
                          "at": datetime.now().isoformat(timespec="seconds")})
        if decision is None:
            if answer and answer != "需要修改":
                unclear.append({"id": number, "answer": answer})
            continue
        if number.startswith("folder:"):
            members = groups.get(number[len("folder:"):], [])
            if not members:
                unclear.append({"id": number, "answer": answer, "problem": "初筛包里找不到这个文件夹"})
                continue
            for item_id in members:
                config["client_answers"][item_id] = decision
        elif "#" in number and number.rsplit("#", 1)[0] in assets:
            asked = assets[number.rsplit("#", 1)[0]]
            location = row.get("location", "")
            # The workbook asks once for the same file kept in several folders.
            content = asked.get("source_hash")
            copies = [a for a in assets.values() if content and a.get("source_hash") == content] or [asked]
            for asset in copies:
                if location in WHOLE_FILE or location not in (asset.get("pending_units") or []):
                    config["client_answers"][asset["item_id"]] = decision
                else:
                    config["client_answers_units"].setdefault(asset["item_id"], {})[location] = decision
        elif number in points:
            config["client_answers"][number] = decision
        else:
            unclear.append({"id": number, "answer": answer, "problem": "初筛包里找不到这一项"})
            continue
        applied.append({"id": number, "decision": decision})
    return applied, notes, unclear


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply answers from 客户确认表.csv to kb-config.json.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", required=True, help="kb-config.json to update")
    parser.add_argument("--answers", help="Filled 客户确认表.csv of the build (default), or the screening "
                        "package's 客户确认表.xlsx")
    parser.add_argument("--release-awaiting-overrides", action="store_true",
                        help="For an explicitly confirmed build CSV, remove its obsolete awaiting-client route overrides")
    parser.add_argument("--previous-config", help="Optional config backup from before these CUS answers, to recover exact folder membership after resolution")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    config_path = Path(args.config).resolve()
    answers_path = Path(args.answers).resolve() if args.answers else build / "客户确认表.csv"
    config: dict[str, Any] = load_json(config_path)
    config.setdefault("client_answers", {})
    config.setdefault("client_answers_units", {})
    records: dict[str, dict[str, Any]] = {}
    if answers_path.suffix.casefold() == ".xlsx":
        if args.release_awaiting_overrides:
            raise SystemExit("解除待确认路由仅适用于本次建库的客户确认 CSV，不能用于旧初筛回答")
        # The screening workbook can be read before the first build.
        applied, notes, unclear = apply_screening(answers_path, config)
        rows: list[dict[str, str]] = []
    else:
        # Each answerable row has its own number (P-007-CUS-002.1), even inside a grouped entry.
        records = {item["sub_id"]: {**item, "id": item["sub_id"]}
                   for r in load_json(build / "governance.json").get("CUS", []) for item in r.get("items", [])}
        records.update({item["client_id"]: {**item, "id": item["sub_id"]}
                        for r in load_json(build / "governance.json").get("CUS", []) for item in r.get("items", [])
                        if item.get("client_id")})
        if args.release_awaiting_overrides:
            # Resolved CUS entries disappear from governance.json; their registered IDs persist.
            registry_path = build / "governance-ids.json"
            registry = load_json(registry_path).get("CUS", {}) if registry_path.is_file() else {}
            package_info = build / "run-info.json"
            packages = load_json(package_info).get("packages", []) if package_info.is_file() else []
            assets = [asset for package in packages for asset in load_json(Path(package) / MANIFEST_NAME).get("assets", [])]
            prior_overrides = load_json(Path(args.previous_config)).get("route_overrides", {}) if args.previous_config else {}
            for key, number in registry.items():
                sub_id = f"{config['project_id']}-CUS-{number:03d}.1"
                if sub_id in records:
                    continue
                group = key.removeprefix("media:")
                members = ([key.removeprefix("pending:")] if key.startswith("pending:") else
                           [a["item_id"] for a in assets
                            if (a.get("source_relpath") or "").replace("\\", "/").startswith(group + "/")
                            and prior_overrides.get(a["item_id"], {}).get("reason") == "media_awaiting_client"]
                           if key.startswith("media:") else [])
                if members:
                    records[sub_id] = {"id": sub_id, "item_id": ",".join(members), "location": ""}
        with answers_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        applied, notes, unclear = [], [], []
    for row in rows:
        answer = (row.get(ANSWER_COLUMN) or row.get("回答") or "").strip()
        record = records.get(row.get("编号", ""))
        if not answer or not record:
            continue
        decision = normalized(answer)
        if not record.get("item_id") or record.get("answer_type") == "text":
            # Which fabric, which year: a person decides, the build does not.
            notes.append({"id": record["id"], "question": record["question"], "answer": answer,
                          "at": datetime.now().isoformat(timespec="seconds")})
            continue
        if decision is None:
            unclear.append({"id": record["id"], "answer": answer})
            continue
        if args.release_awaiting_overrides:
            confirmations = config.setdefault("client_answer_confirmations", {})
            for item_id in [i for i, value in confirmations.items() if value.get("cus_id") == record["id"]]:
                del confirmations[item_id]
        for item_id in record["item_id"].split(","):
            if record.get("location"):
                config["client_answers_units"].setdefault(item_id, {})[record["location"]] = decision
            else:
                if args.release_awaiting_overrides:
                    override = (config.get("route_overrides") or {}).get(item_id) or {}
                    if override.get("reason") in {"awaiting_client", "media_awaiting_client"}:
                        del config["route_overrides"][item_id]
                    config.setdefault("client_answer_confirmations", {})[item_id] = {"decision": decision, "cus_id": record["id"]}
                config["client_answers"][item_id] = decision
                # We asked this one ourselves (ask_client); now that it is answered, the answer decides.
                if item_id in (config.get("ask_client") or {}):
                    del config["ask_client"][item_id]
        applied.append({"id": record["id"], "decision": decision})

    # Read again on every build: only a real change touches the config, and a remark is kept once.
    changed = applied and config != load_json(config_path)
    if changed:
        backup = config_path.with_name(f"{config_path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak.json")
        shutil.copy2(config_path, backup)
        write_json(config_path, config)
    if notes:
        kept = load_jsonl(build / "client-notes.jsonl")
        seen = {(n.get("id"), n.get("answer"), n.get("note")) for n in kept}
        fresh = [n for n in notes if (n.get("id"), n.get("answer"), n.get("note")) not in seen]
        if fresh:
            write_jsonl(build / "client-notes.jsonl", kept + fresh)
    print(json.dumps({"applied": len(applied), "config_changed": bool(changed), "notes_for_people": len(notes), "unclear": unclear,
                      "next": "重跑 run_pipeline.py，再发布" if changed else "没有新的回答"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
