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

from kb_common import BUILD_DIRECTORY, MANIFEST_NAME, POINTS_NAME, load_json, load_jsonl, run_lock, sha256_file, write_json, write_jsonl


KEEP = {"保留", "可以", "可用", "同意", "是", "keep", "yes", "y", "ok"}
DROP = {"不用", "不可以", "删除", "不同意", "否", "drop", "no", "n"}
JSON_KEEP_DECISIONS = {"keep", "yes", "whole", "approve", "allow"}
JSON_DROP_DECISIONS = {"drop", "no", "exclude", "reject"}
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


def answer_key(item_id: str, location: str = "") -> str:
    return json.dumps([item_id, location], ensure_ascii=False)


def apply_screening(path: Path, config: dict[str, Any]) -> tuple[list, list, list, list]:
    """Turn the screening workbook's answers into the same config entries as the CSV."""
    assets, groups, points = screening_targets(path.parent)
    applied, notes, unclear, skipped = [], [], [], []
    manifest = sha256_file(path.parent / MANIFEST_NAME)
    origins = config.get("screening_answer_origins", {})

    def set_answer(item_id: str, location: str, decision: str) -> str:
        answers = (config["client_answers_units"].get(item_id, {}) if location else config["client_answers"])
        key = location or item_id
        current = answers.get(key)
        if current == decision:
            return "unchanged"
        origin_key = answer_key(item_id, location)
        if current is not None and origins.get(origin_key) != {"manifest": manifest, "value": current}:
            return "preserved"
        if location:
            config["client_answers_units"].setdefault(item_id, {})[location] = decision
        else:
            config["client_answers"][item_id] = decision
        origins[origin_key] = {"manifest": manifest, "value": decision}
        config["screening_answer_origins"] = origins
        return "applied"

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
        targets = []
        if number.startswith("folder:"):
            members = groups.get(number[len("folder:"):], [])
            if not members:
                unclear.append({"id": number, "answer": answer, "problem": "初筛包里找不到这个文件夹"})
                continue
            targets = [(item_id, "") for item_id in members]
        elif "#" in number and number.rsplit("#", 1)[0] in assets:
            asked = assets[number.rsplit("#", 1)[0]]
            location = row.get("location", "")
            # The workbook asks once for the same file kept in several folders.
            content = asked.get("source_hash")
            copies = [a for a in assets.values() if content and a.get("source_hash") == content] or [asked]
            for asset in copies:
                unit = location if location not in WHOLE_FILE and location in (asset.get("pending_units") or []) else ""
                targets.append((asset["item_id"], unit))
        elif number in points:
            targets = [(number, "")]
        else:
            unclear.append({"id": number, "answer": answer, "problem": "初筛包里找不到这一项"})
            continue
        results = [(item_id, unit, set_answer(item_id, unit, decision)) for item_id, unit in targets]
        if any(result == "applied" for _, _, result in results):
            applied.append({"id": number, "decision": decision})
        skipped.extend({"id": number, "item_id": item_id, "location": unit,
                        "reason": "已有其他来源的客户决定"} for item_id, unit, result in results if result == "preserved")
    return applied, notes, unclear, skipped


