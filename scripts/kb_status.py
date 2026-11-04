#!/usr/bin/env python3
"""Write 00_知识库总览: what the knowledge base holds, what is pending, and where.

Read-only except for the pages it writes. --monthly also writes a dated health report,
which is the monthly maintenance check.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kb_common import BUILD_DIRECTORY, MODULES, TIERS, load_json, load_jsonl, parse_frontmatter, remaining_ocr_pages, write_text
from curate import page_sections
from mineru_client import probe
from review_cells import progress_of, t3_gate
from sections import load_sections


WEBSITE_STALE_DAYS = 31
STATUS_ORDER = ("有", "薄弱", "有原文没整理", "缺", "决定不写")
MARKER = "<!-- compile-enterprise-knowledge -->"
OVERVIEW = Path("01_工作台/00_知识库总览.md")
COVERAGE = Path("05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md")


def read(path: Path, default: Any) -> Any:
    return load_json(path) if path.is_file() else default


def gather(run: Path, vault: Path | None) -> dict[str, Any]:
    build = run / BUILD_DIRECTORY
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = read(build / "published.json", {})
    statuses = read(build / "statuses.json", {})
    pages = read(build / "website" / "pages.json", [])
    knowledge = sorted((build / "vault" / "03_正式知识").rglob("*.md")) if (build / "vault" / "03_正式知识").is_dir() else []
    topics = read(build / "curation" / "topics.json", [])
    written = {p.parent.name for p in (build / "curation").glob("*/page.md")}
    routes = read(build / "routes.json", {})
    ocr = []
    for item_id, route in routes.items():
        if route.get("route") == "kb" and route.get("ocr_pages"):
            pending = remaining_ocr_pages(build, item_id, route["ocr_pages"])
            if pending:
                ocr.append((route["display_name"], len(pending)))
    token = probe()
    passed, reason = t3_gate(run)
    receipt = vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json" if vault else None
    oldest = min((p["fetched_at"][:10] for p in pages if p.get("fetched_at")), default=None)
    names = {}
    for kind, stem in (("CUS", "待客户补充事项"), ("MAT", "源资料处理台账"), ("ANM", "源文与事实异常台账")):
        # Beside manage-article-knowledge, this skill's ledgers carry their own names.
        side = vault and any(vault.glob(f"05_数据与审核/30_异常与待决定/*/02_知识库构建_{stem}.md"))
        names[kind] = f"02_知识库构建_{stem}" if side else f"01_{stem}"
    return {
        "ledger_names": names,
        "verified": read(build / "校验结果.json", {}).get("verified"),
        "docs": docs, "published": published, "statuses": statuses, "knowledge": knowledge,
        "topics_without_page": [t["topic"] for t in topics if t.get("blocks") and t["slug"] not in written and not t.get("skipped")],
        "topics_skipped": [(t["topic"], t["skipped"]) for t in topics if t.get("blocks") and t["slug"] not in written and t.get("skipped")],
        "warnings": read(build / "校验结果.json", {}).get("warnings", []),
        "sections": section_rows(run),
        "ledgers": read(build / "governance-summary.json", {}),
        "terms_to_review": len(load_jsonl(build / "review-queue.jsonl")),
        "cells": progress_of(run), "t3": "可发布" if passed else reason, "ocr": ocr,
        "token": token["expiry"]["message"] if token["ok"] else token["message"],
        "token_days": token["expiry"].get("days_remaining"),
        "website_pages": len(pages), "website_oldest": oldest,
        "website_stale": bool(oldest) and (date.today() - date.fromisoformat(oldest)).days > WEBSITE_STALE_DAYS,
        "last_publish": read(receipt, {}).get("published_at") if receipt else None,
    }


def overview(state: dict[str, Any]) -> list[str]:
    docs, published, statuses = state["docs"], state["published"], state["statuses"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["# 知识库总览", "", MARKER, f"更新于 {now}；最近发布：{state['last_publish'] or '未发布'}。", ""]

    lines.extend(["## 整理层：给人看的知识页", "", "每条都链接到原文，⚠ 和“需核实”的内容使用前要核对。", ""])
    by_module: dict[str, list[str]] = {}
    for page in state["knowledge"]:
        fields = parse_frontmatter(page.read_text(encoding="utf-8"))
        module = fields.get("module", page.parent.name).split("/")[0]
        by_module.setdefault(module, []).append(page.stem)
    for module in MODULES.values():
        name = module.split("/")[0]
        if name in by_module:
            lines.append(f"- {name}：" + "、".join(f"[[{stem}]]" for stem in by_module[name]))
    if not state["knowledge"]:
        lines.append("- 还没有整理页：运行 curate.py prepare，由 AI 写好后再重跑。")
    if state["topics_without_page"]:
        lines.append(f"- 已准备、还没写的主题：{len(state['topics_without_page'])} 个（`04_知识库构建/curation`）")

    lines.extend(["", "## 来源层：净化后的原文", "", "位置：`02_源资料/10_净化原文`。原文只删不改，每份开头有来源卡片。", ""])
    folders = Counter(docs[d]["folder"].split("/")[0] for d in published)
    status_counts = Counter(statuses.values())
    lines.extend(["| 类别 | 份数 |", "| --- | ---: |"])
    lines.extend(f"| {folder} | {count} |" for folder, count in folders.most_common())
    lines.extend(["", "状态：" + "，".join(f"{status} {count} 份" for status, count in status_counts.most_common())
                  + "。“待审”是产品表和邮件要点，还没核对过；“待客户确认”要等客户回复。", ""])

    ledgers, names = state["ledgers"], state["ledger_names"]
    lines.extend(["## 待处理：都在 05_数据与审核", "",
                  f"- 待客户确认（CUS）：{ledgers.get('CUS', 0)} 项 → [[{names['CUS']}]]",
                  f"- 资料要处理（MAT）：{ledgers.get('MAT', 0)} 项 → [[{names['MAT']}]]",
                  f"- 风险与冲突（ANM）：{ledgers.get('ANM', 0)} 项 → [[{names['ANM']}]]",
                  "- 按规则删掉了什么：[[内容清理记录]]", ""])

    lines.extend(["## 还缺什么", "", "各模块的详细情况见 [[01_知识库覆盖与缺口]]。", ""])
    gaps = []
    counts = section_counts(state["sections"])
    if counts:
        gaps.append("- 小板块：" + "，".join(f"{status} {counts[status]}" for status in STATUS_ORDER if counts[status]))
    if state["ocr"]:
        gaps.append(f"- 图片页尚无可检索文字：{len(state['ocr'])} 份资料，共 {sum(n for _, n in state['ocr'])} 页；需核对是否为空白或纯图片（MinerU：{state['token']}）")
    cells = state["cells"]
    if cells["cells_total"]:
        gaps.append(f"- 产品表逐格判断：{cells['cells_decided']} / {cells['cells_total']}（{state['t3']}）")
    if state["terms_to_review"]:
        gaps.append(f"- 待判断的词：{state['terms_to_review']} 个（`04_知识库构建/review-queue.md`）")
    if state["website_stale"]:
        gaps.append(f"- 官网最早一页抓取于 {state['website_oldest']}，建议重新读取")
    lines.extend(gaps or ["- 暂无"])

    lines.extend(["", "## 资料从哪里来", "", "- 每份资料的身份、能否检索、去向：[[源资料与可检索性台账]]",
                  f"- 官网：{state['website_pages']} 页" + (f"，最早抓取 {state['website_oldest']}" if state["website_oldest"] else ""),
                  "", "## 来源级别", ""])
    lines.extend(f"- {tier}：{info['label']}" for tier, info in TIERS.items())
    lines.append("- 级别只说明资料从哪来，不等于每句话都已核实。")
    return lines


def section_counts(rows: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        counts[row["status"]] += row.get("count", 1)  # A skipped topic counts each of its sub-sections.
    return counts


def section_rows(run: Path) -> list[dict[str, Any]]:
    """One row per sub-section of every module: how much source text it has and whether a page covers it."""
    build = run / BUILD_DIRECTORY
    config = read(build / "kb-config.json", {})
    table = load_sections(config)
    topics = read(build / "curation" / "topics.json", [])
    pages = {t["slug"]: (build / "curation" / t["slug"] / "page.md") for t in topics}
    text = {slug: path.read_text(encoding="utf-8") if path.is_file() else None for slug, path in pages.items()}
    rows: list[dict[str, Any]] = []
    for key in MODULES:
        own = [t for t in topics if t["module"] == key and not t.get("parent")]
        if not own and table.get(key):
            own = [{"slug": None, "topic": "", "sections": {s["name"]: {"available": 0, "documents": 0} for s in table[key]}, "split": {}}]
        hints = {s["name"]: s.get("how", "") for s in table.get(key, [])}
        for topic in own:
            page = text.get(topic["slug"])
            if topic.get("skipped"):
                # One row with the reason, not the same reason under every sub-section.
                rows.append({"module": key, "topic": topic["topic"], "slug": topic["slug"],
                             "section": "、".join(topic["sections"]) or "整个主题",
                             "available": sum(s["available"] for s in topic["sections"].values()),
                             "documents": max((s["documents"] for s in topic["sections"].values()), default=0),
                             "page": topic["slug"], "status": "决定不写", "how": topic["skipped"],
                             "count": len(topic["sections"]) or 1})
                continue
            for name, info in topic["sections"].items():
                child = topic.get("split", {}).get(name)
                if child:
                    written = text.get(child) is not None
                else:
                    written = page is not None and name in page_sections(page)
                # One catalog is one product line: a single source is what such a section always has.
                single_ok = topic.get("by_line")
                if not info["available"]:
                    status, how = "缺", hints.get(name, "补这方面的资料")
                elif not written:
                    status, how = "有原文没整理", "补写这一节" if page is not None else "先写这个主题的 page.md"
                elif info["available"] < 3 or (info["documents"] <= 1 and not single_ok):
                    status, how = "薄弱", hints.get(name, "补一份来源")
                else:
                    status, how = "有", ""
                rows.append({"module": key, "topic": topic["topic"], "slug": topic["slug"], "section": name,
                             "available": info["available"], "documents": info["documents"],
                             "page": child or topic["slug"], "status": status, "how": how})
    return rows


def coverage(state: dict[str, Any], run: Path) -> list[str]:
    """Per module and sub-section: what the knowledge layer covers, what is thin, what is missing."""
    docs, published, statuses = state["docs"], state["published"], state["statuses"]
    usage = read(run / BUILD_DIRECTORY / "curation-used.json", {})
    cited = {doc_id for page in usage.values() for doc_id in page["sources"]}
    rows = state["sections"]
    counts = section_counts(rows)
    lines = ["# 知识库覆盖与缺口", "", MARKER, f"更新于 {datetime.now():%Y-%m-%d %H:%M}。", "",
             "小板块状态：" + "，".join(f"{status} {counts[status]}" for status in STATUS_ORDER if counts[status]) + "。",
             "“有”= 原文和整理页都有；“薄弱”= 原文少于 3 段或只有一份来源；“有原文没整理”= 原文有、整理页没写这一节；"
             "“缺”= 一段原文也没有。分类按关键词自动做，分错了改 kb-config.json 的 sections。", ""]
    for key, module in MODULES.items():
        name = module.split("/")[0]
        pages = [title for title, page in usage.items() if page["module"] == module]
        sources = [d for d in published if docs[d]["module"] == module]
        valid = [d for d in sources if statuses.get(d) == "有效"]
        uncited = [d for d in valid if d not in cited]
        lines.extend([f"## {name}", ""])
        lines.append(f"- 整理页：{'、'.join(f'[[{t}]]' for t in pages) if pages else '无'}")
        lines.append(f"- 来源文档：{len(sources)} 份，其中有效 {len(valid)} 份")
        if uncited:
            shown = "、".join(f"[[{Path(docs[d]['file_name']).stem}]]" for d in uncited[:10])
            lines.append(f"- 有效但没被整理页引用：{len(uncited)} 份 — {shown}{' 等' if len(uncited) > 10 else ''}")
            lines.append("  - 意思是：这些原文已经入库、能检索，但整理页里还没有一句话来自它们，读整理页的人看不到。"
                         "补写对应主题的 page.md，或在整理页里引用它们。")
        if key == "case":
            lines.append("- 合作案例要等客户确认能公开后才能写。")
        mine = [row for row in rows if row["module"] == key]
        for topic in dict.fromkeys(row["topic"] for row in mine):
            if len({row["topic"] for row in mine}) > 1:
                lines.extend(["", f"### {topic}"])
            lines.extend(["", "| 小板块 | 原文 | 来源 | 状态 | 怎么补 |", "| --- | ---: | ---: | --- | --- |"])
            lines.extend(f"| {row['section']} | {row['available']} 段 | {row['documents']} 份 | {row['status']} | {row['how']} |"
                         for row in mine if row["topic"] == topic)
        lines.append("")
    lines.extend(["## 其他缺口", ""])
    if state["topics_without_page"]:
        lines.append(f"- 已准备、还没写的主题：{'、'.join(state['topics_without_page'])}（`04_知识库构建/curation`）")
    for title, reason in state["topics_skipped"]:
        lines.append(f"- 决定不写：{title}（{reason}）")
    for warning in state["warnings"]:
        lines.append(f"- {warning}")
    if state["ocr"]:
        lines.append(f"- 图片页还没识别：{len(state['ocr'])} 份资料，共 {sum(n for _, n in state['ocr'])} 页")
    cells = state["cells"]
    if cells["cells_total"] and cells["cells_decided"] < cells["cells_total"]:
        lines.append(f"- 产品表逐格判断：{cells['cells_decided']} / {cells['cells_total']}")
    return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the knowledge base overview; --monthly writes the monthly check.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--vault", help="Published project folder")
    parser.add_argument("--monthly", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run = Path(args.out).resolve()
    vault = Path(args.vault).resolve() if args.vault else None
    state = gather(run, vault)
    page = overview(state)
    write_text(run / BUILD_DIRECTORY / "总览.md", "\n".join(page) + "\n")
    if vault:
        write_text(vault / OVERVIEW, "\n".join(page) + "\n")
        write_text(vault / COVERAGE, "\n".join(coverage(state, run)) + "\n")
        if args.monthly:
            month = date.today().strftime("%Y-%m")
            report = [f"# 知识库月度体检 {month}", "", *page[3:], "", "## 本月要做", "",
                      "- 有新资料：用 prepare-seo-knowledge-materials 生成小初筛包，加进 --package 重跑",
                      "- 客户回复了确认表：apply_answers.py 读回，再重跑和发布",
                      "- 官网超过一个月没读：fetch_website.py 重读，变了的页才会更新",
                      "- 抽查：让独立 Agent 通读本月新增或变化的文档和整理页"]
            write_text(vault / f"05_数据与审核/40_月度与交接/知识库体检_{month}.md", "\n".join(report) + "\n")
    summary = {key: state[key] for key in ("verified", "ledgers", "terms_to_review", "cells", "t3", "token", "last_publish")}
    print(json.dumps({**summary, "knowledge_pages": len(state["knowledge"]), "source_docs": len(state["published"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
