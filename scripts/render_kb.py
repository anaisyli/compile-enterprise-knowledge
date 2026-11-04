#!/usr/bin/env python3
"""Write the source layer, the ledgers, the source register, and the exports.

The source layer is the cleaned original: text is only deleted, never rewritten. Each
document opens with a card saying where it came from. Risky passages stay in, marked,
and point to the ledger entry that explains them.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from governance import build as build_governance, cleanup_record
from guard_blocks import HARD_PATTERNS, compile_terms
from kb_common import (
    BUILD_DIRECTORY,
    CLEAN_DIRECTORY,
    ROLE_LABELS,
    ROLE_ORDER,
    SKILL_VERSION,
    TIERS,
    frontmatter,
    load_config,
    load_json,
    load_jsonl,
    remaining_ocr_pages,
    safe_name,
    write_json,
    write_jsonl,
    write_text,
)
from review_cells import progress_of, t3_gate


SOURCE_ROOT = "02_源资料/10_净化原文"
REGISTER = "02_源资料/源资料与可检索性台账.md"
MAX_STYLE_IMAGES = 2
PAGE_PICTURE_WIDTH = 360
TABLE_SUMMARY = "产品表/00_产品表汇总.md"
DECISION_LABELS = {"A": "A 原文保留", "B": "B 待清理", "C": "C 提取知识点", "D": "D 待定", "E": "E 排除", "web": "官网页面"}
REASON_LABELS = {
    "excluded": "初筛排除", "duplicate": "重复", "website_duplicate": "网页重复", "website_listing": "分类列表页",
    "third_party_material": "竞品或第三方资料", "writing_operations_material": "写作运营资料", "not_copied_restricted": "受限未复制",
    "no_extractable_content": "读不出可用内容", "unresolved_item": "初筛待定，未确认", "image_needs_review": "图片待识别",
    "unreadable_removal_location": "清理位置看不懂", "client_said_no": "客户说不用", "unknown_decision": "初筛分类不明",
    "website_needs_browser": "要用浏览器读取",
}
VISIBLE = {"kept", "flagged", "pending_client"}
MASK = "〔客户〕"
HIDDEN_NAME = "〔文件名可能含客户名，已隐去；用资料编号查原件〕"
QUOTE_NAME = re.compile(r"(?i)报价|quot|quat|offer|\bpi\b|proforma|invoice|订单|order|客户|客人|\bfor\b")
ASSET_INDEX = "03_正式知识/90_素材/00_素材索引.md"
SEARCH_STATE = {"read_ok": "全文文字可搜索", "partial": "部分内容可搜索", "needs_ocr": "未建立正文索引"}


def escape_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def doc_path(doc: dict[str, Any]) -> str:
    return f"{doc['folder']}/{doc['file_name']}"


def doc_status(doc: dict[str, Any], blocks: list[dict[str, Any]], effective: set[str]) -> str:
    if doc.get("role") == "reference":
        return "参考稿"
    if doc["tier"] not in effective:
        return "待审"
    pending = sum(1 for b in blocks if b["status"] == "pending_client")
    if pending and pending == len(blocks):
        return "待客户确认"
    return "部分待客户确认" if pending else "有效"


def masker(pattern: re.Pattern[str] | None) -> Callable[[str], str]:
    """Replace customer names with 〔客户〕 and keep the rest, so the file can still be found."""
    def mask(text: str) -> str:
        if not pattern or not text:
            return text or ""
        # Only the name is masked; the extension and the folders stay, so the file can be found.
        parts = []
        for part in text.split("/"):
            suffix = Path(part).suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", Path(part).suffix or "") else ""
            stem = part[: len(part) - len(suffix)] if suffix else part
            parts.append(pattern.sub(MASK, stem) + suffix)
        return "/".join(parts)
    return mask


def hides_name(name: str, tier: str = "") -> bool:
    """Quotations are named after the customer they went to, and the dictionary cannot know
    every customer. Product tables, and anything named like a quote or an order, hide the name."""
    return tier == "T3" or Path(name).suffix.casefold() in {".xls", ".xlsx", ".csv"} or bool(QUOTE_NAME.search(Path(name).stem))


def shown_name(name: str, mask: Callable[[str], str], tier: str = "") -> str:
    if not name or not hides_name(name, tier):
        return mask(name)
    return f"{HIDDEN_NAME}{Path(name).suffix}"


def shown_path(path: str, mask: Callable[[str], str], tier: str = "") -> str:
    if not path or path.startswith("http"):
        return path
    folder, _, name = path.rpartition("/")
    return "/".join(part for part in (mask(folder), shown_name(name, mask, tier)) if part)


def source_card(doc: dict[str, Any], status: str, mask: Callable[[str], str], issue_links: list[str], layout: str = "") -> list[str]:
    tier = TIERS[doc["tier"]]
    origin = doc.get("package_path") or ""
    if origin.startswith("http"):
        location = f"[{origin}]({origin})"
    else:
        package = doc.get("package_name") or "初筛包"
        location = f"初筛包 `{package}` → `{shown_path(origin, mask, doc['tier'])}`" if origin else f"初筛包 `{package}`"
    rows = [
        f"> **来源**：{doc.get('source_label', '')}",
        f"> - 原标题 / 原文件名：{shown_name(doc.get('original_name') or '', mask, doc['tier'])}",
        f"> - 原始位置：{location}",
        f"> - 资料编号：`{doc['item_id']}`（找原件：查原件 {doc['title']}）",
    ]
    if doc.get("pages"):
        rows.append(f"> - 页数：{doc['pages']}")
    if doc.get("fetched_at"):
        rows.append(f"> - 抓取时间：{doc['fetched_at']}")
    if doc.get("source_sha256"):
        rows.append(f"> - SHA-256：`{doc['source_sha256'][:16]}`")
    rows.append(f"> - 来源级别：{doc['tier']} {tier['label']}（只说明来源，不等于每句都已核实）")
    rows.append(f"> - 状态：{status}" + (f"；相关事项：{'、'.join(issue_links)}" if issue_links else ""))
    rows.append("> - 本文件只删除不改写；删除了什么见《内容清理记录》。")
    if doc.get("role") == "reference":
        rows.append("> - ⚠ 参考整理稿：这份是从别家公开资料或旧稿整理出来的参考，不是公司自己的资料。"
                    "里面的参数、款式不能直接当公司事实用；整理页引用时带 ⚠，并找公司自己的资料印证。")
    if layout == "columns":
        rows.append("> - 版面：同一页的文字按栏分段，款号和它下面的特点在同一段；原件没写款号的栏，就没有款号，不要套用别栏的。"
                    "没删过内容的页附了原页图，可以直接对照。")
    elif layout == "stored_order" and doc.get("folder") == "产品画册":
        rows.append("> - ⚠ 版面：这份按文件里的存储顺序读取（本机没装 PyMuPDF），一页多款时款号和特点可能错位，用前对照原件。")
    return rows


def flag_note(block: dict[str, Any], anm_ids: dict[str, str]) -> str:
    notes = []
    for flag in block.get("flags", []):
        anm = anm_ids.get(flag["type"], "")
        if flag["type"] == "unverified_name":
            notes.append(f"⚠ 含没核实的词：{'、'.join(flag['terms'][:6])}（{anm}）")
        elif flag["type"] == "report_page":
            notes.append(f"⚠ 检测或审核报告页，引用前核对日期（{anm}）")
        elif flag["type"] == "brand_hidden":
            # The word itself may be the customer's name, so it is not repeated here.
            notes.append("⚠ 隐去了含品牌词的格（可能是客户品牌，词在待判断清单里）")
        elif flag["type"] == "reference_draft":
            continue  # Said once on the card; repeating it on every paragraph buries the text.
        elif flag["type"] == "ai_read":
            notes.append(f"⚠ AI 读图转写，待对照原图核对（{anm}）")
    return "；".join(notes)


def render_doc(doc: dict[str, Any], blocks: list[dict[str, Any]], status: str, card: list[str],
               anm_ids: dict[str, str], cus_ids: dict[str, str], pictures: dict[str, str] | None = None) -> str:
    """pictures: page heading -> picture of that page, for pages nothing was removed from."""
    pictures = pictures or {}
    tier = TIERS[doc["tier"]]
    fields = {
        "doc_id": doc["doc_id"], "title": doc["title"], "layer": "来源层", "tier": doc["tier"], "tier_label": tier["label"],
        "visibility": tier["visibility"] if status == "有效" else "internal", "uses": tier["uses"] if status == "有效" else [],
        "status": status, "module": doc["module"], "source_item_id": doc["item_id"], "source_sha256": doc["source_sha256"],
        "flags": sum(1 for b in blocks if b.get("flags")), "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}",
    }
    parts = [frontmatter(fields), "", f"# {doc['title']}", "", *card]
    current, table_columns, show_images, pending_marked = None, [], False, set()
    for block in blocks:
        if block["group"] != current:
            current = block["group"]
            parts.extend(["", f"## {current}", ""])
            if current in pictures:
                parts.extend([f"![[{pictures[current]}|{PAGE_PICTURE_WIDTH}]]", ""])
            table_columns = []
        if block["status"] == "pending_client" and (block["group"], block["client_hold"]) not in pending_marked:
            pending_marked.add((block["group"], block["client_hold"]))
            hold = block["client_hold"]
            whole = hold.startswith("整份文件") or hold == "官网客户案例页"
            cus = cus_ids.get(f"{doc['item_id']}:{'' if whole else hold}", "")
            parts.extend([f"> 🕒 待客户确认（{cus}）：{block['client_hold']}，回复前不要对外使用。", ""])
        note = flag_note(block, anm_ids)
        if block["kind"] == "row":
            if not table_columns:
                group_rows = [b for b in blocks if b["group"] == current]
                used = {role for b in group_rows for role in b["cells"]}
                table_columns = [role for role in ROLE_ORDER if role in used]
                show_images = any(b.get("images") for b in group_rows)
                header = ["行"] + [ROLE_LABELS[role] for role in table_columns] + (["图片"] if show_images else [])
                parts.append("| " + " | ".join(header) + " |")
                parts.append("|" + " --- |" * len(header))
            row = [block["locator"].rsplit(" ", 2)[-2] + (" ⚠" if note else "")]
            row += [escape_cell(block["cells"].get(role, "")) for role in table_columns]
            if show_images:
                row.append(" ".join(f"![[{name}|80]]" for name in block.get("images", [])[:MAX_STYLE_IMAGES]))
            parts.append("| " + " | ".join(row) + " |")
        elif block["kind"] == "point":
            parts.append(f"- [{block.get('scope', 'single_communication')}] {block['text']}" + (f" {note}" if note else ""))
        else:
            if note:
                parts.append(f"> {note}")
                parts.append("")
            parts.extend([block["text"], ""])
    return "\n".join(parts).rstrip() + "\n"


def style_key(text: str) -> str:
    return re.sub(r"[\s\-_/]+", "", text).upper()


def product_master(kept: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge rows by style. Differing values stay side by side: picking one is a guess."""
    styles: dict[str, dict[str, Any]] = {}
    for block in kept:
        if block["kind"] != "row" or not block["cells"].get("style"):
            continue
        entry = styles.setdefault(style_key(block["cells"]["style"]),
                                  {"style": block["cells"]["style"], "values": defaultdict(list), "sources": [], "images": []})
        for role in ("name", "description", "fabric", "size"):
            value = block["cells"].get(role)
            if value and value not in entry["values"][role]:
                entry["values"][role].append(value)
        title = docs[block["doc_id"]]["title"]
        if title not in entry["sources"]:
            entry["sources"].append(title)
        entry["images"].extend(i for i in block.get("images", []) if i not in entry["images"])
    for entry in styles.values():
        entry["conflict"] = len(entry["values"]["fabric"]) > 1
    return sorted(styles.values(), key=lambda entry: entry["style"])


