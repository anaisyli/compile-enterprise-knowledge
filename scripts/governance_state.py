#!/usr/bin/env python3
"""Decisions about the CUS / MAT / ANM ledgers, kept apart from the findings.

governance.py finds the problems again on every build. This module keeps what people and
agents decided about them, in governance-state.json, and lays it over the findings: the
evidence still there keeps its decision, evidence that disappeared closes, new evidence or a
changed source reopens. Run twice on the same input and nothing grows.

Every finding group has one status:
  internal  待内部处理 · 风险保留 · 已转 CUS · 已关闭
  customer  待汇总 · 已导出待发 · 已发待答 · 已答待核验 · 已关闭
"风险保留" means the material is used with its ⚠ and nothing is left to do: it is not a to-do.

Commands (python governance_state.py <command> --out <run folder> …, or governance.py <command>):
  sync        read the stages and notes people wrote in the published ledgers (run first)
  auto        settle what rules can settle: plain vocabulary words are allowed
  review      write governance-review.md: list A (internal decisions) and list B (questions for the client)
  collect     apply a decisions JSON, rewrite the ledgers, the client sheet and the writing guards
  flag-value  register one value the client must confirm (one CUS question + one writing guard)
  cus-export  export this batch of 待汇总 questions for the client; cus-sent marks it sent
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, load_jsonl, run_lock, write_json, write_text

STATE = "governance-state.json"
STATE_SCHEMA = "governance-state.v2"
GUARDS = "writing-guards.json"
REVIEW = "governance-review.md"
SUMMARY = "governance-decisions-summary.json"
INTERNAL_WEBPAGE_JSON = "internal-review-input.json"
VOCABULARY = Path(__file__).resolve().parents[1] / "assets" / "vocabulary-common.txt"
VOCABULARY_VERSION = "vocab-common-1"
INTERNAL = {"action_needed": "待内部处理", "risk_retained": "风险保留", "escalated_cus": "已转 CUS", "closed": "已关闭"}
CUSTOMER = {"draft": "待汇总", "exported": "已导出待发", "sent": "已发待答", "answered": "已答待核验", "closed": "已关闭"}
LABELS = set(INTERNAL.values()) | set(CUSTOMER.values())
DONE_MEMBER = {"closed", "risk_retained", "answered", "escalated_cus"}
# Findings whose content stays in use with its ⚠: a mark, not a task.
RETAINED_BY_DEFAULT = {"ai_read", "report_page", "reference_draft", "unverified_name_retained"}
# Questions about whether material may be used at all, asked apart from questions about facts.
PERMISSION = ("pending:", "website_cases", "media:", "images")
# What list A recommends for each kind of finding, and the action "按建议" takes.
RECOMMEND = {
    "ai_read": ("保留来源标记（风险保留）：内容照用并带 ⚠，引用前对照原图", "risk_retained"),
    "report_page": ("风险保留：只当提及，不作为已通过检测或认证的证据", "risk_retained"),
    "reference_draft": ("风险保留：只能写成“参考资料显示……（待公司确认）”；确认是公司原稿的在 doc_roles 写 original", "risk_retained"),
    "unverified_name": ("普通词已按词典放行；剩下的品牌、认证、组织、IP 词保留 ⚠（风险保留）", "risk_retained"),
    "text_errors": ("对照原图精校（被整理页引用的页先做），修完的页逐页关闭；原件本身看不清或疑似印错的转客户", None),
    "dimension_drawing_model": ("能从页标题或同页款号确认的改名；其余整组保留为“型号待确认”，不问客户", "closed"),
    "dimension_drawing_model:neighbor_title": ("相邻规格页或标题已有明确型号，核对后整组确认归属（可取消个别例外）", "closed"),
    "dimension_drawing_model:weak_hint": ("目录或文件名有系列线索但页内未写明，抽看原图后决定归属或保留待确认", None),
    "dimension_drawing_model:unknown": ("原图和周边均无线索，整组关闭为“保留为型号待确认”，不问客户", "closed"),
    "removal": ("复查这些文件里已没有价格和联系方式后关闭", "closed"),
    "heavy_removal": ("看《内容清理记录》，确认没删错后关闭；删错的改 deny_terms 或在 review-decisions 放行", "closed"),
    "screening_overruled": ("确认保留的内容是产品信息后关闭；确实该删的写 drop_blocks", "closed"),
    "website": ("沿用旧快照，写明快照日期和下次重试条件（风险保留）", "risk_retained"),
    "browser": ("用浏览器读取后 --browser-capture 导入；暂时做不了就沿用快照（风险保留）", "risk_retained"),
}
DETAIL_NOT_MEMBER = re.compile(r"^- (?:为什么|现在怎么|怎么关闭|下一步|重开条件|回复前|具体要确认|原件位置|处理记录)")


def today() -> str:
    return date.today().isoformat()


def load_state(build: Path) -> dict[str, Any]:
    state = load_json(build / STATE) if (build / STATE).is_file() else {}
    state.setdefault("schema", STATE_SCHEMA)
    for key, empty in (("items", {}), ("cus_batches", {}), ("disputed_values", []), ("extra_cus", []), ("history", [])):
        state.setdefault(key, empty)
    return state


def save_state(build: Path, state: dict[str, Any]) -> None:
    write_json(build / STATE, state)


def member_key(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()[:16]


def members_of(record: dict[str, Any]) -> list[tuple[str, str]]:
    """(key, text) per piece of evidence: a CUS sub-question, or a listed page, term or file."""
    if record.get("items"):
        return [(item["sub_id"], item.get("question") or item["sub_id"]) for item in record["items"]]
    found = []
    for line in record.get("details_raw") or record.get("details") or []:
        text = line.strip()
        if text.startswith("- ") and not DETAIL_NOT_MEMBER.match(text):
            found.append(text[2:])
        elif text.startswith("|") and not re.fullmatch(r"\|[\s:|-]+\|?", text) and not re.match(r"^\|\s*(?:词|写法|款号|位置|页面)\s*\|", text):
            found.append(text)
    return [(member_key(t), t) for t in found] or [("all", record["row"][0])]


def kind_of(record: dict[str, Any]) -> str:
    return record["key"].split(":", 1)[0]


def default_status(ledger: str, record: dict[str, Any]) -> str:
    if ledger == "CUS":
        return "draft"
    return "risk_retained" if kind_of(record) in RETAINED_BY_DEFAULT else "action_needed"


def note(item: dict[str, Any], text: str, by: str = "agent") -> None:
    line = f"{today()} {'Agent' if by == 'agent' else '人工' if by == 'human' else by}：{text}"
    if line not in item.setdefault("records", []):
        item["records"].append(line)  # The same decision twice is one record.


def value_records(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """CUS questions the state itself owns: flagged values and escalated findings."""
    found = []
    for value in state["disputed_values"]:
        where = f"{value['page']} · {value.get('section', '')}"
        cell = "、".join(x for x in (value.get("row"), value.get("column")) if x)
        details = [f"- 具体要确认：{value['question']}", f"- 原件位置：{where}{'（' + cell + '）' if cell else ''}",
                   f"- 原印数值：{value['value']}", "- 回复前：正式知识保留原文值并标待核实；取资料时这一格不可引用，文章写到这个数会被核对报警。"]
        found.append((value["id"], {"id": value["id"], "key": f"value:{value['id']}", "kind": "CUS",
                                    "row": [value["question"], "1 项", "", "内容运营 → 客户"], "details_raw": details,
                                    "items": [{"key": value["id"], "item_id": "", "location": where, "question": value["question"],
                                               "sub_id": f"{value['id']}.1", "answer_type": "text"}],
                                    "updated": value.get("at", today())}))
    for extra in state["extra_cus"]:
        found.append((extra["id"], {"id": extra["id"], "key": f"escalated:{extra['from']}", "kind": "CUS",
                                    "row": [extra["question"], "1 项", "", "内容运营 → 客户"],
                                    "details_raw": [f"- 具体要确认：{extra['question']}", f"- 来自：{extra['from']}（内部核对后转客户）"],
                                    "items": [{"key": extra["id"], "item_id": "", "location": "", "question": extra["question"],
                                               "sub_id": f"{extra['id']}.1", "answer_type": "text"}],
                                    "updated": extra.get("at", today())}))
    return found


def overlay(build: Path, records: dict[str, list[dict[str, Any]]], state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lay the decisions over this build's findings; returns the summary. Records are changed in place:
    their stage shows the status, their details carry the decision history."""
    state = state or load_state(build)
    seen: set[str] = set()
    for record_id, record in value_records(state):
        if not any(r["id"] == record_id for r in records["CUS"]):
            records["CUS"].append(record)
    summary = {"internal_groups": 0, "internal_open_evidence": 0, "cus_draft": 0, "cus_exported": 0, "cus_waiting": 0,
               "cus_answered": 0, "risk_retained": 0, "closed": 0, "evidence": 0}
    for ledger, ledger_records in records.items():
        labels = CUSTOMER if ledger == "CUS" else INTERNAL
        for record in ledger_records:
            record.setdefault("details_raw", list(record.get("details") or []))
            record["kind"] = ledger
            seen.add(record["id"])
            item = state["items"].setdefault(record["id"], {"ledger": ledger, "status": default_status(ledger, record),
                                                            "members": {}, "records": []})
            item["ledger"], item["key"], item["title"] = ledger, record["key"], record["row"][0]
            if "vanished_from" in item:
                # Closed only because a build did not see it: back to where it was.
                item["status"] = item.pop("vanished_from")
                note(item, "重新检测到，恢复原来的状态", "系统")
            current = members_of(record)
            known = item["members"]
            fresh = [k for k, _ in current if k not in known]
            for key, text in current:
                entry = known.setdefault(key, {"status": "open", "text": text})
                if entry.get("text") != text:
                    entry["text"] = text  # Keep full text and heal older truncated state entries.
            # Evidence that is gone is no longer counted; a decision on what remains stands.
            for key in [k for k in known if k not in {k for k, _ in current}]:
                del known[key]
            if fresh and item["status"] in {"closed", "risk_retained", "answered"} and item.get("decided_members"):
                trigger_sample = known[fresh[0]]["text"][:60] if fresh[0] in known else fresh[0]
                # Rule-aware reopen: a project-wide risk-retained rule absorbs same-class additions without reopening.
                if item["status"] == "risk_retained" and (item.get("rule_scope") == "project" or kind_of(record) in RETAINED_BY_DEFAULT):
                    for key in fresh:
                        known[key]["status"] = "risk_retained"
                    note(item, f"新增 {len(fresh)} 条同范围证据，沿用风险保留（触发：{trigger_sample}）", "系统")
                else:
                    item["status"] = "draft" if ledger == "CUS" else "action_needed"
                    note(item, f"新增 {len(fresh)} 条证据，重新打开（触发：{trigger_sample}）", "系统")
            item["decided_members"] = len(known)
            open_members = [k for k, m in known.items() if m["status"] not in DONE_MEMBER]
            if ledger == "CUS" and item["status"] in {"exported", "sent"} and not open_members:
                item["status"] = "answered"
            if item["status"] in {"closed", "risk_retained", "escalated_cus"}:
                open_members = []
            label = labels.get(item["status"], item["status"])
            if item.get("batch"):
                label += f"（批次 {item['batch']}）"
            if open_members and len(open_members) < len(known):
                label += f"（剩 {len(open_members)} / {len(known)}）"
            if item["status"] == "escalated_cus" and item.get("cus"):
                label += f" → {item['cus']}"
            record["row"][2] = label
            item["rendered"] = label
            history = [f"- {line}" for line in item.get("records", [])]
            record["details"] = [*record["details_raw"], *(["", "处理经过（系统记录）：", *history] if history else [])]
            record["status"], record["open"], record["total"] = item["status"], len(open_members), len(known)
            record["members"] = [{"key": k, "text": known[k]["text"], "status": known[k]["status"]} for k, _ in current]
            summary["evidence"] += len(known)
            if ledger == "CUS":
                summary[{"draft": "cus_draft", "exported": "cus_exported", "sent": "cus_waiting", "answered": "cus_answered"}
                        .get(item["status"], "closed")] += 1
            elif item["status"] == "action_needed":
                summary["internal_groups"] += 1
                summary["internal_open_evidence"] += len(open_members)
            elif item["status"] == "risk_retained":
                summary["risk_retained"] += 1
            else:
                summary["closed"] += 1
    for record_id, item in state["items"].items():
        if record_id not in seen and item["status"] != "closed":
            item["vanished_from"] = item["status"]
            item["status"] = "closed"
            note(item, "这轮检测不到了，自动关闭", "系统")
    write_guards(build, state)
    save_state(build, state)
    write_json(build / SUMMARY, summary)
    return summary


