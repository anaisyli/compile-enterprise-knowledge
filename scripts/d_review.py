#!/usr/bin/env python3
"""Look at the D files one by one, with everything a decision needs side by side.

Screening puts a file in D when it could not tell whether the file may be used, and marks
nothing inside it. Two piles wait on a person or the model:

- documents already kept (by the client or by us), 待审 until someone approves them;
- files nobody has decided yet: kept, dropped, or put to the client.

export  writes 04_知识库构建/d-review/: a README with the rules, todo-approve.jsonl (one kept
        document per line: screening's reason, what the rules removed and why, the marked
        words, passages that name other brands, promotions or internal notes) and
        todo-decide.jsonl (one undecided file per line, grouped by folder). Nothing else changes.
collect reads answer-*.jsonl and writes approve_docs / drop_blocks into review-decisions.json,
        and d_keep / d_drop / ask_client into kb-config.json. Both are backed up first.

Rerun the pipeline afterwards; nothing here rebuilds pages.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, TRIAGE_NAME, load_json, load_jsonl, unapproved_d, write_json, write_jsonl, write_text


FOLDER = "d-review"
# Passages a person should read before approving: other companies' names, offers, notes to self.
WATCH = re.compile(
    r"(?i)buy one|free gift|discount|限时|特价|促销|买一送一|优惠|"
    r"price is valid|valid for \d+ days|有效期|warranty for free|免费保修|"
    r"内部|备注|仅供参考|参考款|参考供应商|草稿|draft|待确认|"
    r"客户名单|合作客户|our (?:clients|customers|partners)|cooperat\w+ with|such as:"
)
MEDIA = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".psd", ".heic", ".heif", ".tif", ".tiff", ".svg", ".eps",
         ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".flv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac"}
ARCHIVES = {".zip", ".rar", ".7z", ".tar", ".gz"}
APPROVE = {"approve", "approve_after_drop"}
DECIDE = {"keep", "drop", "ask_client"}
RULES = """# D 类逐份判断

初筛放进 D 类的文件，初筛没读过内容或拿不准能不能用，里面也没标出哪里不能用。这里一份一份看。

## 一、已保留、等批准的文档（todo-approve.jsonl）

每行一份：初筛给的原因、规则删掉了哪些段和原因、标 ⚠ 的词、需要留意的段（别家品牌、促销、内部备注、
客户名单、报价条件）。**先打开 `page` 指向的净化原文通读**，再结合这些信号判断：

- 没有客户项目细节、内部备注、竞品、需要客户确认的说法：`approve`。
- 个别段不能用（混进来的别家文案、促销语、报价有效期、合作客户名单等）：`approve_after_drop`，
  在 `drop_blocks` 写这些段的块编号，其余照常批准。
- 只有客户能定（参考整理稿、拿不准是不是公司自己的资料）：`ask_client`，`reason` 写一句为什么问。
  整份会标 🕒 进 CUS 和客户确认表。
- 整份拿不准、要人看：`hold`，`reason` 写原因。不写进任何文件，汇报时列出来。

规格参数里写的面板、芯片、平台、元器件品牌（BOE、LG、MediaTek、Google EDLA、断路器铭牌等）说的是
零部件来源或兼容性，不算要拦的第三方品牌；代言、授权、IP、竞品才算。项目另有规定的以项目为准。

## 二、还没决定的文件（todo-decide.jsonl）

按文件夹成组看。每行一个文件：路径、类型、初筛的原因。

- 公司自己的产品资料、图片、视频，有用：`keep`。图片、psd、视频会直接进素材库，文档入库后是“待审”。
- 没用的、客户或别家的、压缩包、系统和临时文件（`~$` 开头、`.DS_Store`）：`drop`。
- 只有客户能定的（现场实拍、可能露出客户标志、注明要改品牌的）：`ask_client`，写 `reason`。

## 写答案

写 `answer-<任意名字>.jsonl`，每行一个 JSON：

```json
{"doc_id": "DOC-0123456789", "decision": "approve"}
{"doc_id": "DOC-0123456789", "decision": "approve_after_drop", "drop_blocks": ["a1b2c3d4e5f6"], "reason": "混进别家 CMS 文案"}
{"doc_id": "DOC-0123456789", "decision": "ask_client", "reason": "参考整理稿，请公司确认是不是自己的资料"}
{"item_id": "local-…", "decision": "keep"}
{"item_id": "local-…", "decision": "ask_client", "reason": "现场实拍，可能露出客户标志"}
```