def neutral_title(doc: dict[str, Any]) -> None:
    """A title becomes a file name and a link, so it must pass the same rules as text."""
    if any(pattern.search(doc["title"]) for pattern in HARD_PATTERNS.values()):
        prefix = doc["title"].split("-", 1)[0] if "-" in doc["title"] else "资料"
        doc["title"] = f"{prefix}-{doc['doc_id'][-6:]}"
        doc["file_name"] = safe_name(doc["title"]) + ".md"


def write_asset_index(build: Path, visible: list[dict[str, Any]], docs: dict[str, dict[str, Any]], published: dict[str, str]) -> None:
    """Pictures stay in 90_素材 for good; this page says where each one came from."""
    # One row per picture, without previews: thousands of embedded images make Obsidian crawl.
    uses: dict[str, dict[str, tuple[str, str]]] = {}
    for block in visible:
        doc = docs[block["doc_id"]]
        if doc["doc_id"] not in published:
            continue
        for name in block.get("images", []):
            uses.setdefault(name, {}).setdefault(doc["doc_id"], (block["group"], block["locator"]))
    rows = []
    for name, by_doc in sorted(uses.items()):
        first = min(by_doc, key=lambda d: docs[d]["title"])
        group, locator = by_doc[first]
        doc = docs[first]
        link = f"[[{Path(doc['file_name']).stem}#{group}|{doc['title']}]]"
        rows.append(f"| [[{name}]] | {link}{f' 等 {len(by_doc)} 份' if len(by_doc) > 1 else ''} | {escape_cell(locator)} |")
    lines = ["# 素材索引", "", "图片发布后一直保留，不随资料变化删除。这里每张图一行，写第一次出现的资料和位置；点文件名看图。", "",
             f"共 {len(rows)} 张。", "", "| 图片 | 出现在 | 位置 |", "| --- | --- | --- |", *rows]
    write_text(build / "vault" / ASSET_INDEX, "\n".join(lines) + "\n")


