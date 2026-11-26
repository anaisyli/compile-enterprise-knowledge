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
JSON_DROP_DECISIONS = {"drop", "no", "exclude", "reject", "deny"}
JSON_OMIT_DECISIONS = {"omit", "skip", "unavailable", "no_source", "not_used"}
JSON_PENDING_DECISIONS = {"pending", "defer", "tbd", "hold"}
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
        whole_item_permission = (
            entry.get("sourceLocation") in WHOLE_FILE
            and low in JSON_KEEP_DECISIONS | JSON_DROP_DECISIONS
            and not has_values
            and not applicability
        )
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
                             "_from_json": True,
                             "_binary_permission": whole_item_permission,
                             "_json_decision": low,
                             "_json_decision_label": (entry.get("decisionLabel") or "").strip(),
                             "_json_values": {k: v.strip() for k, v in values.items() if isinstance(v, str) and v.strip()},
                             "_cus_id": entry.get("cusId") or "",
                             "_json_question": entry.get("question") or "",
                             "_source_name": entry.get("sourceName") or "",
                             "_source_location": entry.get("sourceLocation") or "",
                             "_force_note": (low in {"partial", "pending", "exception", "revise"} | JSON_OMIT_DECISIONS | JSON_PENDING_DECISIONS
                                             or has_values or bool(applicability)) and not whole_item_permission})
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply answers from 客户确认表.csv, .xlsx, or customer-feedback.v1 .json to kb-config.json.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", required=True, help="kb-config.json to update")
    parser.add_argument("--answers", help="Filled 客户确认表.csv (default), customer-feedback.v1 .json, or screening 客户确认表.xlsx")
    parser.add_argument("--release-awaiting-overrides", action="store_true",
                        help="For an explicitly confirmed build CSV, remove its obsolete awaiting-client route overrides")
    parser.add_argument("--previous-config", help="Optional config backup from before these CUS answers, to recover exact folder membership after resolution")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview config changes, unmatched IDs, and denied-item cache cleanup without writing")
    return parser.parse_args()