一个文件夹的文件判断相同时，可以写 `{"folder": "<todo-decide 里的 folder>", "decision": "drop", "reason": "…"}`。

写完运行 `d_review.py collect --out <运行目录> --config <kb-config.json>`，再重跑流水线。
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review D files one by one.")
    parser.add_argument("command", choices=["export", "collect"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", help="kb-config.json (collect writes d_keep / d_drop / ask_client into it)")
    return parser.parse_args()


def triage_of(routes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for package in sorted({r.get("package") for r in routes.values() if r.get("package")}):
        path = Path(package) / TRIAGE_NAME
        if path.is_file():
            found.update({item["item_id"]: item for item in load_json(path).get("items", [])})
    return found


def screening_reason(triage: dict[str, Any]) -> str:
    return "；".join(t for t in (triage.get("summary"), triage.get("required_follow_up")) if t)


def export(build: Path) -> dict[str, Any]:
    routes = load_json(build / "routes.json")
    decisions = load_json(build / "review-decisions.json") if (build / "review-decisions.json").is_file() else {}
    docs = {d["item_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = load_json(build / "published.json") if (build / "published.json").is_file() else {}
    triage = triage_of(routes)
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in load_jsonl(build / "blocks.jsonl"):
        by_doc[block["doc_id"]].append(block)

    approve, seen = [], set()
    for item_id in unapproved_d(routes, decisions):
        doc = docs.get(item_id)
        if not doc or doc["doc_id"] in seen:
            continue  # The same file in three folders is one page to approve.
        seen.add(doc["doc_id"])
        blocks = by_doc.get(doc["doc_id"], [])
        removed = [{"block_id": b["block_id"], "why": b.get("hold_reasons", []), "text": b["text"][:160]}
                   for b in blocks if b["status"] == "removed"]
        terms = Counter(t for b in blocks for f in b.get("flags", []) for t in f.get("terms", []))
        flags = Counter(f["type"] for b in blocks for f in b.get("flags", []))
        watch = []
        for b in blocks:
            if b["status"] in {"kept", "flagged", "pending_client"}:
                match = WATCH.search(b["text"])
                if match:
                    start = max(0, match.start() - 60)
                    watch.append({"block_id": b["block_id"], "word": match.group(0),
                                  "text": b["text"][start: match.end() + 100].replace("\n", " ")})
        approve.append({
            "doc_id": doc["doc_id"], "item_id": item_id, "title": doc["title"],
            "page": f"03_净化md/{published.get(doc['doc_id'], doc['file_name'])}",
            "kept_by": doc.get("d_by") or routes[item_id].get("kept_by") or "",
            "screening": screening_reason(triage.get(item_id, {})),
            "reference_draft": doc.get("role") == "reference",
            "blocks": dict(Counter(b["status"] for b in blocks)),
            "removed": removed[:20], "removed_count": len(removed),
            "flags": dict(flags), "marked_words": [t for t, _ in terms.most_common(20)],
            "watch": watch[:20],
        })

    decide = []
    for item_id, route in sorted(routes.items(), key=lambda kv: kv[1].get("source_relpath") or ""):
        if route.get("decision") != "D" or route.get("reason") != "unresolved_item":
            continue
        relpath = route.get("source_relpath") or route.get("path") or route.get("display_name") or ""
        name = Path(relpath).name
        extension = Path(name).suffix.casefold()
        hint = ("压缩包，一般不入库" if extension in ARCHIVES else
                "系统或 Office 临时文件" if name.startswith("~$") or name == ".DS_Store" else
                "图片、设计稿或视频：保留就进素材库" if extension in MEDIA else "文档：保留后入库是“待审”")
        decide.append({"item_id": item_id, "folder": str(Path(relpath).parent.as_posix()), "name": name,
                       "type": extension or "无扩展名", "screening": screening_reason(triage.get(item_id, {})), "hint": hint})

    folder = build / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    write_text(folder / "README.md", RULES)
    write_jsonl(folder / "todo-approve.jsonl", approve)
    write_jsonl(folder / "todo-decide.jsonl", decide)
    groups = Counter(entry["folder"] for entry in decide)
    return {"to_approve": len(approve), "to_decide": len(decide), "decide_folders": len(groups),
            "folder": str(folder), "next": "按 README.md 判断，写 answer-*.jsonl，再运行 collect"}


def backup(path: Path) -> None:
    if path.is_file():
        shutil.copy2(path, path.with_name(f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak{path.suffix}"))


def collect(build: Path, config_path: Path | None) -> dict[str, Any]:
    folder = build / FOLDER
    answers = [row for path in sorted(folder.glob("answer-*.jsonl")) for row in load_jsonl(path)]
    if not answers:
        raise SystemExit(f"没有答案：先按 {folder / 'README.md'} 写 answer-*.jsonl")
    approve_todo = {e["doc_id"]: e for e in load_jsonl(folder / "todo-approve.jsonl")}
    decide_todo = load_jsonl(folder / "todo-decide.jsonl")
    by_folder: dict[str, list[str]] = defaultdict(list)
    for entry in decide_todo:
        by_folder[entry["folder"]].append(entry["item_id"])
    known_items = {e["item_id"] for e in decide_todo} | {e["item_id"] for e in approve_todo.values()}

    approve, drop, ask, held, problems = set(), set(), {}, [], []
    keep, dropped = set(), set()
    for row in answers:
        decision = row.get("decision")
        if row.get("doc_id"):
            entry = approve_todo.get(row["doc_id"])
            if not entry:
                problems.append(f"todo-approve 里没有 {row['doc_id']}")
                continue
            if decision in APPROVE:
                approve.add(row["doc_id"])
                drop.update(row.get("drop_blocks") or [])
                if decision == "approve_after_drop" and not row.get("drop_blocks"):
                    problems.append(f"{row['doc_id']} 写了 approve_after_drop 却没写 drop_blocks")
            elif decision == "ask_client":
                ask[entry["item_id"]] = row.get("reason") or ""
            elif decision == "hold":
                held.append({"doc_id": row["doc_id"], "title": entry["title"], "reason": row.get("reason", "")})
            else:
                problems.append(f"{row['doc_id']} 的 decision 看不懂：{decision}")
            continue
        items = by_folder.get(row["folder"], []) if row.get("folder") else [row.get("item_id")]
        if not items or not all(i in known_items for i in items):
            problems.append(f"todo-decide 里找不到：{row.get('folder') or row.get('item_id')}")
            continue
        if decision not in DECIDE:
            problems.append(f"{row.get('folder') or row.get('item_id')} 的 decision 看不懂：{decision}")
            continue
        for item_id in items:
            if decision == "keep":
                keep.add(item_id)
            elif decision == "drop":
                dropped.add(item_id)
            else:
                ask[item_id] = row.get("reason") or ""
    if problems:
        raise SystemExit("答案有问题，什么都没写：\n- " + "\n- ".join(problems))

    decisions_path = build / "review-decisions.json"
    decisions = load_json(decisions_path) if decisions_path.is_file() else {}
    backup(decisions_path)
    decisions["approve_docs"] = sorted(set(decisions.get("approve_docs", [])) | approve)
    decisions["drop_blocks"] = sorted(set(decisions.get("drop_blocks", [])) | drop)
    write_json(decisions_path, decisions)

    if keep or dropped or ask:
        if not config_path:
            raise SystemExit("有 keep / drop / ask_client 的答案，要用 --config 指定 kb-config.json")
        config = load_json(config_path)
        backup(config_path)
        config["d_keep"] = sorted(set(config.get("d_keep", [])) | keep)
        config["d_drop"] = sorted(set(config.get("d_drop", [])) | dropped)
        config["ask_client"] = {**(config.get("ask_client") or {}), **ask}
        write_json(config_path, config)
    return {"approve_docs": len(approve), "drop_blocks": len(drop), "d_keep": len(keep), "d_drop": len(dropped),
            "ask_client": len(ask), "hold": held, "next": "重跑 run_pipeline.py"}


def main() -> int:
    args = parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    if args.command == "export":
        result = export(build)
    else:
        result = collect(build, Path(args.config).resolve() if args.config else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