def read_feedback_json(path: Path) -> list[dict[str, Any]]:
    """Convert customer-feedback.v1 JSON (exported by customer-confirmation-webpage) into answer rows."""
    payload = load_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
        raise SystemExit(f"不是有效的客户反馈 JSON（缺少 answers 列表）：{path}")
    reviewer = payload.get("reviewer") if isinstance(payload.get("reviewer"), dict) else {}
    reviewer_tag = " / ".join(v for v in (reviewer.get("name"), reviewer.get("team")) if isinstance(v, str) and v.strip())
    rows: list[dict[str, Any]] = []
    for entry in payload["answers"]:
        if not isinstance(entry, dict):
            continue
        decision = (entry.get("decision") or "").strip()
        answer_text = (entry.get("answerText") or "").strip()
        if not decision and not answer_text:
            continue
        values = entry.get("values") if isinstance(entry.get("values"), dict) else {}
        has_values = any(isinstance(v, str) and v.strip() for v in values.values())
        applicability = (entry.get("applicability") or "").strip()
        note = (entry.get("note") or "").strip()
        low = decision.casefold()
        # Only a clean whole-item yes/no without extra scope or correction values auto-applies as binary 保留/不用.
        if low in JSON_KEEP_DECISIONS and not has_values and not applicability:
            answer = "保留"
        elif low in JSON_DROP_DECISIONS and not has_values and not applicability:
            answer = "不用"
        else:
            answer = answer_text or decision
        target_ids = [entry.get("answerId") or entry.get("id") or ""]
        linked = entry.get("linkedAnswers") or []
        if linked and low != "exception":
            target_ids.extend(l.get("answerId") or "" for l in linked if isinstance(l, dict))
        extra_note = "；".join(p for p in (
            f"适用范围：{applicability}" if applicability else "",
            f"备注：{note}" if note else "",
            f"填写人：{reviewer_tag}" if reviewer_tag else "",
        ) if p)
        for tid in target_ids:
            if tid:
                rows.append({"编号": tid, ANSWER_COLUMN: answer, "_json_note": extra_note, "_json_raw": answer_text,
                             "_from_json": True, "_force_note": low in {"partial", "pending", "exception", "revise"} or has_values or bool(applicability)})
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply answers from 客户确认表.csv, .xlsx, or customer-feedback.v1 .json to kb-config.json.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", required=True, help="kb-config.json to update")
    parser.add_argument("--answers", help="Filled 客户确认表.csv (default), customer-feedback.v1 .json, or screening 客户确认表.xlsx")
    parser.add_argument("--release-awaiting-overrides", action="store_true",
                        help="For an explicitly confirmed build CSV, remove its obsolete awaiting-client route overrides")
    parser.add_argument("--previous-config", help="Optional config backup from before these CUS answers, to recover exact folder membership after resolution")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not run_lock(Path(args.out).resolve(), "apply_answers.py"):
        return 3
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    config_path = Path(args.config).resolve()
    answers_path = Path(args.answers).resolve() if args.answers else build / "客户确认表.csv"
    config: dict[str, Any] = load_json(config_path)
    config.setdefault("client_answers", {})
    config.setdefault("client_answers_units", {})
    records: dict[str, dict[str, Any]] = {}
    skipped = []
    if answers_path.suffix.casefold() == ".xlsx":
        if args.release_awaiting_overrides:
            raise SystemExit("解除待确认路由仅适用于本次建库的客户确认 CSV，不能用于旧初筛回答")
        # The screening workbook can be read before the first build.
        applied, notes, unclear, skipped = apply_screening(answers_path, config)
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
        if answers_path.suffix.casefold() == ".json":
            rows = read_feedback_json(answers_path)
        else:
            with answers_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        applied, notes, unclear = [], [], []
    for row in rows:
        answer = (row.get(ANSWER_COLUMN) or row.get("回答") or "").strip()
        record = records.get(row.get("编号", ""))
        if not answer or not record:
            continue
        decision = normalized(answer)
        if not record.get("item_id") or record.get("answer_type") == "text" or row.get("_force_note"):
            # Which fabric, which year, or partial scope from webpage JSON: a person decides, the build does not.
            notes.append({"id": record["id"], "question": record["question"], "answer": answer,
                          **({"note": row["_json_note"]} if row.get("_json_note") else {}),
                          **({"from": "客户确认网页 JSON"} if row.get("_from_json") else {}),
                          "at": datetime.now().isoformat(timespec="seconds")})
            continue
        if decision is None:
            unclear.append({"id": record["id"], "answer": answer})
            continue
        if row.get("_json_note"):
            notes.append({"id": record["id"], "question": record["question"], "answer": answer,
                          "note": row["_json_note"], "from": "客户确认网页 JSON",
                          "at": datetime.now().isoformat(timespec="seconds")})
        if args.release_awaiting_overrides:
            confirmations = config.setdefault("client_answer_confirmations", {})
            for item_id in [i for i, value in confirmations.items() if value.get("cus_id") == record["id"]]:
                del confirmations[item_id]
        for item_id in record["item_id"].split(","):
            (config.get("screening_answer_origins") or {}).pop(answer_key(item_id, record.get("location") or ""), None)
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

    # Answered questions wait for a person to verify (已答待核验); unanswered ones stay with their batch.
    if records:
        from governance_state import mark_answered
        mark_answered(build, [r["id"] for r in applied] + [n["id"] for n in notes])
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
                      "preserved_answers": skipped,
                      "next": "重跑 run_pipeline.py，再发布" if changed else "没有新的回答"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
