#!/usr/bin/env python3
"""Review of D-class files the client agreed to keep.

Screening put a file in D because it could not tell whether the file may be used, often
without reading it (a format it could not open, a customer's name in the file name). The
client's "keep" answers that question for the file as a whole; nobody has marked yet which
places inside it cannot be used, as screening does for B. The guard removes what it knows
(prices, customers, contacts, terms of payment); the rest needs a reader.

So each kept D file gets a review task: the AI reads its passages with the reason screening
gave and marks what cannot be used (drop) or must be asked (ask). Until the review is written
for the file as it is now (same source hash), the file is published as 待审: not 有效, not
in the public export.

    python scripts/d_review.py prepare --out <run>      # write the tasks (run_pipeline does this)
    python scripts/d_review.py status --out <run>       # which files still wait

The answer for a file goes in 04_知识库构建/d-review/<item_id>.json:

    {"item_id": "...", "source_hash": "...", "reviewed": true,
     "drop": [{"block": "<block id>", "why": "客户项目细节"}],
     "ask":  [{"block": "<block id>", "question": "这段案例能公开写吗？"}]}

Then rerun the pipeline: dropped passages are removed (logged in 内容清理记录), asked ones
wait for the client, and the file counts as reviewed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, load_json, load_jsonl, write_text

FOLDER = "d-review"
TRIAGE_FILE = Path("审计记录") / "triage-results.json"
LOOK_FOR = [
    "当初放进 D 类的原因（见上面）：这一点在文件里是不是真的有问题，在哪几段",
    "客户或买家的项目、订单、款式细节，即使没写客户名",
    "内部备注、批注、给同事的话、未定稿的说法",
    "竞品、其他供应商的资料或对比",
    "看起来是别人的资料（第三方品牌、授权、明星、IP）",
    "说不清是否属实、需要客户确认的说法（认证、数据、案例）",
]


def kept_d(routes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """item_id -> route, for D files that entered because the client said keep."""
    return {i: r for i, r in routes.items() if r.get("decision") == "D" and r.get("route") == "kb"}


def reviews(build: Path, routes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reviews that still match their file: a review of an older version does not count."""
    found = {}
    for item_id, route in kept_d(routes).items():
        path = build / FOLDER / f"{item_id}.json"
        if not path.is_file():
            continue
        try:
            review = load_json(path)
        except (OSError, ValueError):
            continue
        if review.get("reviewed") is True and review.get("source_hash") == route.get("source_hash"):
            found[item_id] = review
    return found


def marks(build: Path, routes: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """(block id -> why dropped, block id -> question to ask) from the valid reviews."""
    drop, ask = {}, {}
    for review in reviews(build, routes).values():
        drop.update({m["block"]: m.get("why") or "D 类复核删除" for m in review.get("drop", []) if m.get("block")})
        ask.update({m["block"]: m.get("question") or "D 类复核：待客户确认" for m in review.get("ask", []) if m.get("block")})
    return drop, ask


def unreviewed(build: Path, routes: dict[str, Any]) -> list[str]:
    done = reviews(build, routes)
    return sorted(i for i in kept_d(routes) if i not in done)


def screening_of(route: dict[str, Any], item_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    package = route.get("package")
    if not package:
        return {}
    if package not in cache:
        path = Path(package) / TRIAGE_FILE
        try:
            cache[package] = {i["item_id"]: i for i in load_json(path).get("items", [])} if path.is_file() else {}
        except (OSError, ValueError):
            cache[package] = {}
    return cache[package].get(item_id, {})


def prepare(build: Path) -> dict[str, Any]:
    routes = load_json(build / "routes.json")
    blocks = load_jsonl(build / "blocks.jsonl") if (build / "blocks.jsonl").is_file() else []
    docs = {d["item_id"]: d for d in load_jsonl(build / "docs.jsonl")} if (build / "docs.jsonl").is_file() else {}
    folder = build / FOLDER
    waiting = unreviewed(build, routes)
    cache: dict[str, dict[str, Any]] = {}
    for item_id in waiting:
        route, doc = routes[item_id], docs.get(item_id, {})
        triage = screening_of(route, item_id, cache)
        mine = [b for b in blocks if b.get("doc_id") == doc.get("doc_id")]
        lines = [f"# D 类复核：{doc.get('title') or item_id}", "",
                 f"- 资料编号：`{item_id}`；source_hash：`{route.get('source_hash', '')}`",
                 f"- 初筛为什么放进 D 类：{'、'.join(triage.get('reason_codes', [])) or '没写'}",
                 f"- 初筛说明：{triage.get('summary') or '没有'}",
                 f"- 初筛要求跟进：{triage.get('required_follow_up') or route.get('review_question') or '没有'}",
                 f"- 初筛读过内容吗：{'读过' if triage.get('content_read') else '没读过（只看了文件名或格式）'}",
                 "- 客户已回答：保留。这只说明整份可以用，下面逐段看哪里不能用。", "",
                 "## 要找的", "", *(f"- {item}" for item in LOOK_FOR), "",
                 "价格、客户名、联系方式、付款和贸易条款已经自动删掉（状态 removed），不用再标。", "",
                 "## 怎么回答", "",
                 f"写 `04_知识库构建/{FOLDER}/{item_id}.json`，整段不能用的写进 drop，要问客户的写进 ask，都没有就两个空列表：", "",
                 "```json",
                 json.dumps({"item_id": item_id, "source_hash": route.get("source_hash", ""), "reviewed": True,
                             "drop": [{"block": "<块编号>", "why": "原因"}], "ask": [{"block": "<块编号>", "question": "问客户的话"}]},
                            ensure_ascii=False, indent=2),
                 "```", "", "## 各段", ""]
        for block in mine:
            if block.get("status") in {"removed", "dropped"}:
                continue  # Already out; its text (a price, a customer) is not copied anywhere else.
            lines += [f"### `{block['block_id']}` · {block.get('locator', '')} · {block.get('status', '')}", "", block.get("text", ""), ""]
        write_text(folder / f"{item_id}.md", "\n".join(lines) + "\n")
    if waiting or folder.is_dir():
        write_text(folder / "README.md", "\n".join([
            "# D 类复核", "",
            "客户同意保留的 D 类文件，初筛没有像 B 类那样标出哪里不能用。每份一个任务页（<资料编号>.md），"
            "按页里写的原因逐段看，把答案写成同名 .json，然后重跑流水线。", "",
            f"还没复核：{len(waiting)} 份。复核前这些文件是“待审”，不进公开导出。", "",
            *(f"- [{item_id}]({item_id}.md)" for item_id in waiting), ""]) + "\n")
    return {"kept_d": len(kept_d(routes)), "waiting": len(waiting)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Review tasks for D-class files the client kept.")
    parser.add_argument("command", choices=["prepare", "status"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    if args.command == "prepare":
        result = prepare(build)
    else:
        routes = load_json(build / "routes.json")
        result = {"kept_d": len(kept_d(routes)), "waiting": unreviewed(build, routes)}
    print(json.dumps(result, ensure_ascii=False))
    if result.get("waiting"):
        print(f"D 类复核：还有 {result['waiting'] if isinstance(result['waiting'], int) else len(result['waiting'])} 份没做，"
              f"看 04_知识库构建/{FOLDER}/README.md；复核前这些文件是“待审”。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