def cleanup_denied_caches(build: Path, denied_items: set[str], *, dry_run: bool = False) -> dict[str, Any]:
    """Find (and unless dry_run, remove) derived OCR/text/page-check caches for explicitly denied items."""
    if not denied_items:
        return {"items": [], "removed_files": [], "removed_page_checks": 0}
    removed_files: list[str] = []
    removed_page_checks = 0
    for item_id in sorted(denied_items):
        for rel in (f"mineru-units/{item_id}.json", f"reducto-units/{item_id}.json", f"package-text/{item_id}.json"):
            path = build / rel
            if path.is_file():
                removed_files.append(rel)
                if not dry_run:
                    path.unlink(missing_ok=True)
        for folder_rel in ("page-check/pages", "reviewed-page-images"):
            folder = build / folder_rel
            if folder.is_dir():
                for path in sorted(folder.glob(f"{item_id}*")):
                    if path.is_file():
                        removed_files.append(f"{folder_rel}/{path.name}")
                        if not dry_run:
                            path.unlink(missing_ok=True)
    results_path = build / "page-check" / "results.json"
    if results_path.is_file():
        results = load_json(results_path)
        drop_keys = [k for k, v in results.items() if isinstance(v, dict) and v.get("item_id") in denied_items]
        removed_page_checks = len(drop_keys)
        if drop_keys and not dry_run:
            for key in drop_keys:
                results.pop(key, None)
            write_json(results_path, results)
    return {"items": sorted(denied_items), "removed_files": removed_files, "removed_page_checks": removed_page_checks}


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
    existing_client_notes = load_jsonl(build / "client-notes.jsonl")
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
    resolved_auto: list[dict[str, Any]] = []
    omitted_or_unavailable: list[dict[str, Any]] = []
    facts_for_curation: list[dict[str, Any]] = []
    customer_pending: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    for row in rows:
        answer = (row.get(ANSWER_COLUMN) or row.get("回答") or "").strip()
        answer_id = row.get("编号", "")
        record = records.get(answer_id)
        if not answer:
            continue
        if record:
            seen_record_ids.add(record["id"])
        if not record:
            if row.get("_from_json"):
                cus_id = row.get("_cus_id", "")
                prior_confirmation = any(
                    cus_id and (value.get("cus_id") == cus_id or value.get("cus_id", "").startswith(cus_id + "."))
                    and value.get("decision") == normalized(answer)
                    and config.get("client_answers", {}).get(item_id) == normalized(answer)
                    for item_id, value in (config.get("client_answer_confirmations") or {}).items()
                )
                prior_note = any(
                    note.get("question") == row.get("_json_question") and note.get("answer") == answer
                    and note.get("from") != "客户确认网页 JSON（旧编号待映射）"
                    for note in existing_client_notes
                )
                if not prior_confirmation and not prior_note:
                    unclear.append({"id": answer_id, "answer": answer,
                                    "problem": "当前待处理清单中找不到此 answerId，答复未应用"})
                    if cus_id:
                        source = "、".join(part for part in (row.get("_source_name"), row.get("_source_location")) if part)
                        note_text = "；".join(part for part in (f"旧CUS编号：{cus_id}",
                                                                  f"来源：{source}" if source else "",
                                                                  row.get("_json_note", "")) if part)
                        notes.append({"id": f"{cus_id}-{answer_id}",
                                      "question": row.get("_json_question") or answer_id,
                                      "answer": answer,
                                      "note": note_text,
                                      "from": "客户确认网页 JSON（旧编号待映射）",
                                      "at": datetime.now().isoformat(timespec="seconds")})
            continue
        decision = normalized(answer)
        binary_permission = bool(row.get("_binary_permission"))
        if (not record.get("item_id")
                or (record.get("answer_type") == "text" and not binary_permission)
                or row.get("_force_note")):
            # Which fabric, which year, or partial scope from webpage JSON: a person/AI applies to curated pages.
            notes.append({"id": record["id"], "question": record["question"], "answer": answer,
                          **({"note": row["_json_note"]} if row.get("_json_note") else {}),
                          **({"from": "客户确认网页 JSON"} if row.get("_from_json") else {}),
                          "at": datetime.now().isoformat(timespec="seconds")})
            low_dec = row.get("_json_decision") or ""
            label_and_ans = f"{row.get('_json_decision_label', '')} {answer}"
            summary_item = {
                "id": record["id"],
                "cus_id": row.get("_cus_id") or record["id"].rsplit(".", 1)[0],
                "question": record.get("question") or row.get("_json_question") or "",
                "decision": row.get("_json_decision_label") or low_dec or answer[:40],
                **({"values": row["_json_values"]} if row.get("_json_values") else {}),
                **({"note": row["_json_note"]} if row.get("_json_note") else {}),
                **({"source": row["_source_name"]} if row.get("_source_name") else {}),
            }
            if low_dec in JSON_PENDING_DECISIONS or any(k in label_and_ans for k in ("还需内部确认", "待定", "暂不确定")):
                customer_pending.append(summary_item)
            elif low_dec in JSON_OMIT_DECISIONS or any(k in label_and_ans for k in ("暂不采用", "无法提供", "不采用")):
                omitted_or_unavailable.append(summary_item)
            else:
                facts_for_curation.append(summary_item)
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
        resolved_auto.append({
            "id": record["id"],
            "cus_id": row.get("_cus_id") or record["id"].rsplit(".", 1)[0],
            "decision": "允许" if decision == "keep" else "拒绝（清理缓存）",
            "question": record.get("question") or row.get("_json_question") or "",
            **({"source": row["_source_name"]} if row.get("_source_name") else {}),
            **({"note": row["_json_note"]} if row.get("_json_note") else {}),
        })

    # Answered questions wait for a person to verify (已答待核验); unanswered ones stay with their batch.
    original_config = load_json(config_path)
    changed = bool(applied) and config != original_config
    whole_item_changed = config.get("client_answers") != original_config.get("client_answers") or (
        config.get("route_overrides") != original_config.get("route_overrides")
    )
    unit_changed = config.get("client_answers_units") != original_config.get("client_answers_units")
    denied_items = {
        item_id for item_id, dec in (config.get("client_answers") or {}).items()
        if dec == "drop" and original_config.get("client_answers", {}).get(item_id) != "drop"
    }
    # Also include items explicitly answered "drop" in this batch even if already set, so residual caches get cleaned.
    for row_applied in applied:
        if row_applied.get("decision") == "drop":
            rec = records.get(row_applied["id"])
            if rec and rec.get("item_id") and not rec.get("location"):
                denied_items.update(i for i in rec["item_id"].split(",") if i)
    cache_cleanup = cleanup_denied_caches(build, denied_items, dry_run=args.dry_run)
    unmatched = [u for u in unclear if "找不到" in (u.get("problem") or "")]
    # Deduplicate governance queue items by canonical sub_id to report unanswered CUS items cleanly.
    unique_queue = {rec["id"]: rec for rec in records.values() if rec.get("id")}
    unanswered_in_queue = [
        {"id": sub_id, "cus_id": sub_id.rsplit(".", 1)[0], "question": rec.get("question") or ""}
        for sub_id, rec in sorted(unique_queue.items())
        if sub_id not in seen_record_ids
    ]
    resolution_summary = {
        "counts": {
            "resolved_auto": len(resolved_auto),
            "omitted_or_unavailable": len(omitted_or_unavailable),
            "facts_for_curation": len(facts_for_curation),
            "customer_pending": len(customer_pending),
            "unanswered_in_queue": len(unanswered_in_queue),
            "unmatched_ids": len(unmatched),
        },
        "resolved_auto": resolved_auto,
        "omitted_or_unavailable": omitted_or_unavailable,
        "facts_for_curation": facts_for_curation,
        "still_unresolved": {
            "customer_pending": customer_pending,
            "unanswered_in_queue": unanswered_in_queue,
            "unmatched_ids": unmatched,
        },
    }
    if whole_item_changed or cache_cleanup["removed_files"] or cache_cleanup["removed_page_checks"]:
        recommended_action = "full_pipeline"
        next_step = "涉及许可/入库去向变更，请重跑 run_pipeline.py，再按 resolution_summary 落实整理页修订与护栏后发布"
    elif unit_changed or changed:
        recommended_action = "from_guard"
        next_step = "仅涉及片段级答复，可用 run_pipeline.py --from guard 轻量刷新，再落实整理页修订后发布"
    elif facts_for_curation or omitted_or_unavailable:
        recommended_action = "curate_and_guard"
        next_step = "无文件去向变更；请按 resolution_summary 落实整理页新口径与不采用项护栏，关闭对应 CUS 后 --from curate 发布"
    else:
        recommended_action = "none"
        next_step = "没有新的配置或整理层变更，无需重跑"
    if args.dry_run:
        next_step = f"预演模式未写入任何文件；请先向用户汇报 resolution_summary（4 桶解决状态），确认后去掉 --dry-run 正式回填（预计后续动作：{recommended_action}）"
    else:
        if records:
            from governance_state import mark_answered
            mark_answered(build, [r["id"] for r in applied] + [n["id"] for n in notes])
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
    print(json.dumps({
        "dry_run": bool(args.dry_run),
        "applied": len(applied),
        "config_changed": bool(changed),
        "recommended_action": recommended_action,
        "notes_for_people": len(notes),
        "unclear": unclear,
        "unmatched_ids": unmatched,
        "preserved_answers": skipped,
        "denied_cache_cleanup": cache_cleanup,
        "resolution_summary": resolution_summary,
        "next": next_step,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