def write_guards(build: Path, state: dict[str, Any]) -> None:
    """Values the client has not confirmed: kb_lookup keeps them out of citable facts, check_article flags them."""
    guards = [{k: v for k, v in value.items() if k in {"id", "page", "section", "row", "column", "value", "question"}}
              for value in state["disputed_values"]
              if state["items"].get(value["id"], {}).get("status", "draft") != "closed"]
    write_json(build / GUARDS, {"guards": guards})


# ---------- commands ----------

def build_of(out: str) -> Path:
    return Path(out).resolve() / BUILD_DIRECTORY


def records_of(build: Path) -> dict[str, list[dict[str, Any]]]:
    return load_json(build / "governance.json")


def rewrite(build: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Ledgers, client sheet and guards again from governance.json and the state, without a whole build."""
    from governance import LEDGERS, render_ledger
    records = records_of(build)
    for ledger_records in records.values():
        for record in ledger_records:
            record["details"] = record.get("details_raw") or record.get("details") or []
    summary = overlay(build, records, state)
    config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    for kind, (relative, title, purpose) in LEDGERS.items():
        write_text(build / "vault" / relative, render_ledger(kind, title, purpose, config.get("project_id", "P-000"), today(), records[kind]))
    write_json(build / "governance.json", records)
    write_client_sheet(build, records, state)
    return summary


def write_client_sheet(build: Path, records: dict[str, list[dict[str, Any]]], state: dict[str, Any],
                       target: Path | None = None, only: set[str] | None = None,
                       routes: dict[str, Any] | None = None, friendly: bool | None = None) -> int:
    """客户确认表.csv holds only questions not yet exported; a batch file holds one batch. 授权 first, then 事实."""
    rows = []
    if routes is None:
        routes = load_json(build / "routes.json") if (build / "routes.json").is_file() else {}
    if friendly is None:
        config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
        friendly = bool((config.get("client_confirmation") or {}).get("friendly"))
    for record in sorted(records["CUS"], key=lambda r: r["id"]):
        status = state["items"].get(record["id"], {}).get("status", "draft")
        if (only is None and status != "draft") or (only is not None and record["id"] not in only):
            continue
        group = "一、资料公开与授权（能不能用）" if record["key"].startswith(PERMISSION) else "二、事实与参数（以哪个为准）"
        for item in record.get("items", []):
            route = routes.get(item.get("item_id", ""), {})
            place = (item.get("location") or "整份文件") if friendly else (item.get("place") or route.get("path") or item.get("location") or "")
            rows.append([group, item.get("client_id", item["sub_id"]), item["question"],
                         item.get("name") or route.get("display_name") or "", place, ""])
    rows.sort(key=lambda r: r[0])
    place_header = "在文件里的位置" if friendly else "在初筛包里的位置"
    with (target or build / "客户确认表.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["类别", "编号", "要确认的问题", "原文件名", place_header, "回答（保留 / 不用 / 文字）"])
        writer.writerows(rows)
    return len(rows)


def cited_places(build: Path) -> set[str]:
    """"文档名#位置" of every source passage an integrated page cites: those pages are refined first."""
    blocks = {b["block_id"]: b for b in load_jsonl(build / "blocks.jsonl")}
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    found = set()
    for page in (build / "curation").glob("*/page.md"):
        for block_id in re.findall(r"\{\{(?:src|表|见原文):([0-9a-f]{12})\}\}", page.read_text(encoding="utf-8")):
            block = blocks.get(block_id)
            if block and block["doc_id"] in docs:
                found.add(f"{Path(docs[block['doc_id']]['file_name']).stem}#{block['group']}")
    return found


def review(build: Path) -> Path:
    state = load_state(build)
    records = records_of(build)
    cited = cited_places(build)
    summary = load_json(build / SUMMARY) if (build / SUMMARY).is_file() else {}
    lines = ["# 待办处理单", "", f"生成于 {datetime.now():%Y-%m-%d %H:%M}。",
             f"需要做决定 {summary.get('internal_groups', 0) + summary.get('cus_draft', 0)} 组："
             f"内部 {summary.get('internal_groups', 0)} 组，待发客户 {summary.get('cus_draft', 0)} 组。"
             f"另有风险保留 {summary.get('risk_retained', 0)} 组（照用、带 ⚠，不算待办），已发待答 {summary.get('cus_waiting', 0)} 组，"
             f"涉及证据共 {summary.get('evidence', 0)} 条（审计用）。", "",
             "回复方式：在对话里说“A1–A3 按建议，A4 转客户：……，B 全部导出，B5 先不问”，Agent 会写成决定 JSON 再运行 collect。", "",
             "## 清单 A：需要内部决定", ""]
    template = []
    number = 0
    for ledger in ("ANM", "MAT"):
        for record in sorted(records[ledger], key=lambda r: r["id"]):
            item = state["items"].get(record["id"], {})
            if item.get("status") != "action_needed":
                continue
            number += 1
            advice, action = RECOMMEND.get(record["key"]) or RECOMMEND.get(kind_of(record), ("看详情后决定：关闭、风险保留或转客户", None))
            members = [m for m in record.get("members", []) if m["status"] not in DONE_MEMBER]
            members.sort(key=lambda m: not any(place in m["text"] for place in cited))
            lines.extend([f"### A{number}　{record['id']}｜{record['row'][0]}",
                          f"- 还剩 {len(members)} / {record.get('total', len(members))} 条证据；建议：{advice}"])
            hot = [m for m in members if any(place in m["text"] for place in cited)]
            if hot:
                lines.append(f"- 其中 {len(hot)} 条在整理页里被引用，先处理这些")
            lines.extend(f"  - {'★ ' if m in hot else ''}{m['text'][:120]}" for m in members[:8])
            if len(members) > 8:
                lines.append(f"  - ……另有 {len(members) - 8} 条，见台账详情")
            lines.append("")
            template.append({"ref": f"A{number}", "id": record["id"], "action": action or "keep",
                             "note": advice})
    if not number:
        lines.extend(["没有需要内部决定的事项。", ""])
    lines.extend(["## 清单 B：可以攒进下一批问客户的", ""])
    letter = 0
    for group, test in (("一、资料公开与授权（能不能用）", True), ("二、事实与参数（以哪个为准）", False)):
        rows = [r for r in sorted(records["CUS"], key=lambda r: r["id"])
                if state["items"].get(r["id"], {}).get("status") == "draft" and r["key"].startswith(PERMISSION) == test]
        if not rows:
            continue
        lines.extend([f"### {group}", ""])
        for record in rows:
            letter += 1
            lines.append(f"- B{letter}　{record['id']}｜{record['row'][0]}（{len(record.get('items', []))} 问）")
            template.append({"ref": f"B{letter}", "id": record["id"], "action": "export"})
        lines.append("")
    if not letter:
        lines.extend(["没有待汇总的客户问题。", ""])
    waiting = [r for r in records["CUS"] if state["items"].get(r["id"], {}).get("status") in {"exported", "sent", "answered"}]
    if waiting:
        lines.extend(["## 已在客户那里的（不会再导出）", ""])
        lines.extend(f"- {r['id']}｜{r['row'][0]}：{r['row'][2]}" for r in sorted(waiting, key=lambda r: r["id"]))
    write_text(build / REVIEW, "\n".join(lines).rstrip() + "\n")
    write_json(build / "governance-decisions-template.json", {"decisions": template})
    return build / REVIEW


WEBPAGE_ACTION_MAP = {
    "closed": "close", "close": "close", "auto_resolved": "close",
    "confirm_model": "close", "allow_common": "close", "apply_fix": "close",
    "risk_retained": "risk_retained", "retain_risk": "risk_retained", "keep_unconfirmed": "risk_retained",
    "escalated_cus": "to_cus", "escalate_cus": "to_cus", "to_cus": "to_cus",
    "action_needed": "reopen", "reopen": "reopen",
    "keep": "keep", "hold": "keep", "defer": "keep", "export": "export",
}


def normalize_decisions(raw: Any) -> list[dict[str, Any]]:
    """Accept both native collect JSON and governance-decisions.v1 exported by customer-confirmation-webpage --mode internal."""
    entries = raw.get("decisions", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "action" in entry:
            normalized.append(entry)
            continue
        decision = (entry.get("decision") or "").strip()
        if not decision:
            # Undecided group in a partially completed internal review HTML export: leave untouched.
            continue
        target_status = (entry.get("targetStatus") or "").strip()
        action = WEBPAGE_ACTION_MAP.get(target_status) or WEBPAGE_ACTION_MAP.get(decision) or decision
        selected = entry.get("selectedMembers")
        excluded = entry.get("excludedMembers") or []
        if isinstance(selected, list) and not selected and excluded:
            # Every member was unchecked: do not apply the group action.
            continue
        values = entry.get("values") if isinstance(entry.get("values"), dict) else {}
        question = (entry.get("question") or values.get("cus_question") or values.get("question")
                    or next((v.strip() for v in values.values() if isinstance(v, str) and v.strip()), ""))
        note_parts = [p.strip() for p in (entry.get("decisionLabel"), entry.get("applicability"), entry.get("note"))
                      if isinstance(p, str) and p.strip()]
        item: dict[str, Any] = {"id": entry.get("id"), "action": action, "note": "；".join(note_parts)}
        if isinstance(selected, list) and selected and excluded:
            item["members"] = selected
        if question:
            item["question"] = question
        normalized.append(item)
    return normalized


def next_history_batch(state: dict[str, Any]) -> str:
    return f"D-{date.today():%Y%m%d}-{len(state.get('history', [])) + 1}"


def collect(build: Path, decisions: Any, dry_run: bool = False) -> dict[str, Any]:
    """Apply decisions: {"id", "action", "members"?: [key or text…], "note"?, "question"?, "scope"?}, or governance-decisions.v1.
    action: risk_retained · close · keep · reopen · to_cus (question needed) · export (CUS, marks nothing; use cus-export).
    When dry_run=True, simulates on a copy and returns a change preview without writing any files."""
    decisions = normalize_decisions(decisions)
    state = load_state(build)
    original_state = copy.deepcopy(state)
    work_state = copy.deepcopy(state) if dry_run else state
    all_records = records_of(build)
    records = {r["id"]: r for rs in all_records.values() for r in rs}
    applied, problems, preview_items = [], [], []
    from governance import Registry
    config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    registry = Registry(build / "governance-ids.json", config.get("project_id", "P-000"))
    before_guards = {v["id"] for v in original_state["disputed_values"]
                     if original_state["items"].get(v["id"], {}).get("status", "draft") != "closed"}
    for decision in decisions:
        record_id, action = decision.get("id"), decision.get("action")
        item = work_state["items"].get(record_id)
        if not item or record_id not in records:
            problems.append(f"{record_id}：这一轮没有这个事项")
            continue
        before_status = item.get("status")
        text = decision.get("note") or ""
        scope = (decision.get("scope") or "").strip()
        targets = decision.get("members")
        changed_members = 0
        new_cus_id = ""
        if targets and action in {"close", "risk_retained"}:
            # Evidence-level: "58 of 60 pages are fixed" closes those 58 only.
            chosen = [k for k, m in item["members"].items() if k in targets or any(t and t in m["text"] for t in targets)]
            if not chosen:
                problems.append(f"{record_id}：members 一条也没对上")
                continue
            for key in chosen:
                item["members"][key]["status"] = "closed" if action == "close" else "risk_retained"
            changed_members = len(chosen)
            note(item, f"{len(chosen)} 条证据{'关闭' if action == 'close' else '风险保留'}{'：' + text if text else ''}")
            if all(m["status"] in DONE_MEMBER for m in item["members"].values()):
                item["status"] = "closed" if action == "close" else "risk_retained"
        elif action in {"close", "risk_retained", "reopen"}:
            item["status"] = {"close": "closed", "risk_retained": "risk_retained"}.get(action, "draft" if item["ledger"] == "CUS" else "action_needed")
            if scope:
                item["rule_scope"] = scope
            note(item, f"{INTERNAL.get(item['status'], CUSTOMER.get(item['status']))}{'：' + text if text else ''}")
        elif action == "to_cus":
            question = decision.get("question")
            if not question:
                problems.append(f"{record_id}：转客户要写 question（客户能回答的问题）")
                continue
            cus_id = registry.id_for("CUS", f"escalated:{record_id}:{member_key(question)}")
            if not any(e["id"] == cus_id for e in work_state["extra_cus"]):
                work_state["extra_cus"].append({"id": cus_id, "from": record_id, "question": question, "at": today()})
            item["status"], item["cus"] = "escalated_cus", cus_id
            new_cus_id = cus_id
            note(item, f"转客户 {cus_id}：{question}")
        elif action in {"keep", "export"}:
            continue
        else:
            problems.append(f"{record_id}：不认识的 action {action}")
            continue
        applied.append(record_id)
        preview_items.append({"id": record_id, "from": before_status, "to": item.get("status"),
                              "members_changed": changed_members, **({"new_cus": new_cus_id} if new_cus_id else {})})
    after_guards = {v["id"] for v in work_state["disputed_values"]
                    if work_state["items"].get(v["id"], {}).get("status", "draft") != "closed"}
    released_guards = sorted(before_guards - after_guards)
    if dry_run:
        return {
            "dry_run": True,
            "would_apply": applied,
            "problems": problems,
            "preview": {
                "groups_changed": len(preview_items),
                "members_changed": sum(p["members_changed"] for p in preview_items),
                "new_cus": [p["new_cus"] for p in preview_items if p.get("new_cus")],
                "guards_released": released_guards,
                "changes": preview_items,
            },
            "next": "去掉 --dry-run 执行正式落盘" if applied and not problems else "先修复 problems 或无变更",
        }
    batch_id = ""
    if applied:
        batch_id = next_history_batch(work_state)
        work_state["history"].append({
            "batch_id": batch_id,
            "kind": "collect",
            "at": datetime.now().isoformat(timespec="seconds"),
            "applied": applied,
            "before_items": {k: original_state["items"][k] for k in applied if k in original_state["items"]},
            "before_extra_cus": original_state["extra_cus"],
        })
    registry.save()
    save_state(build, work_state)
    summary = rewrite(build, work_state)
    return {"batch_id": batch_id, "applied": applied, "problems": problems, "guards_released": released_guards, "summary": summary,
            "next": "重跑 run_pipeline.py（或只发布）让台账进知识库" if applied else "没有可应用的决定"}


def undo(build: Path, batch_id: str | None = None) -> dict[str, Any]:
    """Revert a previous collect / auto / allow-term batch cleanly."""
    state = load_state(build)
    history = state.get("history") or []
    if not history:
        return {"undone": None, "problems": ["没有可撤销的决策批次"]}
    idx = len(history) - 1 if not batch_id else next((i for i, h in enumerate(history) if h.get("batch_id") == batch_id), -1)
    if idx < 0:
        return {"undone": None, "problems": [f"找不到批次 {batch_id}"]}
    entry = history.pop(idx)
    if entry.get("kind") == "collect":
        for rec_id in entry.get("applied", []):
            if rec_id in entry.get("before_items", {}):
                state["items"][rec_id] = entry["before_items"][rec_id]
        if "before_extra_cus" in entry:
            state["extra_cus"] = entry["before_extra_cus"]
    elif entry.get("kind") in {"auto", "allow_term", "apply_terms"}:
        path = build / "review-decisions.json"
        decisions = load_json(path) if path.is_file() else {}
        if "before_allow_terms" in entry:
            decisions["allow_terms"] = entry["before_allow_terms"]
        if "before_allow_rules" in entry:
            decisions["allow_rules"] = entry["before_allow_rules"]
        if "before_scoped_allow_terms" in entry:
            decisions["scoped_allow_terms"] = entry["before_scoped_allow_terms"]
        if "before_deny_terms" in entry:
            decisions["deny_terms"] = entry["before_deny_terms"]
        if "before_risk_retained_terms" in entry:
            decisions["risk_retained_terms"] = entry["before_risk_retained_terms"]
        write_json(path, decisions)
        if "before_items" in entry:
            for rec_id, prev in entry["before_items"].items():
                state["items"][rec_id] = prev
    save_state(build, state)
    summary = rewrite(build, state) if (build / "governance.json").is_file() else {}
    return {"undone": entry["batch_id"], "kind": entry.get("kind"), "summary": summary,
            "next": "若撤销了词项放行，请重跑 run_pipeline.py --from guard"}


def flag_value(build: Path, page: str, section: str, value: str, question: str, row: str = "", column: str = "",
               vault: Path | None = None) -> dict[str, Any]:
    from governance import Registry
    config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    registry = Registry(build / "governance-ids.json", config.get("project_id", "P-000"))
    cus_id = registry.id_for("CUS", f"value:{page}|{section}|{row}|{column}|{value}")
    registry.save()
    state = load_state(build)
    if not any(v["id"] == cus_id for v in state["disputed_values"]):
        state["disputed_values"].append({"id": cus_id, "page": page, "section": section, "row": row, "column": column,
                                         "value": value, "question": question, "at": today()})
    save_state(build, state)
    summary = rewrite(build, state)
    if vault:  # Writers read the vault: the guard works before the next publish.
        target = vault / "05_数据与审核/70_构建状态"
        write_json(target / GUARDS, load_json(build / GUARDS))
    return {"cus": cus_id, "guards": len(load_json(build / GUARDS)["guards"]), "summary": summary}


def cus_export(build: Path, ids: list[str] | None) -> dict[str, Any]:
    state = load_state(build)
    records = records_of(build)
    drafts = [r["id"] for r in records["CUS"] if state["items"].get(r["id"], {}).get("status") == "draft"]
    chosen = [i for i in (ids or drafts) if i in drafts]
    if not chosen:
        return {"batch": None, "exported": 0, "next": "没有待汇总的客户问题"}
    batch = f"B-{date.today():%Y%m%d}-{len(state['cus_batches']) + 1}"
    target = build / f"客户确认表-{batch}.csv"
    count = write_client_sheet(build, records, state, target, set(chosen))
    state["cus_batches"][batch] = {"status": "exported", "items": chosen, "exported_at": today(), "file": target.name}
    for record_id in chosen:
        state["items"][record_id].update(status="exported", batch=batch)
        note(state["items"][record_id], f"导出到批次 {batch}，等待发送")
    save_state(build, state)
    rewrite(build, state)
    return {"batch": batch, "exported": len(chosen), "questions": count, "file": str(target),
            "next": f"发给客户后运行 cus-sent --batch {batch}；客户回复后用 apply_answers.py --answers <回复的表> 读回"}


def cus_sent(build: Path, batch: str) -> dict[str, Any]:
    state = load_state(build)
    if batch not in state["cus_batches"]:
        raise SystemExit(f"没有批次 {batch}")
    state["cus_batches"][batch].update(status="sent", sent_at=today())
    for record_id in state["cus_batches"][batch]["items"]:
        item = state["items"].get(record_id)
        if item and item["status"] == "exported":
            item["status"] = "sent"
            note(item, f"批次 {batch} 已发给客户")
    save_state(build, state)
    rewrite(build, state)
    return {"batch": batch, "items": len(state["cus_batches"][batch]["items"])}


def mark_answered(build: Path, sub_ids: list[str]) -> int:
    """apply_answers.py calls this: an answered sub-question waits for a person to verify, then closes."""
    state = load_state(build)
    marked = 0
    for item in state["items"].values():
        for key, member in item.get("members", {}).items():
            if key in sub_ids and member["status"] != "answered":
                member["status"] = "answered"
                marked += 1
        if item["ledger"] == "CUS" and item.get("members") and all(m["status"] in DONE_MEMBER for m in item["members"].values()) \
                and item["status"] in {"draft", "exported", "sent"}:
            item["status"] = "answered"
            note(item, "客户已回复，待核验")
    save_state(build, state)
    return marked


def sync(build: Path, vault: Path) -> dict[str, Any]:
    """Read what people wrote in the published ledgers: a stage that differs from what we rendered is theirs."""
    from governance import LEDGERS, parse_ledger
    state = load_state(build)
    taken = 0
    for kind, (relative, _, _) in LEDGERS.items():
        candidates = [vault / relative, *(vault / Path(relative).parent).glob("02_知识库构建_*.md")]
        path = next((p for p in candidates if p.is_file()), None)
        if not path:
            continue
        header, rows, notes = parse_ledger(path.read_text(encoding="utf-8"))
        stage_at = header.index("当前阶段") if "当前阶段" in header else None
        for record_id, cells in rows.items():
            stage = cells[stage_at] if stage_at is not None and stage_at < len(cells) else ""
            item = state["items"].get(record_id)
            if item and (not stage or stage == item.get("rendered")):
                continue
            status = mapped(kind, stage)
            if not status:
                continue
            item = state["items"].setdefault(record_id, {"ledger": kind, "members": {}, "records": []})
            if item.get("status") != status:
                item["status"] = status
                if status == "sent":
                    item.setdefault("batch", "以前已发")
                note(item, f"台账里写的阶段“{stage}”", "human")
                taken += 1
            for line in notes.get(record_id, [])[1:]:
                if line.strip():
                    note(item, line.strip().lstrip("- "), "human")
    save_state(build, state)
    return {"taken": taken}


def mapped(kind: str, stage: str) -> str | None:
    if re.search(r"已关闭|已完成|结案|已解决|无需|可结案", stage):
        return "closed"
    if kind != "CUS" and "保留" in stage:
        return "risk_retained"
    if kind == "CUS" and re.search(r"等待客户|已发|待客户回复", stage):
        return "sent"
    if kind == "CUS" and re.search(r"已答|已回复|待核验", stage):
        return "answered"
    return None


def vocabulary() -> set[str]:
    if not VOCABULARY.is_file():
        return set()
    return {line.strip().casefold() for line in VOCABULARY.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


# Certification, standard and testing bodies are compliance facts, never plain words.
CERT_LIKE = re.compile(
    r"^(?:"
    r"CE|CB|UL|cUL|cULus|CSA|ETL|cETL|FCC|ICASA|KC|PSE|RCM|TELEC|NCC|BSMI|SRRC|CCC|CQC|BIS|WPC|UKCA|EAC|NOM|ANATEL|INMETRO|"
    r"T[ÜU]V|Tüv|VDE|GS|SGS|BV|ITS|Intertek|DEKRA|DNV|ABS|CCS|LR|GL|RINA|NFPA|FM|"
    r"RoHS|ROHS|REACH|WEEE|ErP|ERP|ATEX|IECEx|PED|LVD|EMC|FDA|NSF|WRAS|ACS|KTW|DVGW|MSDS|UN\s?38\.?3|"
    r"BSCI|SEDEX|SMETA|WRAP|SA\s?8000|OEKO(?:-?TEX)?|Oeko-?Tex|GRS|RCS|GOTS|FSC|BCI|ZDHC|HIGG|"
    r"(?:ISO|IEC|EN|DIN|ANSI|ASME|ASTM|API|AWWA|MSS|JIS|BS|GB|GB/T|JB|JB/T|HG|SH|NACE|SIL|NFPA)(?:[-/\s]?[A-Z0-9.]{1,10})?"
    r")$"
)
RESOLVED_TEXT_ERROR = re.compile(r"已修正|已逐图恢复|已按原图恢复|已恢复完整|与原图一致|无实质差异|纯排版差异|空白页")
STILL_OPEN_TEXT_ERROR = re.compile(r"仍待|未解决|疑似印错|看不清|待确认|无法确认|尚需|需客户")
TERM_BATCH_SIZE = 200


PROTECTED_BRAND_OR_PLATFORM = {
    "windows", "apple", "android", "linux", "google", "chrome", "microsoft", "intel",
    "bluetooth", "wifi", "amazon", "oracle", "adobe", "samsung", "sony", "disney", "marvel",
}


def _matches_plain_vocab(term: str, words: set[str]) -> bool:
    """Exact match or simple English inflection of a plain vocabulary word (never certs/standards/codes/platforms)."""
    if CERT_LIKE.match(term) or any(ch.isdigit() for ch in term):
        return False
    low = term.casefold()
    if low in PROTECTED_BRAND_OR_PLATFORM:
        return False
    if low in words:
        return True
    if not low.isalpha() or len(low) < 4:
        return False
    candidates = []
    if low.endswith("ies") and len(low) > 4:
        candidates.append(low[:-3] + "y")
    for suffix in ("es", "s", "ed", "d", "ing", "er", "est", "ly"):
        if low.endswith(suffix) and len(low) - len(suffix) >= 3:
            stem = low[:-len(suffix)]
            candidates.append(stem)
            candidates.append(stem + "e")
            if len(stem) >= 2 and stem[-1] == stem[-2]:
                candidates.append(stem[:-1])
    return any(c in words for c in candidates)


def auto(build: Path) -> dict[str, Any]:
    """Auto-settle plain vocabulary words (with inflections) and already-resolved text_errors, then export remaining terms in batches for Agent triage."""
    queue = load_jsonl(build / "review-queue.jsonl")
    words = vocabulary()
    path = build / "review-decisions.json"
    decisions = load_json(path) if path.is_file() else {}
    before_terms = list(decisions.get("allow_terms", []))
    before_rules = copy.deepcopy(decisions.get("allow_rules", {}))
    current = decisions.setdefault("allow_terms", [])
    current_cf = {t.casefold() for t in current}
    denied_cf = {t.casefold() for t in decisions.get("deny_terms", [])}
    retained_cf = {t.casefold() for t in decisions.get("risk_retained_terms", [])}
    allowed = [e["term"] for e in queue if e.get("kind") == "unverified_name"
               and _matches_plain_vocab(e["term"], words)
               and e["term"].casefold() not in denied_cf]
    added = [t for t in allowed if t.casefold() not in current_cf]
    if added:
        current.extend(added)
        current_cf.update(t.casefold() for t in added)
        decisions.setdefault("allow_rules", {})[VOCABULARY_VERSION] = sorted(set(decisions.get("allow_rules", {}).get(VOCABULARY_VERSION, []) + added))
        write_json(path, decisions)

    # Auto-close text_errors members that already record a completed fix or cosmetic-only match.
    state = load_state(build)
    before_items: dict[str, Any] = {}
    closed_text_errors = 0
    for rec_id, item in state["items"].items():
        if item.get("key") != "text_errors" or item.get("status") != "action_needed":
            continue
        newly_closed = []
        for mkey, member in item.get("members", {}).items():
            if member.get("status") in DONE_MEMBER:
                continue
            txt = member.get("text") or ""
            if RESOLVED_TEXT_ERROR.search(txt) and not STILL_OPEN_TEXT_ERROR.search(txt):
                newly_closed.append(mkey)
        if newly_closed:
            before_items[rec_id] = copy.deepcopy(item)
            for mkey in newly_closed:
                item["members"][mkey]["status"] = "closed"
            closed_text_errors += len(newly_closed)
            note(item, f"自动关闭 {len(newly_closed)} 条已核对/已恢复/无实质差异记录", "系统")
            if all(m["status"] in DONE_MEMBER for m in item["members"].values()):
                item["status"] = "closed"

    batch_id = ""
    if added or closed_text_errors:
        batch_id = next_history_batch(state)
        state["history"].append({
            "batch_id": batch_id,
            "kind": "auto",
            "rule": VOCABULARY_VERSION,
            "at": datetime.now().isoformat(timespec="seconds"),
            "added": added,
            "closed_text_errors": closed_text_errors,
            "before_allow_terms": before_terms,
            "before_allow_rules": before_rules,
            "before_items": before_items,
        })
        save_state(build, state)
        if closed_text_errors and (build / "governance.json").is_file():
            rewrite(build, state)

    # Export remaining unjudged terms in batches so the Agent (or parallel subagents) triages them BEFORE asking the user.
    remaining = [
        {"term": e["term"], "blocks": e.get("blocks", 0), "documents": e.get("documents", 0), "examples": e.get("examples", [])}
        for e in queue
        if e.get("kind") == "unverified_name"
        and e["term"].casefold() not in current_cf
        and e["term"].casefold() not in denied_cf
        and e["term"].casefold() not in retained_cf
    ]
    triage_dir = build / "triage-terms"
    if triage_dir.is_dir():
        for old in triage_dir.glob("batch-*.jsonl"):
            old.unlink()
    batch_files = []
    if remaining:
        triage_dir.mkdir(parents=True, exist_ok=True)
        for i in range(0, len(remaining), TERM_BATCH_SIZE):
            bf = triage_dir / f"batch-{i // TERM_BATCH_SIZE + 1:03d}.jsonl"
            bf.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in remaining[i:i + TERM_BATCH_SIZE]), encoding="utf-8")
            batch_files.append(str(bf))
    return {
        "batch_id": batch_id,
        "allowed": added,
        "closed_text_errors": closed_text_errors,
        "remaining_terms": len(remaining),
        "term_batches": batch_files,
        "rule": VOCABULARY_VERSION,
        "next": (
            f"还有 {len(remaining)} 个剩余词条已分 {len(batch_files)} 批写入 {triage_dir}：由 Agent（可并行分给子 Agent）批量判定后运行 apply-terms，再 --from guard 重跑；不要直接把几百/几千个词抛给用户"
            if remaining else "重跑 run_pipeline.py --from guard（或整条流水线）让放行生效" if added else "没有可按词典放行的词"
        ),
    }


def apply_terms(build: Path, decisions_input: dict[str, Any]) -> dict[str, Any]:
    """Apply Agent batch term-triage decisions: {"allow": [...], "deny": [...], "risk_retained": [...], "human": [...]}."""
    path = build / "review-decisions.json"
    decisions = load_json(path) if path.is_file() else {}
    before_allow = list(decisions.get("allow_terms", []))
    before_rules = copy.deepcopy(decisions.get("allow_rules", {}))
    before_deny = list(decisions.get("deny_terms", []))
    before_retained = list(decisions.get("risk_retained_terms", []))

    allow_in = [str(t).strip() for t in decisions_input.get("allow", []) if str(t).strip()]
    deny_in = [str(t).strip() for t in decisions_input.get("deny", []) if str(t).strip()]
    retained_in = [str(t).strip() for t in decisions_input.get("risk_retained", []) if str(t).strip()]
    human_in = [str(t).strip() for t in decisions_input.get("human", []) if str(t).strip()]

    cur_allow = decisions.setdefault("allow_terms", [])
    allow_cf = {t.casefold() for t in cur_allow}
    added_allow = [t for t in allow_in if t.casefold() not in allow_cf and not CERT_LIKE.match(t)]
    cur_allow.extend(added_allow)
    if added_allow:
        decisions.setdefault("allow_rules", {})["ai-triage"] = sorted(set(decisions.get("allow_rules", {}).get("ai-triage", []) + added_allow))

    cur_deny = decisions.setdefault("deny_terms", [])
    deny_cf = {t.casefold() for t in cur_deny}
    added_deny = [t for t in deny_in if t.casefold() not in deny_cf]
    cur_deny.extend(added_deny)

    # Any CERT_LIKE term mistakenly put in allow is safely diverted to risk_retained instead of silently allowed.
    cert_diverted = [t for t in allow_in if CERT_LIKE.match(t)]
    cur_retained = decisions.setdefault("risk_retained_terms", [])
    retained_cf = {t.casefold() for t in cur_retained}
    added_retained = [t for t in [*retained_in, *cert_diverted] if t.casefold() not in retained_cf]
    cur_retained.extend(added_retained)

    batch_id = ""
    if added_allow or added_deny or added_retained:
        write_json(path, decisions)
        state = load_state(build)
        batch_id = next_history_batch(state)
        state["history"].append({
            "batch_id": batch_id,
            "kind": "apply_terms",
            "at": datetime.now().isoformat(timespec="seconds"),
            "added_allow": len(added_allow),
            "added_deny": len(added_deny),
            "added_retained": len(added_retained),
            "before_allow_terms": before_allow,
            "before_allow_rules": before_rules,
            "before_deny_terms": before_deny,
            "before_risk_retained_terms": before_retained,
        })
        save_state(build, state)
    return {
        "batch_id": batch_id,
        "added_allow": len(added_allow),
        "added_deny": len(added_deny),
        "added_retained": len(added_retained),
        "cert_diverted_to_retained": cert_diverted,
        "remaining_for_human": human_in,
        "next": "重跑 run_pipeline.py --from guard 刷新台账（风险保留词保留 ⚠ 但退出待办）",
    }


VALID_SCOPE = re.compile(r"^(?:project|industry:[A-Za-z0-9_-]+|doc:[A-Za-z0-9_-]+)$")


def allow_term(build: Path, term: str, scope: str = "project", rule_version: str = "manual") -> dict[str, Any]:
    """Record a scoped allow rule (project, industry:<name>, or doc:<doc_id>) with undo history."""
    term = term.strip()
    scope = scope.strip() or "project"
    if not term:
        return {"problems": ["term 不能为空"]}
    if not VALID_SCOPE.match(scope):
        return {"problems": [f"scope 只能是 project、industry:<行业> 或 doc:<DOC编号>：{scope}"]}
    path = build / "review-decisions.json"
    decisions = load_json(path) if path.is_file() else {}
    before_terms = list(decisions.get("allow_terms", []))
    before_scoped = copy.deepcopy(decisions.get("scoped_allow_terms", []))
    scoped = decisions.setdefault("scoped_allow_terms", [])
    key = (term.casefold(), scope)
    if not any((e.get("term", "").casefold(), e.get("scope")) == key for e in scoped):
        scoped.append({"term": term, "scope": scope, "rule_version": rule_version, "at": today()})
    if scope == "project" and term not in decisions.setdefault("allow_terms", []):
        decisions["allow_terms"].append(term)
    write_json(path, decisions)
    state = load_state(build)
    batch_id = next_history_batch(state)
    state["history"].append({
        "batch_id": batch_id,
        "kind": "allow_term",
        "at": datetime.now().isoformat(timespec="seconds"),
        "term": term,
        "scope": scope,
        "before_allow_terms": before_terms,
        "before_scoped_allow_terms": before_scoped,
    })
    save_state(build, state)
    return {"batch_id": batch_id, "term": term, "scope": scope, "rule_version": rule_version,
            "next": "重跑 run_pipeline.py --from guard 使作用域放行生效"}


def format_webpage_member(text: str, hot: bool = False) -> dict[str, str]:
    readable = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", text)
    readable = re.sub(r"\[\[([^\]]+)\]\]", r"\1", readable).strip()
    head, _, body = readable.partition("：")
    return {"label": ("★ " if hot else "") + head.strip(), "detail": body.strip()}


def _load_blocks_and_docs(build: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    docs_list = load_jsonl(build / "docs.jsonl") if (build / "docs.jsonl").is_file() else []
    docs_by_id = {d["doc_id"]: d for d in docs_list if d.get("doc_id")}
    docs_by_item = {d["item_id"]: d for d in docs_list if d.get("item_id")}
    docs_by_title = {d["title"]: d for d in docs_list if d.get("title")}
    blocks_by_doc: dict[str, list[dict[str, Any]]] = {}
    if (build / "blocks.jsonl").is_file():
        for b in load_jsonl(build / "blocks.jsonl"):
            if b.get("doc_id"):
                blocks_by_doc.setdefault(b["doc_id"], []).append(b)
    pages_by_item: dict[str, dict[str, Any]] = {}
    pages_path = build / "website" / "pages.json"
    if pages_path.is_file():
        for p in load_json(pages_path):
            if isinstance(p, dict) and p.get("page_id"):
                pages_by_item[p["page_id"]] = p
    return docs_by_item, docs_by_title, blocks_by_doc, pages_by_item


def _removal_evidence_for_member(label: str, docs_by_title: dict[str, dict[str, Any]],
                                 blocks_by_doc: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Attach representative removed sentence, location, restore benefit, and suggestion for heavy_removal members."""
    clean_title = label.lstrip("★ ").strip()
    doc = docs_by_title.get(clean_title)
    if not doc:
        return {}
    removed = [b for b in blocks_by_doc.get(doc["doc_id"], [])
               if b.get("status") == "removed" and (b.get("text") or "").strip()]
    if not removed:
        return {}
    first = removed[0]
    snippet = re.sub(r"\s+", " ", (first.get("text") or "").strip())[:160]
    if len(removed) > 1:
        second = re.sub(r"\s+", " ", (removed[1].get("text") or "").strip())[:80]
        if second and second != snippet:
            snippet = f"{snippet}；另见：{second}"
    return {
        "removedText": snippet,
        "sourceName": doc["title"],
        "location": first.get("locator") or first.get("group") or "",
        "restoreBenefit": "若属产品能力或技术参数而非敏感/装饰内容，补回后可补充对应产品或公司事实",
        "suggestion": "核对原句：属电话/邮箱/价格/装饰图标则保持删除并关闭；属误删产品信息则放行重跑",
    }


def _real_evidence_for_cus(item_id: str, location: str, docs_by_item: dict[str, dict[str, Any]],
                           blocks_by_doc: dict[str, list[dict[str, Any]]],
                           pages_by_item: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Pull genuine verbatim excerpt (and website URL/capture date) from blocks.jsonl / website snapshot."""
    result: dict[str, Any] = {}
    page = pages_by_item.get(item_id)
    if page and page.get("url"):
        result["sourceUrl"] = page["url"]
        if page.get("fetched_at"):
            result["capturedAt"] = str(page["fetched_at"])[:10]
    doc = docs_by_item.get(item_id)
    if not doc:
        return result
    doc_blocks = [b for b in blocks_by_doc.get(doc["doc_id"], [])
                  if b.get("status") != "removed" and (b.get("text") or "").strip()]
    if not doc_blocks:
        return result
    loc = (location or "").strip()
    matched = [b for b in doc_blocks if loc and loc not in {"整份文件", "整个文件"}
               and (b.get("group") == loc or b.get("locator") == loc or loc in (b.get("group") or "") or loc in (b.get("locator") or ""))]
    chosen = matched or doc_blocks[:2]
    excerpt = "\n".join(re.sub(r"\s+", " ", (b.get("text") or "").strip()) for b in chosen[:2])[:320].strip()
    if excerpt:
        result["excerpt"] = excerpt
    return result


def export_internal_webpage_json(build: Path, output: Path | None = None) -> Path:
    """Export an internal-review-input.v1 JSON directly consumable by customer-confirmation-webpage --mode internal."""
    state = load_state(build)
    records = records_of(build)
    config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    decisions_doc = load_json(build / "review-decisions.json") if (build / "review-decisions.json").is_file() else {}
    cited = cited_places(build)
    project_id = config.get("project_id", "P-000")
    _, docs_by_title, blocks_by_doc, _ = _load_blocks_and_docs(build)

    issues: list[dict[str, Any]] = []
    risk_retained: list[dict[str, Any]] = []
    auto_resolved: list[dict[str, Any]] = []

    for rule_ver, words in sorted((decisions_doc.get("allow_rules") or {}).items()):
        if words:
            auto_resolved.append({
                "id": f"AUTO-{rule_ver}",
                "ledger": "TERM",
                "title": f"{len(words)} 个普通词按版本化词典自动放行",
                "ruleVersion": rule_ver,
                "evidenceCount": len(words),
                "reason": "精确命中纯普通词典，排除品牌、平台、认证、标准、检测机构和 IP 名。",
                "members": list(words[:100]),
            })

    for ledger in ("ANM", "MAT"):
        for record in sorted(records.get(ledger, []), key=lambda r: r["id"]):
            item = state["items"].get(record["id"], {})
            status = item.get("status", "action_needed")
            if status not in {"risk_retained", "action_needed"}:
                continue
            # Older builds cached truncated member text, but kept the full evidence in the ledger details.
            full_pairs = members_of(record)
            full_text = dict(full_pairs)
            raw_members = record.get("members") or [{"key": k, "text": t, "status": "open"} for k, t in full_pairs]
            members = [
                {**member, "text": full_text.get(member["key"], member["text"])}
                for member in raw_members
            ]
            if status == "risk_retained":
                risk_retained.append({
                    "id": record["id"],
                    "ledger": ledger,
                    "title": record["row"][0],
                    "evidenceCount": record.get("total", len(members) or 1),
                    "reason": "已归入风险保留（不计入待办），内容仍带 ⚠ 提示。",
                    "members": [format_webpage_member(m["text"]) for m in members],
                })
                continue
            advice, rec_action = RECOMMEND.get(record["key"]) or RECOMMEND.get(kind_of(record), ("看详情后决定：关闭、风险保留或转客户", None))
            open_members = [m for m in members if m["status"] not in DONE_MEMBER]
            open_members.sort(key=lambda m: not any(place in m["text"] for place in cited))
            rec_choice = {"closed": "close", "risk_retained": "risk_retained"}.get(rec_action or "", "")
            choices = [
                {"value": "close", "label": "确认已处理并关闭", "detail": "完成核对或整组保留结论后关闭待办", "targetStatus": "closed"},
                {"value": "risk_retained", "label": "归入风险保留（带 ⚠ 退出待办）", "detail": "内容继续使用并保留 ⚠，不再计入待办", "targetStatus": "risk_retained"},
                {"value": "to_cus", "label": "转客户确认（进待汇总 CUS）", "detail": "原件存疑或需客户授权时填写客户问法", "targetStatus": "escalated_cus", "correctionField": "cus_question"},
                {"value": "keep", "label": "暂留待内部处理", "detail": "本轮暂不结案，保留在待办列表", "targetStatus": "action_needed"},
            ]
            is_removal_check = record["key"] == "heavy_removal" or kind_of(record) in {"heavy_removal", "removal"}
            formatted_members = []
            for m in open_members:
                entry_m: dict[str, Any] = {
                    "memberId": m["key"],
                    **format_webpage_member(m["text"], any(p in m["text"] for p in cited)),
                    "selected": True,
                }
                if is_removal_check:
                    entry_m.update(_removal_evidence_for_member(entry_m["label"], docs_by_title, blocks_by_doc))
                formatted_members.append(entry_m)
            issue_entry: dict[str, Any] = {
                "id": record["id"],
                "rootKey": record["key"],
                "ledger": ledger,
                "category": kind_of(record),
                "product": ledger,
                "title": record["row"][0],
                "question": advice,
                "evidenceCount": len(open_members) or 1,
                "owner": record["row"][3] if len(record["row"]) > 3 else "知识库专员",
                "originalValues": [["剩余证据", f"{len(open_members)} / {record.get('total', len(open_members))}"], ["根因键", record["key"]]],
                "members": formatted_members,
                "fields": [{"key": "cus_question", "label": "转客户问法（仅选“转客户确认”时填写）", "placeholder": "写一句客户能直接回答的问题"}],
                "choices": choices,
            }
            if rec_choice:
                issue_entry["recommendedAction"] = rec_choice
                issue_entry["recommendedReason"] = advice
            issues.append(issue_entry)

    cus_items = []
    draft_groups = exported_groups = sent_groups = 0
    for record in sorted(records.get("CUS", []), key=lambda r: r["id"]):
        st = state["items"].get(record["id"], {}).get("status", "draft")
        if st == "closed":
            continue
        if st == "draft":
            draft_groups += 1
        elif st == "exported":
            exported_groups += 1
        elif st == "sent":
            sent_groups += 1
        cus_items.append({
            "id": record["id"],
            "title": record["row"][0],
            "status": CUSTOMER.get(st, st),
            "batchId": state["items"].get(record["id"], {}).get("batch", ""),
            "kind": "授权许可" if record["key"].startswith(PERMISSION) else "事实参数",
            "evidenceCount": len(record.get("items", [])) or 1,
        })

    payload = {
        "schema": "internal-review-input.v1",
        "mode": "internal",
        "projectId": project_id,
        "revision": f"{date.today():%Y%m%d}-{len(state.get('history', [])) + 1}",
        "company": config.get("company", ""),
        "createdAt": today(),
        "title": f"{project_id} 内部待办决策页",
        "autoResolved": auto_resolved,
        "riskRetained": risk_retained,
        "cusSummary": {
            "draftGroups": draft_groups,
            "exportedGroups": exported_groups,
            "sentGroups": sent_groups,
            "totalGroups": len(cus_items),
            "items": cus_items,
        },
        "issues": issues,
    }
    target = output or (build / INTERNAL_WEBPAGE_JSON)
    write_json(target, payload)
    return target


def _permission_list_group(record_key: str) -> dict[str, str] | None:
    if record_key == "website_cases":
        return {"id": "perm-website-cases", "title": "官网案例页授权清单",
                "description": "请确认以下官网案例页的文字和配图能否纳入知识库与对外使用"}
    if record_key.startswith("media:"):
        return {"id": "perm-media", "title": "图片与视频素材授权清单",
                "description": "请确认以下图片/视频素材能否用于本项目官网与对外内容"}
    if record_key.startswith("pending:"):
        return {"id": "perm-docs", "title": "资料文件授权清单",
                "description": "请确认以下文档资料能否纳入知识库与对外使用"}
    return None


def export_customer_webpage_json(build: Path, batch: str, chosen_ids: list[str], output: Path | None = None) -> Path:
    """Export a customer-review-input.v1 draft JSON for customer-confirmation-webpage --mode customer."""
    state = load_state(build)
    records = records_of(build)
    routes = load_json(build / "routes.json") if (build / "routes.json").is_file() else {}
    config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    docs_by_item, _, blocks_by_doc, pages_by_item = _load_blocks_and_docs(build)
    project_id = config.get("project_id", "P-000")
    friendly = bool((config.get("client_confirmation") or {}).get("friendly"))
    issues: list[dict[str, Any]] = []
    chosen_set = set(chosen_ids)
    for record in sorted(records.get("CUS", []), key=lambda r: r["id"]):
        if record["id"] not in chosen_set:
            continue
        # Never export closed items even when re-exporting an older batch.
        if state["items"].get(record["id"], {}).get("status") == "closed":
            continue
        is_perm = record["key"].startswith(PERMISSION)
        list_group = _permission_list_group(record["key"]) if is_perm else None
        is_cert = (not is_perm) and bool(re.search(r"证书|认证|资质|检测报告|测试型号", f"{record['row'][0]} {record['key']}"))
        for idx, sub in enumerate(record.get("items", []), start=1):
            item_id = (sub.get("item_id") or "").split(",")[0]
            route = routes.get(item_id, {})
            src_name = sub.get("name") or route.get("display_name") or record["row"][0]
            src_loc = sub.get("location") or "整份文件"
            raw_rel = (route.get("source_relpath") or route.get("path") or "").replace("\\", "/").lstrip("/")
            source_path = raw_rel if raw_rel and not re.match(r"^[A-Za-z]:", raw_rel) and ".." not in raw_rel.split("/") else None
            answer_id = sub.get("client_id", sub["sub_id"])
            card_id = record["id"] if len(record.get("items", [])) == 1 else f"{record['id']}-{idx}"
            real_ev = _real_evidence_for_cus(item_id, src_loc, docs_by_item, blocks_by_doc, pages_by_item)
            ev_entry: dict[str, Any] = {"caption": f"{src_name} · {src_loc}", "sourceName": src_name, "location": src_loc}
            if real_ev.get("sourceUrl"):
                ev_entry["sourceUrl"] = real_ev["sourceUrl"]
                ev_entry["capturedAt"] = real_ev.get("capturedAt") or today()
            if real_ev.get("excerpt"):
                ev_entry["excerpt"] = real_ev["excerpt"]
            if is_perm:
                choices = [
                    {"value": "whole", "label": "可以公开使用", "detail": "整份资料可用于知识库与对外内容", "csvAnswer": "保留"},
                    {"value": "partial", "label": "仅部分可用", "detail": "请填写允许使用的范围或限制", "requiresCorrection": True, "csvAnswer": ""},
                    {"value": "exclude", "label": "不使用", "detail": "整份排除，不对外引用", "csvAnswer": "不用"},
                    {"value": "pending", "label": "还需内部确认", "detail": "暂不确定，稍后答复", "csvAnswer": ""},
                ]
                fields = [{"key": "scope", "label": "允许使用的范围或限制", "placeholder": "请注明可用页码、章节或限制条件"}]
                purpose = "确认该资料/素材是否可用于本项目官网及对外内容"
                expected_answer = "请选择“可以公开使用”“仅部分可用（注明范围）”或“不使用”"
                evidence = [ev_entry]
            elif is_cert:
                choices = [
                    {"value": "confirm", "label": "可以用这句话", "detail": "按拟用对外表述发布"},
                    {"value": "revise", "label": "请按我提供的说法写", "detail": "请填写您希望对外展示的说法或适用型号", "requiresCorrection": True},
                    {"value": "exclude", "label": "这次先不提", "detail": "本次建站与文章暂不引用该项"},
                    {"value": "pending", "label": "稍后回复", "detail": "还需内部核实，稍后答复"},
                ]
                fields = [{"key": "correct_value", "label": "您希望对外展示的说法", "placeholder": "请写明准确的对外表述或适用型号"}]
                purpose = "确认资质/检测信息的对外表述与适用范围"
                expected_answer = "请确认拟用说法是否合适，或直接给出您希望对外展示的表述"
                if "excerpt" not in ev_entry:
                    ev_entry["excerpt"] = f"来源文件：{src_name}（{src_loc}），具体原件内容请对照原文件核对。"
                evidence = [ev_entry]
            else:
                choices = [
                    {"value": "confirm", "label": "确认以原件为准", "detail": "原件数值或说法正确，可对外引用"},
                    {"value": "revise", "label": "按我填写的内容修改", "detail": "请填写正确数值、单位或说法", "requiresCorrection": True},
                    {"value": "exclude", "label": "暂不使用此内容", "detail": "对外资料暂不采用"},
                    {"value": "pending", "label": "还需内部确认", "detail": "暂不确定，稍后答复"},
                ]
                fields = [{"key": "correct_value", "label": "正确数值 / 说明", "placeholder": "请写明正确数值、单位或适用型号"}]
                purpose = "确认产品/公司参数与事实的对外口径"
                expected_answer = "请确认以原件为准、填写正确数值/单位，或选择暂不使用"
                if "excerpt" not in ev_entry:
                    ev_entry["excerpt"] = f"来源文件：{src_name}（{src_loc}），请对照原件确认对应条目。"
                evidence = [ev_entry]
            entry: dict[str, Any] = {
                "id": card_id,
                "cusId": record["id"],
                "answerId": answer_id,
                "kind": "usage" if is_perm else "fact",
                "product": "资料授权" if is_perm else "参数与事实核对",
                "title": record["row"][0],
                "question": sub["question"],
                "ledgerQuestion": sub["question"],
                "purpose": purpose,
                "expectedAnswer": expected_answer,
                "originalValues": [],
                "sourceName": src_name,
                "sourceLocation": src_loc,
                "fields": fields,
                "choices": choices,
                "evidence": evidence,
            }
            if list_group:
                entry["listGroup"] = list_group
            if source_path and not real_ev.get("sourceUrl"):
                entry["sourcePath"] = source_path
            issues.append(entry)
    payload = {
        "schema": "customer-review-input.v1",
        "projectId": project_id,
        "revision": batch,
        "company": config.get("company") or project_id,
        "createdAt": today(),
        "title": f"{config.get('company') or project_id} 资料确认（{batch}）",
        "exportPrefix": f"{project_id}-{batch}",
        "csv": {
            "headers": ["编号", "要确认的问题", "原文件名", "在文件里的位置" if friendly else "在初筛包里的位置", "回答（保留 / 不用 / 文字）"],
            "columns": ["answerId", "question", "sourceName", "sourceLocation", "answerText"],
        },
        "issues": issues,
    }
    target = output or (build / f"customer-review-input-{batch}.json")
    write_json(target, payload)
    return target


def migrate(build: Path, vault: Path) -> dict[str, Any]:
    """Migrate an existing vault's ledgers and review-decisions into governance-state.v2 idempotently."""
    synced = sync(build, vault)
    state = load_state(build)
    state["schema"] = STATE_SCHEMA
    decisions_path = build / "review-decisions.json"
    migrated_rules = 0
    if decisions_path.is_file():
        decisions = load_json(decisions_path)
        scoped = decisions.setdefault("scoped_allow_terms", [])
        existing = {(e.get("term", "").casefold(), e.get("scope")) for e in scoped}
        for term in decisions.get("allow_terms", []):
            if (term.casefold(), "project") not in existing:
                scoped.append({"term": term, "scope": "project", "rule_version": "migrated", "at": today()})
                existing.add((term.casefold(), "project"))
                migrated_rules += 1
        if migrated_rules:
            write_json(decisions_path, decisions)
    save_state(build, state)
    return {"schema": STATE_SCHEMA, "synced_from_ledgers": synced["taken"], "migrated_allow_rules": migrated_rules}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decisions over the CUS / MAT / ANM ledgers.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "auto", "apply-terms", "review", "collect", "undo", "allow-term", "flag-value", "cus-export", "cus-sent", "migrate"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True, help="Run folder")
        if name in {"sync", "flag-value", "migrate"}:
            p.add_argument("--vault", required=name in {"sync", "migrate"})
        if name == "apply-terms":
            p.add_argument("--decisions", required=True, help="JSON: {\"allow\": [...], \"deny\": [...], \"risk_retained\": [...], \"human\": [...]}")
        if name == "review":
            p.add_argument("--webpage-json", nargs="?", const="__DEFAULT__", default=None,
                           help="Also export internal-review-input.v1 JSON for customer-confirmation-webpage --mode internal")
        if name == "collect":
            p.add_argument("--decisions", required=True, help="JSON: {\"decisions\": [{\"id\", \"action\", …}]} or governance-decisions.v1")
            p.add_argument("--dry-run", action="store_true", help="Preview changes without modifying state or ledgers")
        if name == "undo":
            p.add_argument("--batch", default="", help="Batch ID to revert; defaults to latest batch")
        if name == "allow-term":
            p.add_argument("--term", required=True, help="Term to allow")
            p.add_argument("--scope", default="project", help="project | industry:<name> | doc:<doc_id>")
            p.add_argument("--rule-version", default="manual")
        if name == "flag-value":
            for field in ("page", "section", "value", "question"):
                p.add_argument(f"--{field}", required=True)
            p.add_argument("--row", default="")
            p.add_argument("--column", default="")
        if name == "cus-export":
            p.add_argument("--items", default="", help="Comma separated CUS IDs; default every 待汇总 question")
            p.add_argument("--batch", default="", help="Re-export webpage JSON for an existing exported batch without creating a new batch")
            p.add_argument("--webpage-json", action="store_true",
                           help="Also export customer-review-input.v1 draft JSON for customer-confirmation-webpage --mode customer")
        if name == "cus-sent":
            p.add_argument("--batch", required=True)
    args = parser.parse_args(argv)
    if not run_lock(Path(args.out).resolve(), f"governance {args.command}"):
        return 3
    build = build_of(args.out)
    if args.command == "sync":
        result: Any = sync(build, Path(args.vault).resolve())
    elif args.command == "auto":
        result = auto(build)
    elif args.command == "apply-terms":
        result = apply_terms(build, load_json(Path(args.decisions)))
    elif args.command == "review":
        result = {"review": str(review(build)), "template": str(build / "governance-decisions-template.json")}
        if args.webpage_json:
            target = None if args.webpage_json == "__DEFAULT__" else Path(args.webpage_json).resolve()
            result["webpage_json"] = str(export_internal_webpage_json(build, target))
    elif args.command == "collect":
        data = load_json(Path(args.decisions))
        result = collect(build, data, dry_run=args.dry_run)
    elif args.command == "undo":
        result = undo(build, args.batch or None)
    elif args.command == "allow-term":
        result = allow_term(build, args.term, args.scope, args.rule_version)
    elif args.command == "flag-value":
        result = flag_value(build, args.page, args.section, args.value, args.question, args.row, args.column,
                            Path(args.vault).resolve() if args.vault else None)
    elif args.command == "cus-export":
        if args.batch:
            state = load_state(build)
            if args.batch not in state["cus_batches"]:
                raise SystemExit(f"没有批次 {args.batch}")
            chosen = state["cus_batches"][args.batch]["items"]
            result = {"batch": args.batch, "reexported": len(chosen),
                      "file": str(build / state["cus_batches"][args.batch].get("file", ""))}
            if args.webpage_json:
                result["webpage_json"] = str(export_customer_webpage_json(build, args.batch, chosen))
        else:
            result = cus_export(build, [i.strip() for i in args.items.split(",") if i.strip()] or None)
            if args.webpage_json and result.get("batch"):
                chosen = load_state(build)["cus_batches"][result["batch"]]["items"]
                result["webpage_json"] = str(export_customer_webpage_json(build, result["batch"], chosen))
    elif args.command == "migrate":
        result = migrate(build, Path(args.vault).resolve())
    else:
        result = cus_sent(build, args.batch)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if isinstance(result, dict) and result.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())
