#!/usr/bin/env python3
"""Write 00_知识库总览: what the knowledge base holds, what is pending, and where.

Read-only except for the pages it writes. --monthly also writes a dated health report,
which is the monthly maintenance check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kb_common import (BUILD_DIRECTORY, MODULES, PAGED_FILES, TIERS, load_json, load_jsonl, parse_frontmatter, read_by,
                       remaining_ocr_pages, run_lock, site_view_enabled, write_json, write_text)
from curate import page_sections
from mineru_client import probe
from review_cells import progress_of, t3_gate
from sections import load_sections
from governance import CLOSED_STAGES, SOLVED_STAGE, LEDGERS, parse_ledger


WEBSITE_STALE_DAYS = 31
STATUS_ORDER = ("有", "薄弱", "有原文没整理", "缺", "决定不写")
MARKER = "<!-- compile-enterprise-knowledge -->"
OVERVIEW = Path("01_工作台/00_知识库总览.md")
COVERAGE = Path("05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md")


def read(path: Path, default: Any) -> Any:
    return load_json(path) if path.is_file() else default


def external_summary(vault: Path | None) -> dict[str, Any] | None:
    if not vault:
        return None
    from research import status as research_status
    return research_status(vault)


def gather(run: Path, vault: Path | None) -> dict[str, Any]:
    build = run / BUILD_DIRECTORY
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = read(build / "published.json", {})
    statuses = read(build / "statuses.json", {})
    pages = read(build / "website" / "pages.json", [])
    knowledge_root = build / "vault" / "03_正式知识" / "10_客户知识"
    knowledge = sorted(knowledge_root.rglob("*.md")) if knowledge_root.is_dir() else []
    topics = read(build / "curation" / "topics.json", [])
    written = {p.parent.name for p in (build / "curation").glob("*/page.md")}
    routes = read(build / "routes.json", {})
    ocr = []
    for item_id, route in routes.items():
        if route.get("route") == "kb" and route.get("ocr_pages"):
            pending = remaining_ocr_pages(build, item_id, route["ocr_pages"])
            if pending:
                ocr.append((route["display_name"], len(pending)))
    found, reading = read_by(build), {}
    for item_id, route in routes.items():
        suffix = Path(route.get("display_name") or "").suffix.casefold()
        if route.get("route") != "kb" or suffix not in PAGED_FILES:
            continue
        row = reading.setdefault("PDF" if suffix == ".pdf" else "PPT", Counter())
        how = found.get(item_id, set())
        row.update(["files", *how] if how else ["files", "text"])
    token = probe()
    passed, reason = t3_gate(run)
    receipt = vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json" if vault else None
    oldest = min((p["fetched_at"][:10] for p in pages if p.get("fetched_at")), default=None)
    names, ledger_counts = {}, {}
    for kind, stem in (("CUS", "待客户补充事项"), ("MAT", "源资料处理台账"), ("ANM", "源文与事实异常台账")):
        # Beside manage-article-knowledge, this skill's ledgers carry their own names.
        side = vault and any(vault.glob(f"05_数据与审核/30_异常与待决定/*/02_知识库构建_{stem}.md"))
        name = f"02_知识库构建_{stem}.md" if side else f"01_{stem}.md"
        names[kind] = (Path(LEDGERS[kind][0]).parent / name).as_posix()
        ledger = vault / names[kind] if vault else build / "vault" / names[kind]
        if ledger.is_file():
            header, rows, _ = parse_ledger(ledger.read_text(encoding="utf-8"))
            stage = header.index("当前阶段") if "当前阶段" in header else None
            closed = sum(stage is not None and row[stage] in CLOSED_STAGES - {SOLVED_STAGE} for row in rows.values())
            solved = sum(stage is not None and row[stage] == SOLVED_STAGE for row in rows.values())
            ledger_counts[kind] = {"pending": len(rows) - closed - solved, "resolved": closed, "system_solved": solved}
    pictures = read(build / "pictures.json", {})
    media = read(build / "media.json", [])
    flagged_terms = {term.casefold() for block in load_jsonl(build / "blocks.jsonl")
                     if block["doc_id"] in published and docs[block["doc_id"]]["tier"] in {"T1", "T2"}
                     and block["status"] in {"kept", "flagged"}
                     for flag in block.get("flags", []) if flag["type"] == "unverified_name" for term in flag["terms"]}
    return {
        "ledger_names": names,
        "ledger_counts": ledger_counts,
        "build_stage": read(build / "kb-config.json", {}).get("build_stage", "full"),
        # Linked only when made: a link to a view that is switched off would be dead.
        "site_view": site_view_enabled(read(build / "kb-config.json", {})),
        "picture_count": len(pictures),
        "retained_count": sum(e.get("id") not in pictures for e in media),
        "risk_terms": len(flagged_terms),
        "verified": read(build / "校验结果.json", {}).get("verified"),
        "docs": docs, "published": published, "statuses": statuses, "knowledge": knowledge,
        "topics_without_page": [t["topic"] for t in topics if t.get("blocks") and t["slug"] not in written and not t.get("skipped")],
        "topics_skipped": [(t["topic"], t["skipped"]) for t in topics if t.get("blocks") and t["slug"] not in written and t.get("skipped")],
        "warnings": read(build / "校验结果.json", {}).get("warnings", []),
        "sections": section_rows(run),
        "ledger": read(build / "curation-ledger.json", {}),
        "decided": read(build / "governance-decisions-summary.json", {}),
        # External research lives in the vault only; every writer of the overview reads it the same way.
        "external": external_summary(vault),
        "build": str(build),
        "ledgers": read(build / "governance-summary.json", {}),
        "terms_to_review": len(load_jsonl(build / "review-queue.jsonl")),
        "cells": progress_of(run), "t3": "可发布" if passed else reason, "ocr": ocr, "reading": reading,
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

    site = (["- 按栏目和产品查资料：[[03_正式知识/80_建站资料/00_建站资料总览|建站资料总览]]"] if state.get("site_view")
            else ["- 建站资料视图没有打开；要按网站栏目看客户资料，运行 `site_view.py update`。"])
    lines.extend(["## 建站资料与素材", "", *site,
                  f"- 图片和原件素材：[[03_正式知识/90_素材/00_素材索引|素材索引]]（库内图片 {state.get('picture_count', 0)} 张；原位置素材 {state.get('retained_count', 0)} 份）",
                  "- 建站用表格：[[03_正式知识/90_素材/00_素材清单.csv|素材清单]]",
                  "- 大图、视频和设计稿可按记录打开原件；已确认可用的素材不列为待处理问题。", "",
                  "## 整理层：给人看的知识页", "", "每条都链接到原文，⚠ 和“需核实”的内容使用前要核对。", ""])
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
        lines.append("- 当前仅构建来源层（sources_only），尚未编写整理页。" if state.get("build_stage") == "sources_only"
                     else "- 还没有整理页：运行 curate.py prepare，由 AI 写好后再重跑。")
    if state["topics_without_page"]:
        lines.append(f"- 预备整理主题：{len(state['topics_without_page'])} 个，详情见覆盖与缺口。")
    lines.extend(["", "## 写文章用", "",
                  "- 写作目录（每页的别名和小板块）：[[01_工作台/写作目录|写作目录]]；取资料用 `kb_lookup.py pack`。"])
    external = state.get("external")
    if external:
        lines.append(f"- 外部公共知识：{external['pages']} 页，待抽检 {len(external['to_spot_check'])} 页，"
                     f"过了复查日期 {len(external['overdue'])} 页；待调研缺口 {external['todo']} 个（`research.py status`）。")

    lines.extend(["", "## 来源层：净化后的原文", "", "位置：`02_源资料/10_净化原文`。原文只删不改，每份开头有来源卡片。", ""])
    folders = Counter(docs[d]["folder"].split("/")[0] for d in published)
    status_counts = Counter(statuses.values())
    lines.extend(["| 类别 | 份数 |", "| --- | ---: |"])
    lines.extend(f"| {folder} | {count} |" for folder, count in folders.most_common())
    lines.extend(["", f"合计 {len(published)} 份来源文档；自动生成的产品表汇总页单列，不重复计为来源文档。"])
    lines.extend(["", "状态：" + "，".join(f"{status} {count} 份" for status, count in status_counts.most_common())
                  + "。“待审”表示尚未完成内容核对；“待客户确认”表示要等客户回复。", ""])
    unapproved = sum(statuses.get(k) == "待审" and bool(docs[k].get("d_unapproved")) for k in published)
    other_pending = sum(statuses.get(k) == "待审" and not docs[k].get("d_unapproved") for k in published)
    if unapproved or other_pending:
        lines.extend([f"待审来源中：D 类尚未内部批准 {unapproved} 份，其他待审 {other_pending} 份。"
                      "客户同意入库和使用已生效；参考稿、参数核对等内部审核状态另行保留。", ""])

    ledgers, names = state["ledgers"], state["ledger_names"]
    lines.extend(["## 待处理：都在 05_数据与审核", ""])
    decided = state.get("decided") or {}
    if decided:
        lines.append(f"- **需要做决定 {decided.get('internal_groups', 0) + decided.get('cus_draft', 0)} 组**（内部 {decided.get('internal_groups', 0)} 组，"
                     f"待发客户 {decided.get('cus_draft', 0)} 组）；已发客户待答 {decided.get('cus_waiting', 0)} 组，客户已答待核验 {decided.get('cus_answered', 0)} 组；"
                     f"风险保留 {decided.get('risk_retained', 0)} 组（照用、带 ⚠，不算待办）。说“处理这个项目的待办”即可。")
        lines.append(f"- 审计：涉及证据 {decided.get('evidence', 0)} 条，其中待内部处理 {decided.get('internal_open_evidence', 0)} 条。")
    for kind, label in (("CUS", "待客户确认"), ("MAT", "资料要处理"), ("ANM", "风险与冲突")):
        counts = state.get("ledger_counts", {}).get(kind, {"pending": ledgers.get(kind, 0), "resolved": 0, "system_solved": 0})
        lines.append(f"- {label}（{kind}）：当前待处理 {counts['pending']} 项；历史解除、待关闭 {counts.get('system_solved', 0)} 项；人工已关闭 {counts['resolved']} 项 → [[{names[kind].removesuffix('.md')}|{Path(names[kind]).stem.removeprefix('01_')}]]")
    lines.extend(["- 按规则删掉了什么：[[05_数据与审核/50_运行记录/内容清理记录|内容清理记录]]", ""])

    lines.extend(["## 还缺什么", "", "各模块的详细情况见 [[05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口|知识库覆盖与缺口]]。", ""])
    gaps = []
    counts = section_counts(state["sections"])
    if counts:
        gaps.append("- 小板块：" + "，".join(f"{status} {counts[status]}" for status in STATUS_ORDER if counts[status]))
    left = state.get("ledger", {}).get("total", {}).get("未处理", 0)
    if left:
        gaps.append(f"- 整理覆盖：还有 {left} 段原文在整理页里没有去处，见覆盖与缺口的“整理覆盖账本”")
    if state["ocr"]:
        gaps.append(f"- 已入库资料中尚无可检索文字的图片页：{len(state['ocr'])} 份资料，共 {sum(n for _, n in state['ocr'])} 页；是否需要识别见源资料处理台账。")
    cells = state["cells"]
    if cells["cells_total"]:
        gaps.append(f"- 产品表逐格判断：{cells['cells_decided']} / {cells['cells_total']}（{state['t3']}）")
    if state["terms_to_review"]:
        gaps.append(f"- 待复核词：全部来源 {state['terms_to_review']} 个，其中 T1/T2 资料涉及 {state.get('risk_terms', 0)} 个（忽略大小写去重）；见源文与事实异常台账。")
    if state["website_stale"]:
        gaps.append(f"- 官网最早一页抓取于 {state['website_oldest']}，建议重新读取")
    lines.extend(gaps or ["- 暂无"])

    waiting = read(Path(state["build"]) / "samples-waiting.json", []) if state.get("build") else []
    lines.extend(["", "## 资料从哪里来", "", "- 每份资料的身份、能否检索、去向：[[02_源资料/源资料与可检索性台账|源资料与可检索性台账]]",
                  "- 每类资料怎么处理、样例和确认状态：[[02_源资料/处理计划与样例|处理计划与样例]]"
                  + (f"（还有 {len(waiting)} 类没确认样例）" if waiting else ""),
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
        own = [t for t in topics if t["module"] == key and not t.get("parent") and not t.get("brief")]
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
                single_ok = topic.get("by_line") or topic.get("by_doc")
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


LEDGER_ORDER = ("写进", "原样表", "指针", "重复", "不需要写", "已归类", "决定不写", "未处理")


def ledger_section(ledger: dict[str, Any]) -> list[str]:
    """Every source passage and where it went: the check that nothing was left out of the pages."""
    total = ledger.get("total") or {}
    if not any(total.values()):
        return []
    auto = ledger.get("auto", {})
    lines = ["## 整理覆盖账本", "",
             f"可用原文 {sum(total.values())} 段要在整理页里有去处：" + "，".join(f"{k} {total.get(k, 0)}" for k in LEDGER_ORDER if total.get(k)) + "。"
             f"另有脚本自动处理的：明显噪音 {auto.get('noise', 0)} 段、重复 {auto.get('repeats', 0)} 段（由第一次出现的那段代表）。",
             "“写进”= 写成要点；“原样表”= 原表放进整理页；“指针”= 一句话说明加原文位置；“重复”“不需要写”= AI 在 ledger.jsonl 里登记，"
             "含型号、数字、标准号的段不能标不需要写；“未处理”不是 0 时校验不通过（kb-config.json 写 `\"curation\": {\"ledger\": \"warn\"}` 时只提醒）。", "",
             "| 主题 | " + " | ".join(LEDGER_ORDER) + " |", "| --- |" + " ---: |" * len(LEDGER_ORDER)]
    for slug, row in ledger.get("topics", {}).items():
        lines.append(f"| {row.get('title', slug)}（`{slug}`） | " + " | ".join(str(row.get(k, 0)) for k in LEDGER_ORDER) + " |")
    lines.append("")
    return lines


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
    if state.get("build_stage") == "sources_only":
        lines.extend(["当前仅构建来源层（sources_only）；整理页需获批准后再编写。“有原文没整理”属于当前阶段的正常状态。", ""])
    lines.extend(ledger_section(state.get("ledger", {})))
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
    if state.get("reading"):
        lines.extend(["## PDF 和 PPT 怎么读的", "", "| 类型 | 入库 | 走过 MinerU | 有 Reducto 难页 | 有 AI 读图 | 看图核对过 | 只用文字层 |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        lines.extend(f"| {kind} | {row['files']} | {row['mineru']} | {row.get('reducto', 0)} | {row['ai']} | {row['check']} | {row['text']} |"
                     for kind, row in sorted(state["reading"].items()))
        lines.extend(["", "一份文件可能走过几种，所以各列加起来会多于入库份数。“只用文字层”的要整批补跑 MinerU 时，"
                      "运行 `mineru_ocr.py plan --missing`，用户批准后 `mineru_ocr.py run --missing`。"
                      "Reducto 只读难页，和 MinerU 的对照在 `04_知识库构建/reducto-diff.md`。", ""])
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
    parser.add_argument("--out", help="Run directory (required unless running standalone --vault --monthly or --vault-root)")
    parser.add_argument("--vault", help="Published project folder")
    parser.add_argument("--vault-root", help="Obsidian root folder containing multiple project vaults (batch monthly audit)")
    parser.add_argument("--monthly", action="store_true", help="Write dated monthly health report")
    parser.add_argument("--month", help="Target month YYYY-MM for monthly audit (default: current month)")
    parser.add_argument("--json", action="store_true", help="Print full JSON result")
    return parser.parse_args()


def resolve_month(raw: str | None) -> str:
    month = raw or date.today().strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise SystemExit(f"--month 格式应为 YYYY-MM，收到：{month}")
    return month


def parse_project_info(vault: Path) -> dict[str, str]:
    info_path = vault / "01_工作台/10_项目基础信息.md"
    info: dict[str, str] = {}
    if not info_path.is_file():
        return info
    for line in info_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*-\s*([^：:]+)[：:]\s*(.+?)\s*$", line)
        if m:
            info[m.group(1).strip()] = m.group(2).strip()
    return info


def gather_ledgers_vault(vault: Path) -> tuple[dict[str, str], dict[str, dict[str, int]]]:
    names, ledger_counts = {}, {}
    for kind, stem in (("CUS", "待客户补充事项"), ("MAT", "源资料处理台账"), ("ANM", "源文与事实异常台账")):
        side = any(vault.glob(f"05_数据与审核/30_异常与待决定/*/02_知识库构建_{stem}.md"))
        name = f"02_知识库构建_{stem}.md" if side else f"01_{stem}.md"
        names[kind] = (Path(LEDGERS[kind][0]).parent / name).as_posix()
        ledger = vault / names[kind]
        if ledger.is_file():
            header, rows, _ = parse_ledger(ledger.read_text(encoding="utf-8", errors="replace"))
            stage = header.index("当前阶段") if "当前阶段" in header else None
            closed = sum(stage is not None and row[stage] in CLOSED_STAGES - {SOLVED_STAGE} for row in rows.values())
            solved = sum(stage is not None and row[stage] == SOLVED_STAGE for row in rows.values())
            ledger_counts[kind] = {"pending": len(rows) - closed - solved, "resolved": closed, "system_solved": solved}
        else:
            ledger_counts[kind] = {"pending": 0, "resolved": 0, "system_solved": 0}
    return names, ledger_counts


def gather_articles(vault: Path, month: str) -> dict[str, Any]:
    ledger = vault / "04_文章任务/00_文章台账.md"
    all_rows: list[dict[str, Any]] = []
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
            if len(cells) < 7 or cells[0] == "任务编号" or set(cells[0]) <= set("-: "):
                continue
            task_key, when, title, skill, status, cov, link = cells[:7]
            fact_m = re.search(r"硬事实\s*\d+/\d+[（(](\d+(?:\.\d+)?)%[）)]", cov)
            if not fact_m and cov not in {"", "-"}:
                fact_m = re.search(r"(\d+(?:\.\d+)?)%", cov)
            word_m = re.search(r"正文\s*\d+/\d+词[（(](\d+(?:\.\d+)?)%[）)]", cov)
            trace_file = vault / "04_文章任务/10_写作记录" / task_key / "引用追溯.md"
            has_trace = trace_file.is_file() or (cov not in {"", "-"})
            all_rows.append({
                "task_key": task_key, "date": when, "title": title, "writing_skill": skill,
                "status": status, "coverage": cov, "link": link,
                "fact_rate": float(fact_m.group(1)) if fact_m else None,
                "word_rate": float(word_m.group(1)) if word_m else None,
                "has_trace": has_trace,
            })
    month_rows = [r for r in all_rows if r["date"].startswith(month)]
    fact_rates = [r["fact_rate"] for r in month_rows if r["fact_rate"] is not None]
    word_rates = [r["word_rate"] for r in month_rows if r["word_rate"] is not None]
    return {
        "total": len(all_rows),
        "month_count": len(month_rows),
        "month_rows": month_rows,
        "avg_fact_rate": round(sum(fact_rates) / len(fact_rates), 1) if fact_rates else None,
        "avg_word_rate": round(sum(word_rates) / len(word_rates), 1) if word_rates else None,
        "missing_trace": sum(1 for r in month_rows if not r["has_trace"]),
    }


def gather_website_vault(vault: Path) -> tuple[int, str | None, bool]:
    dates: list[str] = []
    units_dir = vault / "02_源资料/30_官网快照/units"
    pages_json = vault / "02_源资料/30_官网快照/pages.json"
    count = 0
    if pages_json.is_file():
        pages = read(pages_json, [])
        if isinstance(pages, list):
            count = len(pages)
            dates.extend(str(p["fetched_at"])[:10] for p in pages if isinstance(p, dict) and p.get("fetched_at"))
    if not count and units_dir.is_dir():
        unit_files = sorted(units_dir.glob("*.json"))
        count = len(unit_files)
        for uf in unit_files:
            data = read(uf, {})
            if isinstance(data, dict) and data.get("fetched_at"):
                dates.append(str(data["fetched_at"])[:10])
    if not count:
        web_md = vault / "02_源资料/10_净化原文/官网"
        if web_md.is_dir():
            md_files = sorted(web_md.rglob("*.md"))
            count = len(md_files)
            for mf in md_files:
                fields = parse_frontmatter(mf.read_text(encoding="utf-8", errors="replace"))
                if fields.get("fetched_at"):
                    dates.append(str(fields["fetched_at"])[:10])
    valid_dates = [d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    oldest = min(valid_dates) if valid_dates else None
    stale = bool(oldest) and (date.today() - date.fromisoformat(oldest)).days > WEBSITE_STALE_DAYS
    return count, oldest, stale


def gather_coverage_vault(vault: Path) -> dict[str, Any]:
    cov_path = vault / COVERAGE
    unhandled = 0
    section_summary = ""
    uncited_docs = 0
    if cov_path.is_file():
        text = cov_path.read_text(encoding="utf-8", errors="replace")
        m_sec = re.search(r"(?m)^小板块状态：(.+?)。?$", text)
        if m_sec:
            section_summary = m_sec.group(1).strip()
        m_led = re.search(r"可用原文\s*\d+\s*段要在整理页里有去处：([^\n。]+)", text)
        if m_led:
            m_un = re.search(r"未处理\s*(\d+)", m_led.group(1))
            if m_un:
                unhandled = int(m_un.group(1))
        for m_unc in re.finditer(r"有效但没被整理页引用：(\d+)\s*份", text):
            uncited_docs += int(m_unc.group(1))
    return {"unhandled_segments": unhandled, "section_summary": section_summary, "uncited_docs": uncited_docs}


def gather_vault(vault: Path, month: str, token_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gather monthly health metrics directly from a published vault (no --out run folder needed)."""
    from kb_relink import check_links
    token = token_info if token_info is not None else probe()
    info = parse_project_info(vault)
    receipt = vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
    knowledge_root = vault / "03_正式知识/10_客户知识"
    source_root = vault / "02_源资料/10_净化原文"
    knowledge_pages = len(list(knowledge_root.rglob("*.md"))) if knowledge_root.is_dir() else 0
    source_docs = len(list(source_root.rglob("*.md"))) if source_root.is_dir() else 0
    ledger_names, ledger_counts = gather_ledgers_vault(vault)
    website_pages, website_oldest, website_stale = gather_website_vault(vault)
    index_path = vault / "02_源资料/knowledge-index.sqlite"
    index_exists = index_path.is_file()
    index_updated = datetime.fromtimestamp(index_path.stat().st_mtime).strftime("%Y-%m-%d") if index_exists else None
    return {
        "vault": str(vault),
        "vault_name": vault.name,
        "month": month,
        "info": info,
        "last_publish": read(receipt, {}).get("published_at") if receipt.is_file() else None,
        "knowledge_pages": knowledge_pages,
        "has_catalog": (vault / "01_工作台/写作目录.md").is_file(),
        "source_docs": source_docs,
        "ledger_names": ledger_names,
        "ledger_counts": ledger_counts,
        "coverage": gather_coverage_vault(vault),
        "articles": gather_articles(vault, month),
        "external": external_summary(vault),
        "website_pages": website_pages,
        "website_oldest": website_oldest,
        "website_stale": website_stale,
        "links": check_links(vault),
        "index_exists": index_exists,
        "index_updated": index_updated,
        "token": token["expiry"]["message"] if token["ok"] else token["message"],
        "token_days": token["expiry"].get("days_remaining"),
    }