def write_table_summary(clean: Path, visible: list[dict[str, Any]], docs: dict[str, dict[str, Any]],
                        statuses: dict[str, str]) -> int:
    """Every product row kept from every product table and quotation, once, with where it came from.

    A quotation keeps one or two lines once prices and the customer are gone; read together
    they are the company's product range."""
    entries: dict[str, dict[str, Any]] = {}
    for block in visible:
        doc = docs[block["doc_id"]]
        if block["kind"] != "row" or block["status"] == "pending_client" or block["doc_id"] not in statuses:
            continue
        cells = block["cells"]
        key = style_key(cells["style"]) if cells.get("style") else squeeze_key(cells.get("description", "") + cells.get("fabric", "") + cells.get("name", ""))
        entry = entries.setdefault(key, {"values": defaultdict(list), "sources": [], "images": [], "flagged": False})
        for role in ("style", "name", "description", "fabric", "size"):
            if cells.get(role) and cells[role] not in entry["values"][role]:
                entry["values"][role].append(cells[role])
        link = f"[[{Path(doc['file_name']).stem}#{block['group']}|{doc['title']}]]"
        if link not in entry["sources"]:
            entry["sources"].append(link)
        entry["images"].extend(name for name in block.get("images", []) if name not in entry["images"])
        entry["flagged"] = entry["flagged"] or bool(block.get("flags"))
    if not entries:
        return 0
    tables = [d for d in statuses if docs[d]["tier"] == "T3"]
    status = "有效" if tables and all(statuses[d] == "有效" for d in tables) else "待审"
    fields = {"doc_id": "SUM-product-tables", "title": "产品表汇总", "layer": "来源层汇总", "tier": "T3", "tier_label": TIERS["T3"]["label"],
              "visibility": "internal", "uses": TIERS["T3"]["uses"] if status == "有效" else [], "status": status,
              "module": "20_产品介绍", "source_item_id": "product-tables", "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}
    lines = [frontmatter(fields), "", "# 产品表汇总", "",
             f"> 所有产品表和报价单里保留下来的产品行，同一款号（没有款号的按描述和面料）只列一次，共 {len(entries)} 条。",
             "> 价格、数量、客户信息已按规则删除；每行链接到原表，原表的状态就是这一行的状态。"
             + ("" if status == "有效" else "产品表还没过逐格判断，现在是“待审”，不能当已确认的产品事实用。"), "",
             "| 款号 | 品名 | 描述 | 面料 | 尺码 | 图片 | 出现在 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for key, entry in sorted(entries.items(), key=lambda kv: (not kv[1]["values"]["style"], kv[0])):
        values = entry["values"]
        cell = lambda role: escape_cell(" / ".join(values[role][:3]) + (" 等" if len(values[role]) > 3 else ""))
        sources = "、".join(entry["sources"][:3]) + (f" 等 {len(entry['sources'])} 份" if len(entry["sources"]) > 3 else "")
        picture = f"![[{entry['images'][0]}|80]]" if entry["images"] else ""
        lines.append(f"| {cell('style')}{' ⚠' if entry['flagged'] else ''} | {cell('name')} | {cell('description')} | {cell('fabric')} "
                     f"| {cell('size')} | {picture} | {sources} |")
    write_text(clean / TABLE_SUMMARY, "\n".join(lines) + "\n")
    return len(entries)


def squeeze_key(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text).casefold()


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason)


def package_reconciliation(build: Path, routes: dict[str, Any], docs: dict[str, dict[str, Any]], published: dict[str, str],
                           statuses: dict[str, str], status_info: dict[str, Any]) -> list[str]:
    """Each screening category A–E: how many files, how many entered, and how far each got."""
    by_item = {d["item_id"]: d for d in docs.values() if d["doc_id"] in published}
    lines = ["## 初筛包处理对照", "",
             "按初筛分类逐类对照：包里有多少、进了知识库多少、进来以后到了哪一步、没进的原因。每次构建自动更新。", "",
             "| 初筛类别 | 包内 | 入库 | 入库后状态 | 图片页待识别 | 暂缓 | 不入库 |", "| --- | ---: | ---: | --- | --- | --- | --- |"]
    for code, label in DECISION_LABELS.items():
        members = {item: r for item, r in routes.items() if (r.get("decision") or "") == code}
        if not members:
            continue
        entered = [by_item[item] for item in members if item in by_item]
        if code == "C":
            points = [d for d in docs.values() if d.get("origin") == "points" and d["doc_id"] in published]
            entered_count = f"{sum(1 for r in members.values() if r['route'] == 'points')} 份→汇总成 {len(points)} 份《业务要点》"
            state = "、".join(f"{s} {n}" for s, n in Counter(statuses[d["doc_id"]] for d in points).most_common())
        else:
            entered_count = str(len(entered))
            state = "、".join(f"{s} {n}" for s, n in Counter(statuses[d["doc_id"]] for d in entered).most_common())
        waiting = [(d, remaining_ocr_pages(build, d["item_id"], status_info.get(d["item_id"], {}).get("ocr_pages") or [])) for d in entered]
        waiting = [(d, pages) for d, pages in waiting if pages]
        ocr = f"{len(waiting)} 份共 {sum(len(p) for _, p in waiting)} 页" if waiting else ""
        held = Counter(reason_label(r["reason"]) for r in members.values() if r["route"] == "hold")
        skipped = Counter(reason_label(r["reason"]) for r in members.values() if r["route"] == "skip")
        summary = lambda counts: (f"{sum(counts.values())}（" + "、".join(f"{k} {v}" for k, v in counts.most_common()) + "）") if counts else ""
        lines.append(f"| {label} | {len(members)} | {entered_count} | {state} | {ocr} | {summary(held)} | {summary(skipped)} |")
    lines.extend(["", "“待审”是产品表和邮件要点，过了逐格判断或人工核对才算有效；“待客户确认”要等客户回复；"
                  "图片页待识别要走 MinerU，用不了时用 AI 读图。", ""])
    return lines


def write_register(build: Path, routes: dict[str, Any], docs: dict[str, dict[str, Any]], published: dict[str, str],
                   statuses: dict[str, str], status_info: dict[str, Any], mask: Callable[[str], str], issues: dict[str, list[str]],
                   counts: dict[str, Counter] | None = None) -> None:
    """One row per document that entered; excluded material is only counted, never listed by name."""
    counts = counts or {"kept": Counter(), "removed": Counter()}
    columns = ["资料ID", "新标题", "原名称", "来源类型", "原始位置", "格式", "SHA-256", "可检索状态", "页数", "待识别页",
               "规则删掉", "处理去向", "来源级别", "关联事项"]
    lines = ["# 源资料与可检索性台账", "", f"- 净化原文目录：`{SOURCE_ROOT}`", "- 机器索引：`02_源资料/knowledge-index.sqlite`",
             "- 说明：原文件留在初筛包或客户官网；这里登记每份资料的身份、能否检索、提取到什么程度和去向。", "",
             *package_reconciliation(build, routes, docs, published, statuses, status_info),
             "## 已入库资料", "",
             "“待识别页”是没有文字层、MinerU 和 AI 读图都还没读过的页；“规则删掉”是按价格、客户信息等规则删掉的段落数和占比。", "",
             "| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for doc_id, path in sorted(published.items(), key=lambda kv: kv[1]):
        doc = docs[doc_id]
        info = status_info.get(doc["item_id"], {})
        origin = doc.get("package_path") or ""
        waiting = remaining_ocr_pages(build, doc["item_id"], info.get("ocr_pages") or [])
        state = "全文文字可搜索" if doc.get("origin") == "website" else SEARCH_STATE.get(info.get("status"), "全文文字可搜索")
        if waiting and state == "全文文字可搜索":
            state = "部分内容可搜索"
        kept, removed = counts["kept"].get(doc_id, 0), counts["removed"].get(doc_id, 0)
        share = f"{removed} 段（{removed * 100 // (kept + removed)}%）" if removed else "0"
        pages = f"第 {'、'.join(map(str, waiting[:8]))}{' 等' if len(waiting) > 8 else ''} 页（共 {len(waiting)} 页）" if waiting else ""
        row = [doc["doc_id"], f"[[{Path(doc['file_name']).stem}]]", shown_name(doc.get("original_name", ""), mask, doc["tier"]),
               doc.get("source_label", ""), shown_path(origin, mask, doc["tier"]),
               Path(doc.get("original_name") or "").suffix.lstrip(".") or ("网页" if origin.startswith("http") else ""),
               doc["source_sha256"][:12], state, str(info.get("pages") or ""), pages, share,
               statuses.get(doc_id, ""), doc["tier"], "、".join(issues.get(doc["item_id"], []))]
        lines.append("| " + " | ".join(escape_cell(c) for c in row) + " |")
    held = [r for r in routes.values() if r["route"] == "hold"]
    lines.extend(["", "## 暂缓的资料", "", "| 原名称 | 原因 |", "| --- | --- |"])
    lines.extend(f"| {escape_cell(shown_name(r['display_name'], mask))} | {reason_label(r['reason'])} |" for r in held)
    skipped = Counter(reason_label(r["reason"]) for r in routes.values() if r["route"] == "skip")
    lines.extend(["", "## 不入库的资料（只计数）", "", "| 原因 | 数量 |", "| --- | ---: |"])
    lines.extend(f"| {reason} | {count} |" for reason, count in skipped.most_common())
    write_text(build / "vault" / REGISTER, "\n".join(lines) + "\n")


def write_exports(build: Path, visible: list[dict[str, Any]], docs: dict[str, dict[str, Any]], styles: list[dict[str, Any]],
                  statuses: dict[str, str]) -> None:
    exports = build / "exports"
    records = []
    for block in visible:
        doc = docs[block["doc_id"]]
        status = "待客户确认" if block["status"] == "pending_client" else statuses[doc["doc_id"]]
        public = TIERS[doc["tier"]]["visibility"] == "public" and status == "有效" and not block.get("flags")
        records.append({"block_id": block["block_id"], "doc_id": doc["doc_id"], "title": doc["title"], "module": doc["module"],
                        "tier": doc["tier"], "status": status, "visibility": "public" if public else "internal",
                        "flags": [f["type"] for f in block.get("flags", [])], "section": block["group"],
                        "locator": "" if doc["tier"] == "T3" else block["locator"], "text": block["text"]})
    write_jsonl(exports / "blocks-all.jsonl", records)
    write_jsonl(exports / "blocks-public.jsonl", [r for r in records if r["visibility"] == "public"])
    with (exports / "product-master.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["款号", "品名", "描述", "面料", "尺码", "面料说法不一致", "来源数", "图片"])
        for entry in styles:
            writer.writerow([entry["style"]] + [" / ".join(entry["values"][r]) for r in ("name", "description", "fabric", "size")]
                            + ["是" if entry["conflict"] else "", len(entry["sources"]), " ".join(entry["images"])])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the source layer, ledgers, register, and exports.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--tiers", default="T1,T2", help="Tiers that count as valid; the others are published as 待审")
    parser.add_argument("--skip-t3-gate", action="store_true", help="Tests only: treat T3 as reviewed")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run = Path(args.out).resolve()
    build, clean = run / BUILD_DIRECTORY, run / CLEAN_DIRECTORY
    config = load_config(Path(args.config) if args.config else None)
    passed, reason = t3_gate(run)
    effective = set(args.tiers.split(","))
    if "T3" in effective and not (passed or args.skip_t3_gate):
        effective.discard("T3")  # Still published, as 待审, until the review gate passes.
    for directory in (clean, build / "vault"):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

    routes = load_json(build / "routes.json")
    docs = {doc["doc_id"]: doc for doc in load_jsonl(build / "docs.jsonl")}
    blocks = load_jsonl(build / "blocks.jsonl")
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        if block["status"] in VISIBLE:
            by_doc[block["doc_id"]].append(block)

    # Saved-again copies differ in bytes but say the same thing; publish one.
    same_content, fingerprints, names_seen = {}, {}, Counter()
    order = []
    for doc_id, doc in docs.items():
        members = by_doc.get(doc_id, [])
        if not members:
            continue
        neutral_title(doc)
        fingerprint = hash(tuple(sorted(b["text"] for b in members)))
        if fingerprint in fingerprints:
            same_content[doc["item_id"]] = docs[fingerprints[fingerprint]]["item_id"]
            continue
        fingerprints[fingerprint] = doc_id
        names_seen[(doc["folder"], doc["file_name"])] += 1
        if names_seen[(doc["folder"], doc["file_name"])] > 1:
            doc["file_name"] = f"{Path(doc['file_name']).stem}_{doc_id[-6:]}.md"
        order.append(doc_id)

    visible = [b for doc_id in order for b in by_doc[doc_id]]
    styles = product_master([b for b in visible if b["status"] != "pending_client"], docs)
    status_path = build / "package-text" / "extract-status.json"
    status_info = load_json(status_path)["items"] if status_path.is_file() else {}
    terms = load_json(build / "guard-terms.json")
    mask = masker(compile_terms(terms["soft"] + terms["hard"] + config["deny_terms"]))
    gov = build_governance(config.get("project_id", "P-000"), build, routes, docs, blocks, styles,
                           load_json(build / "sheet-report.json"), load_jsonl(build / "review-queue.jsonl"),
                           status_info, (passed or args.skip_t3_gate, reason), progress_of(run), effective,
                           show=lambda name: shown_name(name, mask))
    cleanup_record(build, blocks, docs)
    issues: dict[str, list[str]] = defaultdict(list)
    for item_key, record_id in gov["cus_items"].items():
        if record_id not in issues[item_key.split(":")[0]]:
            issues[item_key.split(":")[0]].append(record_id)
    published, statuses = {}, {}
    # A page picture shows the whole page, so only pages nothing was removed from or held on get one.
    unsafe = {(b["doc_id"], b["group"]) for b in blocks if b["status"] in {"removed", "pending_client"}}
    for doc_id in order:
        doc, members = docs[doc_id], by_doc[doc_id]
        status = doc_status(doc, members, effective)
        links = sorted(set(issues.get(doc["item_id"], [])) | {gov["anm_ids"][f["type"]] for b in members for f in b.get("flags", []) if f["type"] in gov["anm_ids"]})
        card = source_card(doc, status, mask, links, status_info.get(doc["item_id"], {}).get("layout", ""))
        pictures = {f"第 {page} 页": name for page, name in status_info.get(doc["item_id"], {}).get("page_images", {}).items()
                    if (doc_id, f"第 {page} 页") not in unsafe and (build / "assets" / name).is_file()}
        write_text(clean / doc_path(doc), render_doc(doc, members, status, card, gov["anm_ids"], gov["cus_items"], pictures))
        published[doc_id], statuses[doc_id] = doc_path(doc), status

    write_table_summary(clean, visible, docs, statuses)
    counts = {"kept": Counter(b["doc_id"] for b in visible), "removed": Counter(b["doc_id"] for b in blocks if b["status"] == "removed")}
    write_register(build, routes, docs, published, statuses, status_info, mask, issues, counts)
    write_asset_index(build, visible, docs, published)
    write_exports(build, visible, docs, styles, statuses)
    write_jsonl(build / "docs.jsonl", docs.values())
    write_json(build / "published.json", published)
    write_json(build / "statuses.json", statuses)
    write_json(build / "governance-summary.json", gov["counts"])
    kept_counts = Counter(b["doc_id"] for b in visible)
    removed = Counter(b["doc_id"] for b in blocks if b["status"] == "removed")
    with (build / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["item_id", "文件", "去向", "原因", "入库段数", "删除段数", "入库文件"])
        by_item = {d["item_id"]: d for d in docs.values()}
        for item_id, route in routes.items():
            doc = by_item.get(item_id)
            writer.writerow([item_id, route["display_name"], route["route"],
                             f"same_content_as:{same_content[item_id]}" if item_id in same_content else route["reason"],
                             kept_counts.get(doc["doc_id"], 0) if doc else 0, removed.get(doc["doc_id"], 0) if doc else 0,
                             published.get(doc["doc_id"], "") if doc else ""])
    print(json.dumps({"documents": len(published), "status": dict(Counter(statuses.values())), "blocks": len(visible),
                      "flagged": sum(1 for b in visible if b.get("flags")), "styles": len(styles), "ledgers": gov["counts"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