def classify_health(vstate: dict[str, Any]) -> tuple[str, list[str]]:
    red: list[str] = []
    yellow: list[str] = []
    unhandled = vstate["coverage"]["unhandled_segments"]
    if unhandled:
        red.append(f"整理未处理段 {unhandled}")
    links = vstate["links"]
    if links["broken"] or links["wiki_broken"]:
        red.append(f"断链（原件 {links['broken']} / Wiki {links['wiki_broken']}）")
    if not vstate["index_exists"]:
        red.append("缺少检索索引")
    if vstate["token_days"] is not None and vstate["token_days"] <= 7:
        red.append(f"MinerU Token 剩 {vstate['token_days']} 天")

    lc = vstate["ledger_counts"]
    pending_parts = [f"{k} {lc[k]['pending']}" for k in ("CUS", "MAT", "ANM") if lc.get(k, {}).get("pending")]
    if pending_parts:
        yellow.append("待处理 " + "/".join(pending_parts))
    if vstate["website_stale"]:
        yellow.append(f"官网超 {WEBSITE_STALE_DAYS} 天（{vstate['website_oldest']}）")
    ext = vstate.get("external") or {}
    if ext.get("to_spot_check") or ext.get("overdue"):
        yellow.append(f"外部知识 待抽检 {len(ext.get('to_spot_check', []))} / 过期 {len(ext.get('overdue', []))}")
    if vstate["articles"]["missing_trace"]:
        yellow.append(f"本月 {vstate['articles']['missing_trace']} 篇文章缺引用追溯")
    if vstate["coverage"]["uncited_docs"]:
        yellow.append(f"未引用有效原文 {vstate['coverage']['uncited_docs']} 份")

    if red:
        return "🔴 需处理", red + yellow
    if yellow:
        return "🟡 待关注", yellow
    return "🟢 正常", ["无异常"]


def render_vault_monthly(vstate: dict[str, Any]) -> str:
    level, reasons = classify_health(vstate)
    info = vstate["info"]
    title_name = info.get("项目名称") or vstate["vault_name"]
    pid = info.get("项目ID", "")
    owner = info.get("内容运营负责人", "未填写")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 知识库月度体检 {vstate['month']}",
        "",
        f"- 项目：{f'{pid} · ' if pid else ''}{title_name}（`{vstate['vault_name']}`）",
        f"- 负责人：{owner}；检查时间：{now}；最近发布：{vstate['last_publish'] or '未记录'}",
        f"- 体检结论：**{level}**（{'；'.join(reasons)}）",
        f"- 基础盘：整理页 {vstate['knowledge_pages']} 页、来源原文 {vstate['source_docs']} 份、"
        f"写作目录 {'正常' if vstate['has_catalog'] else '缺失'}",
        "",
        "## 一、本月写作与引用覆盖",
        "",
    ]
    art = vstate["articles"]
    avg_fact = f"{art['avg_fact_rate']}%" if art["avg_fact_rate"] is not None else "N/A"
    avg_word = f"{art['avg_word_rate']}%" if art["avg_word_rate"] is not None else "N/A"
    lines.append(f"- 累计文章 {art['total']} 篇；**本月（{vstate['month']}）产出 {art['month_count']} 篇**"
                 f"；本月平均硬事实引用覆盖率：**{avg_fact}**（正文引用占比均值：{avg_word}）；缺引用追溯：{art['missing_trace']} 篇。")
    lines.append("- 说明：引用覆盖率仅作知识库支撑度观察，不设及格线，不直接代表文章质量。")
    if art["month_rows"]:
        lines.extend(["", "| 日期 | 标题 | 写作 Skill | 状态 | 引用覆盖率 | 追溯 |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for row in art["month_rows"]:
            trace_ok = "有" if row["has_trace"] else "⚠ 缺"
            lines.append(f"| {row['date']} | {row['title']} | {row['writing_skill']} | {row['status']} | {row['coverage']} | {trace_ok} |")
    lines.extend(["", "## 二、待处理台账（CUS / MAT / ANM）", ""])
    vault_path = Path(vstate["vault"])
    lc, ln = vstate["ledger_counts"], vstate["ledger_names"]
    for kind, label in (("CUS", "待客户确认"), ("MAT", "资料要处理"), ("ANM", "风险与冲突")):
        c = lc[kind]
        tail = (f" → [[{ln[kind].removesuffix('.md')}|{Path(ln[kind]).stem.removeprefix('01_')}]]"
                if (vault_path / ln[kind]).is_file() else "（暂未建表）")
        lines.append(f"- {label}（{kind}）：待处理 **{c['pending']}** 项；历史解除待关闭 {c['system_solved']} 项；已关闭 {c['resolved']} 项{tail}")

    cov = vstate["coverage"]
    lines.extend(["", "## 三、整理覆盖与缺口", ""])
    if cov["section_summary"]:
        lines.append(f"- 小板块状态：{cov['section_summary']}")
    lines.append(f"- 整理覆盖账本未处理段：**{cov['unhandled_segments']}** 段；有效但未被整理页引用的原文：**{cov['uncited_docs']}** 份。")
    if (vault_path / COVERAGE).is_file():
        lines.append("- 详情见 [[05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口|知识库覆盖与缺口]]。")

    ext = vstate.get("external") or {"pages": 0, "to_spot_check": [], "overdue": [], "todo": 0}
    lines.extend(["", "## 四、外部公共知识（待抽检与复查）", "",
                  f"- 共 {ext['pages']} 页：待抽检 **{len(ext['to_spot_check'])}** 页、已过复查日期 **{len(ext['overdue'])}** 页、待调研缺口 **{ext['todo']}** 个。"])
    if ext["to_spot_check"]:
        lines.append("- 待抽检：" + "、".join(f"[[{Path(p['page']).stem}|{p['title']}]]" for p in ext["to_spot_check"]))
    if ext["overdue"]:
        lines.append("- 已过复查日期：" + "、".join(f"[[{Path(p['page']).stem}|{p['title']}]]（{p['review_due']}）" for p in ext["overdue"]))

    links = vstate["links"]
    web_note = (f"{vstate['website_pages']} 页，最早抓取 {vstate['website_oldest']}"
                + (f"（⚠ 已超 {WEBSITE_STALE_DAYS} 天，建议人工确认后重抓）" if vstate["website_stale"] else "（新鲜）")
                if vstate["website_pages"] else "暂无官网快照")
    idx_note = f"正常（更新于 {vstate['index_updated']}）" if vstate["index_exists"] else "⚠ 缺失（请运行 `kb_index.py` 重建）"
    lines.extend([
        "",
        "## 五、官网新鲜度与链接/检索健康",
        "",
        f"- 官网快照：{web_note}",
        f"- 链接检查：原件链接 {links['file_links']} 条（断链 **{links['broken']}**），Wiki 链接 {links['wiki_links']} 条（断链 **{links['wiki_broken']}**）",
        f"- 检索索引（`knowledge-index.sqlite`）：{idx_note}",
        f"- 本机 MinerU Token：{vstate['token']}",
    ])
    if links["examples"]:
        lines.append("- 断链样例（最多列 5 条）：")
        lines.extend(f"  - `{ex}`" for ex in links["examples"][:5])

    actions: list[str] = []
    if cov["unhandled_segments"]:
        actions.append(f"- 清理整理覆盖账本里剩余的 {cov['unhandled_segments']} 段“未处理”原文")
    if links["broken"] or links["wiki_broken"]:
        actions.append("- 检查断链：若知识库刚跨电脑拷贝，运行 `kb_relink.py --vault <知识库> --package <初筛包>` 修复")
    if not vstate["index_exists"]:
        actions.append("- 运行 `python scripts/kb_index.py --vault <知识库>` 重建检索索引")
    if vstate["website_stale"]:
        actions.append(f"- 官网已超 {WEBSITE_STALE_DAYS} 天未更新，确认需要时用 `fetch_website.py` 重读（变动页才会更新）")
    if ext["to_spot_check"] or ext["overdue"]:
        actions.append("- 抽检外部公共知识的“待抽检”页、重查“已过复查日期”页，完成后用 `research.py mark` 标记")
    if any(lc[k]["pending"] for k in ("CUS", "MAT", "ANM")):
        actions.append("- 跟进待处理 CUS/MAT/ANM；收到客户回复后用 `apply_answers.py` 读回并 `--from guard` 更新")
    if art["missing_trace"]:
        actions.append("- 补全本月缺少引用追溯的文章核对记录（`check_article.py --citations`）")
    if not actions:
        actions.append("- 本月无紧急待办；如有客户新资料，用 `prepare-seo-knowledge-materials` 初筛后增量重跑即可。")
    lines.extend(["", "## 六、本月建议动作", "", *actions, ""])
    return "\n".join(lines)


def is_kb_vault(path: Path) -> bool:
    if not path.is_dir() or path.name.startswith("."):
        return False
    if not (path / "01_工作台").is_dir():
        return False
    markers = (
        path / "01_工作台/00_知识库总览.md",
        path / "01_工作台/写作目录.md",
        path / "05_数据与审核/70_构建状态",
        path / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json",
    )
    return any(m.exists() for m in markers)


def run_batch_monthly(vault_root: Path, month: str) -> dict[str, Any]:
    token_info = probe()
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for child in sorted(vault_root.iterdir(), key=lambda p: p.name.casefold()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not is_kb_vault(child):
            skipped.append(child.name)
            continue
        try:
            vstate = gather_vault(child, month, token_info=token_info)
            level, reasons = classify_health(vstate)
            report_path = child / f"05_数据与审核/40_月度与交接/知识库体检_{month}.md"
            write_text(report_path, render_vault_monthly(vstate))
            results.append({
                "vault_name": child.name,
                "project_name": vstate["info"].get("项目名称") or child.name,
                "owner": vstate["info"].get("内容运营负责人", "-"),
                "level": level,
                "reasons": reasons,
                "report": report_path.relative_to(vault_root).as_posix(),
                "articles_month": vstate["articles"]["month_count"],
                "avg_fact_rate": vstate["articles"]["avg_fact_rate"],
                "pending": {k: vstate["ledger_counts"][k]["pending"] for k in ("CUS", "MAT", "ANM")},
                "unhandled_segments": vstate["coverage"]["unhandled_segments"],
                "external_spot_check": len((vstate.get("external") or {}).get("to_spot_check", [])),
                "external_overdue": len((vstate.get("external") or {}).get("overdue", [])),
                "website_oldest": vstate["website_oldest"],
                "website_stale": vstate["website_stale"],
                "broken_links": vstate["links"]["broken"],
                "wiki_broken": vstate["links"]["wiki_broken"],
                "index_exists": vstate["index_exists"],
            })
        except Exception as exc:  # One bad vault never blocks the rest of the root scan.
            errors.append({"vault": child.name, "error": f"{type(exc).__name__}: {exc}"})

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    token_msg = token_info["expiry"]["message"] if token_info["ok"] else token_info["message"]
    red_n = sum(1 for r in results if r["level"].startswith("🔴"))
    yellow_n = sum(1 for r in results if r["level"].startswith("🟡"))
    green_n = sum(1 for r in results if r["level"].startswith("🟢"))
    lines = [
        f"# 全库月度体检总览 {month}",
        "",
        f"- 扫描根目录：`{vault_root}`",
        f"- 生成时间：{now}；检查项目：{len(results)} 个（🔴 需处理 {red_n}、🟡 待关注 {yellow_n}、🟢 正常 {green_n}）；"
        f"跳过非知识库目录：{len(skipped)} 个；异常：{len(errors)} 个",
        f"- 本机 MinerU Token：{token_msg}",
        "- 官网超期策略：只读检查与提醒，不自动抓网。",
        "",
        "## 一、全项目体检总表",
        "",
        "| 项目 | 本月文章 / 均引用率 | 待处理 (CUS/MAT/ANM) | 整理未处理 | 外部知识 (抽检/过期) | 官网状态 | 断链 (原件/Wiki) | 结论与关注重点 |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for r in results:
        avg_str = f"{r['avg_fact_rate']}%" if r["avg_fact_rate"] is not None else "N/A"
        art_col = f"{r['articles_month']} 篇 / {avg_str}"
        pend_col = f"{r['pending']['CUS']}/{r['pending']['MAT']}/{r['pending']['ANM']}"
        ext_col = f"{r['external_spot_check']}/{r['external_overdue']}"
        if r["website_oldest"]:
            web_col = f"{'⚠ ' if r['website_stale'] else ''}{r['website_oldest']}"
        else:
            web_col = "无"
        link_col = f"{r['broken_links']}/{r['wiki_broken']}"
        report_link = f"[[{r['report'].removesuffix('.md')}|{r['vault_name']}]]"
        note = r["level"] if r["level"].startswith("🟢") else f"{r['level']}：{'；'.join(r['reasons'])}"
        lines.append(f"| {report_link} | {art_col} | {pend_col} | {r['unhandled_segments']} | {ext_col} | {web_col} | {link_col} | {note} |")

    need_action = [r for r in results if not r["level"].startswith("🟢")]
    lines.extend(["", "## 二、本月需要人工处理的重点", ""])
    if need_action:
        for r in need_action:
            lines.append(f"- **{r['vault_name']}**（负责人：{r['owner']}，{r['level']}）：{'；'.join(r['reasons'])} → [[{r['report'].removesuffix('.md')}|查看体检详情]]")
    else:
        lines.append("- 本月所有知识库均正常，无紧急待办。")

    if skipped or errors:
        lines.extend(["", "## 三、跳过与异常目录", ""])
        if skipped:
            lines.append("- 已跳过（非本 Skill 知识库目录）：" + "、".join(f"`{s}`" for s in skipped))
        for err in errors:
            lines.append(f"- ❌ `{err['vault']}` 检查失败：{err['error']}")
    lines.append("")
    summary_path = vault_root / f"月度体检总览_{month}.md"
    write_text(summary_path, "\n".join(lines))
    return {
        "month": month,
        "vault_root": str(vault_root),
        "summary_report": str(summary_path),
        "checked": len(results),
        "red": red_n,
        "yellow": yellow_n,
        "green": green_n,
        "skipped": skipped,
        "errors": errors,
        "token": token_msg,
        "projects": results,
    }


def main() -> int:
    args = parse_args()
    month = resolve_month(args.month)
    if args.vault_root:
        vault_root = Path(args.vault_root).resolve()
        if not vault_root.is_dir():
            raise SystemExit(f"找不到知识库根目录：{vault_root}")
        batch = run_batch_monthly(vault_root, month)
        print(json.dumps(batch, ensure_ascii=False, indent=2))
        return 1 if batch["errors"] else 0
    if not args.out:
        if not args.vault:
            raise SystemExit("请提供 --vault-root（批量月度体检）、--vault --monthly（单库月度体检），或 --out（建库总览）。")
        vault = Path(args.vault).resolve()
        if not vault.is_dir():
            raise SystemExit(f"找不到知识库目录：{vault}")
        vstate = gather_vault(vault, month)
        level, reasons = classify_health(vstate)
        report_path = vault / f"05_数据与审核/40_月度与交接/知识库体检_{month}.md"
        write_text(report_path, render_vault_monthly(vstate))
        out = {
            "vault": str(vault), "month": month, "level": level, "reasons": reasons,
            "report": str(report_path), "articles": vstate["articles"],
            "ledger_counts": vstate["ledger_counts"], "coverage": vstate["coverage"],
            "links": {k: vstate["links"][k] for k in ("file_links", "broken", "wiki_links", "wiki_broken")},
            "website_oldest": vstate["website_oldest"], "website_stale": vstate["website_stale"],
            "token": vstate["token"],
        }
        print(json.dumps(vstate if args.json else out, ensure_ascii=False, indent=2))
        return 0
    if not run_lock(Path(args.out).resolve(), "kb_status.py"):
        return 3
    run = Path(args.out).resolve()
    vault = Path(args.vault).resolve() if args.vault else None
    state = gather(run, vault)
    page = overview(state)
    write_text(run / BUILD_DIRECTORY / "总览.md", "\n".join(page) + "\n")
    if vault:
        from refresh_navigation import Links, RECEIPT, obsidian_prefix
        from publish_vault import normalized_hash
        files = {p.relative_to(vault).as_posix(): p for p in vault.rglob("*")
                 if p.is_file() and "60_版本归档" not in p.parts and "70_构建状态" not in p.parts}
        links = Links(files, obsidian_prefix(vault))
        pages = {OVERVIEW: links.rewrite("\n".join(page) + "\n"),
                 COVERAGE: links.rewrite("\n".join(coverage(state, run)) + "\n")}
        receipt_path = vault / RECEIPT
        receipt = read(receipt_path, {})
        for key in pages:
            digest = receipt.get("files", {}).get(key.as_posix())
            if digest and (vault / key).is_file() and normalized_hash(vault / key) != digest:
                raise SystemExit(f"文件有人修改，不覆盖：{key}")
        for key, text in pages.items():
            write_text(vault / key, text)
            if receipt:
                receipt.setdefault("files", {})[key.as_posix()] = normalized_hash(vault / key)
        if receipt:
            write_json(receipt_path, receipt)
        if args.monthly:
            vstate = gather_vault(vault, month)
            write_text(vault / f"05_数据与审核/40_月度与交接/知识库体检_{month}.md", render_vault_monthly(vstate))
    summary = {key: state[key] for key in ("verified", "ledgers", "terms_to_review", "cells", "t3", "token", "last_publish")}
    print(json.dumps({**summary, "knowledge_pages": len(state["knowledge"]), "source_docs": len(state["published"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
