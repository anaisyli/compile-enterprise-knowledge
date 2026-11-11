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
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from governance import build as build_governance, cleanup_record
from pictures import build_pictures, load_media, page_group, write_index as write_picture_index
from products import Matcher, catalog as product_catalog, place_media
from site_view import write_site
from guard_blocks import HARD_PATTERNS, compile_terms
from kb_common import (
    unapproved_d,
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
from source_fidelity import literal_dimensions, merged_sheet_table
from samples import (METHOD_HELP, METHODS, PAGED_KINDS, PLAN_PAGE, STRUCTURE_PREFIX, TYPES, choose_samples, doc_type, is_confirmed,
                     load_decisions, load_gate, method_of, status_of)


SOURCE_ROOT = "02_源资料/10_净化原文"
REGISTER = "02_源资料/源资料与可检索性台账.md"
MAX_STYLE_IMAGES = 2
PAGE_PICTURE_WIDTH = 360
TABLE_SUMMARY = "产品表/00_产品表汇总.md"
DECISION_LABELS = {"A": "A 原文保留", "B": "B 待清理", "C": "C 提取知识点", "D": "D 待定", "E": "E 排除", "web": "官网页面"}
REASON_LABELS = {
    "user_excluded": "用户指定不处理",
    "excluded": "初筛排除", "duplicate": "重复", "website_duplicate": "网页重复", "website_listing": "分类列表页",
    "third_party_material": "竞品或第三方资料", "writing_operations_material": "写作运营资料", "not_copied_restricted": "受限未复制",
    "no_extractable_content": "读不出可用内容", "unresolved_item": "D 类待决定（客户和内部都还没定）", "image_needs_review": "图片待识别",
    "unreadable_removal_location": "清理位置看不懂", "client_said_no": "客户说不用", "unknown_decision": "初筛分类不明",
    "website_needs_browser": "要用浏览器读取", "awaiting_sample": "等样例确认后提取",
    "media_kept": "按文件夹保留的图片和视频", "dropped_internally": "D 类，内部判断不用", "other_format": "同一份资料的另一种格式", "media_awaiting_client": "整个文件夹待客户确认",
}
VISIBLE = {"kept", "flagged", "pending_client"}
# Inside each package: the knowledge base's name for every file beside its original name.
NAME_MAP_PAGE = "审计记录/知识库文件对照.md"
NAME_MAP_CSV = "审计记录/知识库文件对照.csv"
ALIAS_FOLDER = "审计记录/按知识库名字"
MARKER_PLAN = "<!-- compile-enterprise-knowledge plan -->"
TABLE_LOCATOR = re.compile(r"^table at \d+$| table \d+$|^sheet .+ \(visible\)$")
SEARCH_STATE = {"read_ok": "全文文字可搜索", "partial": "部分内容可搜索", "needs_ocr": "未建立正文索引"}


# Picture id (img-<hash>) -> its file name in 90_素材, set once per build before pages are written.
PICTURE_FILES: dict[str, str] = {}


def shown(name: str) -> str:
    return PICTURE_FILES.get(name, name)


def escape_cell(text: str) -> str:
    return literal_dimensions(str(text).replace("|", "\\|").replace("\n", " "))


def doc_path(doc: dict[str, Any]) -> str:
    return f"{doc['folder']}/{doc['file_name']}"


def doc_status(doc: dict[str, Any], blocks: list[dict[str, Any]], effective: set[str]) -> str:
    if doc.get("role") == "reference":
        return "参考稿"
    if doc["tier"] not in effective or doc.get("d_unapproved"):
        return "待审"  # A kept D file counts once someone who looked at it has approved it.
    pending = sum(1 for b in blocks if b["status"] == "pending_client")
    if pending and pending == len(blocks):
        return "待客户确认"
    return "部分待客户确认" if pending else "有效"


def source_card(doc: dict[str, Any], status: str, issue_links: list[str], layout: str = "", original: str = "",
                twin: str | None = None) -> list[str]:
    """Where a document came from. Original file names are not printed: they often name the
    customer a quote went to. The package's name map pairs each name here with the original."""
    tier = TIERS[doc["tier"]]
    origin = doc.get("package_path") or ""
    rows = [f"> **来源**：{doc.get('source_label', '')}"]
    if origin.startswith("http"):
        rows += [f"> - 原标题：{doc.get('original_name') or ''}", f"> - 原始位置：[{origin}]({origin})"]
    elif doc.get("origin") == "points":
        rows.append(f"> - 原始位置：初筛包 `{doc.get('package_name') or '初筛包'}` → `{origin}`")
    else:
        opener = f"[打开原件]({original})；" if original else ""
        rows.append(f"> - 原文件：{opener}在初筛包 `{doc.get('package_name') or '初筛包'}` 里，原名和位置见初筛包的 `{NAME_MAP_PAGE}`，"
                    f"按本页名字“{doc['title']}”查")
        if twin is not None:
            # The same document saved as PDF too; it is not read twice, but stays one click away.
            rows.append("> - 同一份资料还有 PDF 版" + (f"：[打开原件]({twin})" if twin else "，在初筛包同一个文件夹或旁边的 PDF 文件夹里")
                        + "；内容相同，没有另做一页")
    rows.append(f"> - 资料编号：`{doc['item_id']}`")
    if doc.get("pages"):
        rows.append(f"> - 页数：{doc['pages']}")
    if doc.get("fetched_at"):
        rows.append(f"> - 抓取时间：{doc['fetched_at']}")
    if doc.get("source_sha256"):
        rows.append(f"> - SHA-256：`{doc['source_sha256'][:16]}`")
    rows.append(f"> - 来源级别：{doc['tier']} {tier['label']}（只说明来源，不等于每句都已核实）")
    rows.append(f"> - 状态：{status}" + (f"；相关事项：{'、'.join(issue_links)}" if issue_links else ""))
    rows.append("> - 本文件只删除不改写；删除了什么见《内容清理记录》。")
    if doc.get("decision") == "D":
        who = "内部判断保留" if doc.get("d_by") == "internal" else "客户同意保留"
        rows.append(f"> - 初筛 D 类，{who}。" + ("⚠ D 类，用前看原文：初筛没标出哪里不能用，只自动删了价格、客户名、联系方式和付款条款；"
                                                "有人看过没问题后写进 review-decisions.json 的 approve_docs，才算有效。"
                                                if doc.get("d_unapproved") else "已有人看过并批准。"))
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
        elif flag["type"] == "screening_hint":
            notes.append(f"⚠ 初筛建议删除（{'、'.join(flag['terms'])}），整理时看是产品内容、规则也没发现价格或客户信息，保留；用前核对")
        elif flag["type"] == "brand_hidden":
            # The word itself may be the customer's name, so it is not repeated here.
            notes.append("⚠ 隐去了含品牌词的格（可能是客户品牌，词在待判断清单里）")
        elif flag["type"] == "reference_draft":
            continue  # Said once on the card; repeating it on every paragraph buries the text.
        elif flag["type"] == "ai_added":
            notes.append(f"⚠ AI 看图补充：提取的文字里没有、图上有的内容，待对照原图核对（{anm_ids.get('ai_read', '')}）")
        elif flag["type"] == "ai_read":
            notes.append(f"⚠ AI 读图转写，待对照原图核对（{anm}）")
    return "；".join(notes)


def as_table(text: str, header: bool = True) -> str:
    """Rows written as "a | b | c" become a Markdown table; the words stay as they are."""
    rows = [[cell.strip() for cell in line.split(" | ")] for line in text.splitlines() if line.strip()]
    width = max((len(row) for row in rows), default=1)
    lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
    if header and lines:
        lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)


def render_doc(doc: dict[str, Any], blocks: list[dict[str, Any]], status: str, card: list[str],
               anm_ids: dict[str, str], cus_ids: dict[str, str], pictures: dict[str, str] | None = None,
               page_notes: dict[str, list[str]] | None = None) -> str:
    """pictures: page heading -> picture of that page, for pages nothing was removed from.
    page_notes: page heading -> what the page check found there."""
    pictures, page_notes = pictures or {}, page_notes or {}
    tier = TIERS[doc["tier"]]
    fields = {
        "doc_id": doc["doc_id"], "title": doc["title"], "layer": "来源层", "tier": doc["tier"], "tier_label": tier["label"],
        "visibility": tier["visibility"] if status == "有效" else "internal", "uses": tier["uses"] if status == "有效" else [],
        "status": status, "module": doc["module"], "source_item_id": doc["item_id"], "source_sha256": doc["source_sha256"],
        "flags": sum(1 for b in blocks if b.get("flags")), "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}",
    }
    parts = [frontmatter(fields), "", f"# {doc['title']}", "", *card]
    current, table_columns, show_images, pending_marked, previous = None, [], False, set(), ""
    row_notes: dict[str, list[str]] = {}

    def flush_row_notes() -> None:
        # A table cell has no room for a sentence: what the ⚠ in a row means is said under the table.
        if row_notes:
            parts.append("")
            parts.extend(f"> 第 {'、'.join(numbers)} 行：{text}" for text, numbers in row_notes.items())
            row_notes.clear()

    for block in blocks:
        if block["group"] != current:
            flush_row_notes()
            current = block["group"]
            parts.extend(["", f"## {current}", ""])
            if current in pictures:
                parts.extend([f"![[{shown(pictures[current])}|{PAGE_PICTURE_WIDTH}]]", ""])
            for note in page_notes.get(current, []) if not any(b.get('transcription_review') for b in blocks if b['group'] == current) else []:
                parts.extend([f"> {line}" for line in note.splitlines()] + [""])
            if any(b.get('transcription_review') for b in blocks if b['group'] == current):
                parts.extend(['> 已由 Agent 对照本页原图核对提取稿；原始提取及校正依据保留在构建审计记录。', ''])
                for note in dict.fromkeys(b.get('transcription_review_note') for b in blocks
                                         if b['group'] == current and b.get('transcription_review_note')):
                    parts.extend([f'> {literal_dimensions(line)}' for line in note.splitlines()] + [''])
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
            if note:
                row_notes.setdefault(note, []).append(block["locator"].rsplit(" ", 2)[-2])
            row += [escape_cell(block["cells"].get(role, "")) for role in table_columns]
            if show_images:
                row.append(" ".join(f"![[{shown(name)}|80]]" for name in block.get("images", [])[:MAX_STYLE_IMAGES] if name in PICTURE_FILES))
            parts.append("| " + " | ".join(row) + " |")
        elif block["kind"] == "point":
            parts.append(f"- [{block.get('scope', 'single_communication')}] {block['text']}" + (f" {note}" if note else ""))
        else:
            text = block["text"]
            table = bool(TABLE_LOCATOR.search(block["locator"]))
            # The rest of a table a removed row split off joins the rows above it, so it stays one table.
            continued = table and block["locator"] == previous
            if table and not text.lstrip().startswith("|"):
                text = as_table(text, header=not continued)  # Read before tables were kept as tables.
            if note:
                parts.append(f"> {note}")
                parts.append("")
            elif continued and parts and parts[-1] == "":
                parts.pop()
            parts.extend([merged_sheet_table(text, block.get('table_layout', {})), ""])
        previous = block["locator"]
    flush_row_notes()
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


def neutral_title(doc: dict[str, Any], forbidden: re.Pattern[str] | None = None) -> None:
    """A title becomes a file name and a link, so it must pass the same rules as text: a price, or a
    customer's name taken from the original file name (报价单 sent to Acme), gives way to a number."""
    if any(pattern.search(doc["title"]) for pattern in HARD_PATTERNS.values()) or (forbidden and forbidden.search(doc["title"])):
        prefix = doc["title"].split("-", 1)[0] if "-" in doc["title"] else "资料"
        doc["title"] = f"{prefix}-{doc['doc_id'][-6:]}"
        doc["file_name"] = safe_name(doc["title"]) + ".md"


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
        named = [name for name in entry["images"] if name in PICTURE_FILES]
        picture = f"![[{shown(named[0])}|80]]" if named else ""
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
            media = Counter(r["reason"] for r in members.values() if r["route"] == "media")
            if media.get("media_kept"):
                entered_count += f"，另有素材 {media['media_kept']} 个"
            if media.get("media_awaiting_client"):
                state = "、".join(p for p in (state, f"素材待客户确认 {media['media_awaiting_client']}") if p)
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
                   statuses: dict[str, str], status_info: dict[str, Any], issues: dict[str, list[str]],
                   counts: dict[str, Counter] | None = None, originals: dict[str, str] | None = None) -> None:
    """One row per document that entered; excluded material is only counted, never listed by name."""
    counts = counts or {"kept": Counter(), "removed": Counter()}
    columns = ["资料ID", "知识库名字", "来源类型", "原始位置", "格式", "SHA-256", "可检索状态", "页数", "待识别页",
               "规则删掉", "处理去向", "来源级别", "关联事项"]
    lines = ["# 源资料与可检索性台账", "", f"- 净化原文目录：`{SOURCE_ROOT}`", "- 机器索引：`02_源资料/knowledge-index.sqlite`",
             "- 说明：原文件留在初筛包或客户官网；这里登记每份资料的身份、能否检索、提取到什么程度和去向。",
             f"- 原文件名：知识库里只用改过的名字。原名和在初筛包里的位置见初筛包的 `{NAME_MAP_PAGE}`，按“知识库名字”一列查。", "",
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
        place = origin if origin.startswith("http") else (f"[打开原件]({(originals or {})[doc_id]})" if doc_id in (originals or {})
                                                          else f"初筛包 `{origin}`" if doc.get("origin") == "points" and origin
                                                          else f"初筛包 `{doc.get('package_name') or ''}`（见对照表）")
        file_format = "jsonl" if doc.get("origin") == "points" else Path(doc.get("original_name") or "").suffix.lstrip(".")
        if not file_format and origin.startswith("http"):
            file_format = "网页"
        row = [doc["doc_id"], f"[[{Path(doc['file_name']).stem}]]", doc.get("source_label", ""), place,
               file_format, doc["source_sha256"][:12], state, str(info.get("pages") or ""), pages, share,
               statuses.get(doc_id, ""), doc["tier"], "、".join(issues.get(doc["item_id"], []))]
        lines.append("| " + " | ".join(escape_cell(c) for c in row) + " |")
    held = [(item, r) for item, r in routes.items() if r["route"] == "hold"]
    lines.extend(["", "## 暂缓的资料", "", "原名见初筛包的对照表，按资料编号查。", "", "| 资料编号 | 类别 | 原因 |", "| --- | --- | --- |"])
    lines.extend(f"| `{item}` | {r.get('decision') or ''} | {reason_label(r['reason'])} |" for item, r in held)
    skipped = Counter(reason_label(r["reason"]) for r in routes.values() if r["route"] == "skip")
    lines.extend(["", "## 不入库的资料（只计数）", "", "| 原因 | 数量 |", "| --- | ---: |"])
    lines.extend(f"| {reason} | {count} |" for reason, count in skipped.most_common())
    write_text(build / "vault" / REGISTER, "\n".join(lines) + "\n")


def check_group(result: dict[str, Any]) -> str:
    """The heading a page-check result belongs under, the same one extract_blocks gives the page."""
    unit, number = result["unit"], result["page"]
    return {"slide": f"第 {number} 张幻灯片", "image": f"图片 {number}"}.get(unit, f"第 {number} 页")


def page_check_notes(build: Path, gov: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """item -> page heading -> notes from looking at the page next to its extracted text."""
    path = build / "page-check" / "results.json"
    notes: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for result in (load_json(path).values() if path.is_file() else []):
        page = notes[result["item_id"]][check_group(result)]
        if result.get("text_errors"):
            page.append(f"⚠ 看图核对：提取的文字和原图不一致，以原图为准（{result['text_errors']}）{gov['mat_ids'].get('text_errors', '')}")
        if result.get("unclear"):
            # Say what is unclear, so a reader knows which products the code may not cover.
            detail = f"{result['pairing']}。" if result.get("pairing") else ""
            if "不作分配" in detail or "归属不清" in detail:
                page.append(f"⚠ 看图核对：{detail}款号位置已确认，但特征归属不清；保留原图共用文字，不自行分配（{gov['cus_ids'].get('pairing', '')}）")
            else:
                page.append(f"⚠ 看图核对：{detail}原件上看不出款号和产品的对应，不要自行配对（{gov['cus_ids'].get('pairing', '')}）")
        elif result.get("pairing"):
            page.append(f"看图核对：{result['pairing']}")
        if result.get("structured"):
            # The page's own words in the page's own order: a table as a table, a flow in its steps.
            order = {"流程图": "按图上箭头的顺序", "时间轴": "按时间先后"}.get(result["structure"], "按图上的排法")
            page.append(f"⚠ 看图整理（{result['structure']}，{order}；AI 整理，只用了图上的字，待核对）：\n\n{result['structured']}")
    return notes


def write_plan_page(build: Path, routes: dict[str, Any], docs: dict[str, dict[str, Any]], published: dict[str, str],
                    statuses: dict[str, str], originals: dict[str, str], config: dict[str, Any], status_info: dict[str, Any]) -> list[str]:
    """02_源资料/处理计划与样例: every kind of material, what is done to it, one example, and whether
    a person has confirmed the example. Returns the kinds still waiting for confirmation."""
    entered = [docs[d] for d in published]
    decisions = load_decisions(build)
    samples = choose_samples(entered, config, build)
    gate = load_gate(build).get("kinds", {})
    check_dir = build / "page-check"
    todo = load_jsonl(check_dir / "todo.jsonl")
    done = load_json(check_dir / "results.json") if (check_dir / "results.json").is_file() else {}
    deferred = (load_json(check_dir / "plan.json") if (check_dir / "plan.json").is_file() else {}).get("deferred", {})
    by_item = {d["item_id"]: d for d in entered}
    checks: Counter = Counter()
    for entry in list(todo) + list(done.values()):
        if entry["item_id"] in by_item:
            checks[doc_type(by_item[entry["item_id"]])] += 1
    maps = sorted({(Path(r["package"]) / NAME_MAP_PAGE).resolve().as_uri() for r in routes.values() if r.get("package")})
    lines = ["# 处理计划与样例", "", MARKER_PLAN,
             "每类文件先只提取一份样例给人看，确认后再批量提取同类。还没确认的类，这次只提取了样例那一份，其余的在“要做的事”里写着“确认后提取”。", "",
             "- 怎么确认：在下表“确认状态”一列写“已确认”，或者写“要改：……”说明怎么改；也可以直接告诉 AI。下次建库会读回来，不会被覆盖。",
             "- 怎么选处理方式（PDF、PPT）：先看“样例情况”，再在“处理方式”一列写下面三种之一，和确认一起生效：",
             *[f"  - **{name}**：{METHOD_HELP[key]}" for key, name in METHODS.items()],
             "  - 想先看 MinerU 读样例的效果再选，跟 AI 说“样例试读 MinerU”（要批准上传，只传样例）。",
             "- 原文件：点“打开原件”直接打开初筛包里的原文件。原名对照：" + "、".join(f"[打开对照表]({uri})" for uri in maps), ""]
    lines.extend(["## 按资料类型", "", "| 类型 | 份数 | 在初筛包哪里 | 怎么提取 | 样例 | 样例情况 | 要做的事 | 处理方式 | 确认状态 |",
                  "| --- | ---: | --- | --- | --- | --- | --- | --- | --- |"])
    waiting: list[str] = []
    for kind, (label, how) in TYPES.items():
        members = [d for d in entered if doc_type(d) == kind]
        info = gate.get(kind, {})
        if not members and not info:
            continue
        folders = Counter(str(Path(d.get("package_path") or "").parent.as_posix()) if not str(d.get("package_path", "")).startswith("http")
                          else "官网" for d in members)
        if info.get("folders"):
            folders = Counter(info["folders"])  # Every file of the kind, read or waiting.
        where = "、".join(f"{f}（{n} 份）" for f, n in folders.most_common(3)) + (" 等" if len(folders) > 3 else "")
        todo_text = []
        if info.get("waiting"):
            todo_text.append(f"确认后提取其余 {info['waiting']} 份")
        # What reading the files found they are: quotes, product lists, other people's compilations.
        roles = Counter("报价单" if d.get("kind") == "quotation" else "产品表" if d.get("tier") == "T3" else
                        "参考整理稿（不当公司事实）" if d.get("role") == "reference" else "" for d in members)
        found_roles = "；".join(f"其中 {n} 份是{r}" for r, n in roles.items() if r)
        if checks[kind] or deferred.get(kind):
            todo_text.append(f"对照原图核对 {checks[kind]} 页" + (f"，确认后再核对其余 {deferred[kind]} 页" if deferred.get(kind) else ""))
        ocr = sum(len(remaining_ocr_pages(build, d["item_id"], status_info.get(d["item_id"], {}).get("ocr_pages") or [])) for d in members)
        if ocr:
            todo_text.append(f"{ocr} 页图片要 MinerU 或 AI 读图")
        sample = docs.get(samples.get(kind, ""))
        sample_text = ""
        if sample:
            stem = Path(sample["file_name"]).stem
            sample_text = f"[[{stem}]]" + (f" · [打开原件]({originals[sample['doc_id']]})" if sample["doc_id"] in originals else "")
        status = status_of(kind, decisions)
        if not is_confirmed(kind, decisions, config):
            waiting.append(label)
        count = info.get("total", len(members))
        how_text = how + (f"。{found_roles}" if found_roles else "")
        seen = sample_findings(build, sample, done, status_info) if sample and kind in PAGED_KINDS else "—"
        chosen = (decisions.get(kind) or {}).get("method")
        method = (METHODS[method_of(kind, decisions)] + ("" if chosen else "（默认）")) if kind in PAGED_KINDS else "—"
        lines.append(f"| {label} | {count} | {escape_cell(where)} | {how_text} | {sample_text} | {escape_cell(seen)} "
                     f"| {'；'.join(todo_text) or '—'} | {method} | {status} |")
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in done.values():
        if result.get("structure") and result["item_id"] in by_item:
            found[result["structure"]].append(result)
    if found:
        lines.extend(["", "## 特殊结构", "", "看图核对时认出的表格、流程图、时间轴、地图、图表。每种先看样例，确认整理方式后再统一处理。", "",
                      "| 结构 | 出现几处 | 出现在 | 样例 | 确认状态 |", "| --- | ---: | --- | --- | --- |"])
        for name, results in sorted(found.items()):
            links = [f"[[{Path(by_item[r['item_id']]['file_name']).stem}#{check_group(r)}|{by_item[r['item_id']]['title']} · {check_group(r)}]]"
                     for r in results]
            key = f"{STRUCTURE_PREFIX}{name}"
            if not is_confirmed(key, decisions, config):
                waiting.append(f"结构：{name}")
            lines.append(f"| {name} | {len(results)} | {'、'.join(links[:3])}{' 等' if len(links) > 3 else ''} | {links[0]} | {status_of(key, decisions)} |")
    held = Counter((r.get("decision") or "", reason_label(r["reason"])) for r in routes.values()
                   if r["route"] in {"hold", "skip"} and r["reason"] != "awaiting_sample")  # Those are in the rows above.
    lines.extend(["", "## 不处理的资料", "", "| 初筛类别 | 原因 | 份数 |", "| --- | --- | ---: |"])
    lines.extend(f"| {DECISION_LABELS.get(code, code)} | {reason} | {count} |" for (code, reason), count in sorted(held.items()))
    write_text(build / "vault" / PLAN_PAGE, "\n".join(lines) + "\n")
    return waiting


def sample_findings(build: Path, sample: dict[str, Any], done: dict[str, Any], status_info: dict[str, Any]) -> str:
    """What the sample showed about its kind, in numbers, and the way of reading it that suggests."""
    item = sample["item_id"]
    pages = int(sample.get("pages") or 0)
    unread = remaining_ocr_pages(build, item, status_info.get(item, {}).get("ocr_pages") or [])
    looked = [r for r in done.values() if r["item_id"] == item]
    added = sum(1 for r in looked if r.get("added"))
    wrong = sum(1 for r in looked if r.get("text_errors"))
    unclear = sum(1 for r in looked if r.get("unclear"))
    parts = [f"共 {pages} 页" if pages else ""]
    if unread:
        parts.append(f"{len(unread)} 页没有文字层")
    if looked:
        parts.append(f"对照原图看了 {len(looked)} 页：{added} 页图里有字文字层没有，{wrong} 页文字层有错" + (f"，{unclear} 页看不出款号对应" if unclear else ""))
    else:
        parts.append("还没对照原图看（先做看图核对，才看得出文字层够不够）")
    read_path = build / "mineru-units" / f"{item}.json"
    reviewed_path = build / 'reviewed-pages.json'
    reviewed = load_json(reviewed_path).get(item, []) if reviewed_path.is_file() else []
    if (build / 'mineru-drafts' / item / 'provenance.json').is_file() and reviewed:
        parts.append(f"本样例已用 MinerU 初稿 + Agent 对照原页复核，共 {len(reviewed)} 页；原始初稿及核对依据已保存")
    if read_path.is_file() and load_json(read_path).get("source") == "mineru":
        native = defaultdict(str)
        record = load_json(read_path).get("units", [])
        text_path = build / "package-text" / f"{item}.json"
        for unit in load_json(text_path).get("units", []) if text_path.is_file() else []:
            native[re.sub(r" (?:col|table) \d+$", "", unit["locator"])] += squeeze_key(unit["text"])
        fuller = sum(1 for u in record if u["locator"].endswith(" ocr") and u.get("engine") != "ai"
                     and any(squeeze_key(line) and squeeze_key(line) not in native[u["locator"][:-4]] for line in u["text"].splitlines()))
        parts.append(f"MinerU 试读：{fuller} 页读出了文字层没有的内容")
    if not looked and not unread:
        advice = ""
    elif unread or (looked and (added + wrong) * 5 >= len(looked)):
        advice = "建议：MinerU 初稿 + AI 复核（不想上传就选 AI 看图复核）"
    else:
        advice = "建议：文字层够用"
    return "；".join(p for p in parts if p) + (f"。{advice}" if advice else "")


def make_aliases(routes: dict[str, Any], docs: dict[str, dict[str, Any]], order: list[str]) -> dict[str, str]:
    """doc_id -> link that opens the original file, under a name without the customer in it.

    Each original gets a second name in the package, 审计记录/按知识库名字/<knowledge-base name>,
    as a hard link: the same file, no copy, no extra space. A link in the vault can then open
    the original with one click while the vault never prints the original name. Where a hard
    link cannot be made (another drive, a network share), the document has no link and the
    name map is the way."""
    links: dict[str, str] = {}
    for doc_id in order:
        doc = docs[doc_id]
        link = alias_link(routes.get(doc["item_id"], {}), Path(doc["file_name"]).stem)
        if link:
            links[doc_id] = link
    return links


def alias_link(route: dict[str, Any], stem: str) -> str:
    """The original's second name, <stem><its suffix>, in the package's alias folder; "" when none can be made."""
    if not route.get("package") or not route.get("path"):
        return ""
    source = Path(route["package"]) / route["path"]
    alias = Path(route["package"]) / ALIAS_FOLDER / f"{stem}{source.suffix}"
    try:
        if alias.exists() and not alias.samefile(source):
            alias.unlink()  # The name now belongs to another file, or the original was replaced.
        if not alias.exists():
            alias.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, alias)
        return alias.resolve().as_uri()
    except OSError:
        return ""


def twin_links(routes: dict[str, Any], docs: dict[str, dict[str, Any]], order: list[str]) -> dict[str, str]:
    """doc_id -> link to the PDF of the same document ("" when the PDF has no alias), for pages kept from Word or PowerPoint."""
    page_of = {docs[d]["item_id"]: d for d in order}
    twins: dict[str, str] = {}
    for route in routes.values():
        doc_id = page_of.get(route.get("same_as") or "")
        if route.get("reason") == "other_format" and doc_id:
            # The PDF's alias takes the page's name, so the two originals sort side by side.
            twins[doc_id] = alias_link(route, Path(docs[doc_id]["file_name"]).stem)
    return twins


def write_name_maps(routes: dict[str, Any], docs: dict[str, dict[str, Any]], published: dict[str, str],
                    statuses: dict[str, str], same_content: dict[str, str]) -> None:
    """Write, into each package's audit folder, which knowledge-base name each file got.

    The vault only uses the new names, because original names often name a customer.
    This map is the one place that pairs them, kept with the originals it points to."""
    by_item = {d["item_id"]: d for d in docs.values() if d["doc_id"] in published}
    points = next((d for d in docs.values() if d.get("origin") == "points" and d["doc_id"] in published), None)
    per_package: dict[str, list[list[str]]] = defaultdict(list)
    excluded: Counter = Counter()
    for item_id, route in sorted(routes.items(), key=lambda kv: (kv[1].get("decision") or "", kv[1].get("path") or "")):
        package = route.get("package")
        if not package:
            continue  # Website pages have no file in a package.
        if route.get("decision") == "E":
            excluded[package] += 1
            continue
        doc = by_item.get(item_id) or (by_item.get(same_content[item_id]) if item_id in same_content else None)
        if route["route"] == "points" and points:
            doc, fate = points, "汇总进业务要点"
        elif route.get("fate"):
            fate = route["fate"]
        elif item_id in same_content and doc:
            fate = "内容和另一份相同，只发布了一份"
        elif route.get("reason") == "other_format" and by_item.get(route.get("same_as") or ""):
            doc = by_item[route["same_as"]]
            fate = f"同一份资料的另一种格式，已用 {Path(routes[route['same_as']].get('path') or '').suffix or '原格式'} 版入库"
        elif doc:
            fate = f"已入库，{statuses[doc['doc_id']]}"
        else:
            fate = f"{'暂缓' if route['route'] == 'hold' else '不入库'}：{reason_label(route['reason'])}"
        per_package[package].append([
            Path(doc["file_name"]).stem if doc else "", f"{SOURCE_ROOT}/{published[doc['doc_id']]}" if doc else "",
            route.get("path") or "", route.get("decision") or "", fate, item_id])
    columns = ["知识库名字", "知识库位置", "初筛包里的位置（原名）", "初筛类别", "去向", "资料编号"]
    for package, rows in per_package.items():
        root = Path(package)
        if not (root / "审计记录").is_dir():
            continue
        lines = ["# 知识库文件对照", "",
                 "知识库（Obsidian）里只用改过的名字，比如“产品画册-2027TAPESTRY产品系列”“报价单-0a663a2b”，因为原文件名里常有客户的买家名。"
                 "这张表把两边对上：按“知识库名字”找到初筛包里的原文件，或者按原文件名找到它在知识库里叫什么。", "",
                 "- 由 compile-enterprise-knowledge 每次建库时自动更新，不要手改；改了下次会被覆盖。",
                 "- 重建、更新知识库，或者给客户看原文件时，先读这张表。名字两边不一样是正常的，不算错误。",
                 "- 这张表只放在初筛包里，不放进知识库。",
                 f"- E 类（排除）的 {excluded[package]} 份不进知识库，没有列出。" if excluded[package] else "", "",
                 "| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
        lines.extend("| " + " | ".join(escape_cell(cell) for cell in row) + " |" for row in rows)
        write_text(root / NAME_MAP_PAGE, "\n".join(line for line in lines) + "\n")
        with (root / NAME_MAP_CSV).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)


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
                            + ["是" if entry["conflict"] else "", len(entry["sources"]), " ".join(PICTURE_FILES[i] for i in entry["images"] if i in PICTURE_FILES)])


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
    decisions_path = build / "review-decisions.json"
    waiting_d = set(unapproved_d(routes, load_json(decisions_path) if decisions_path.is_file() else {}))
    for doc in docs.values():
        doc["d_unapproved"] = doc["item_id"] in waiting_d
        doc["d_by"] = "internal" if routes.get(doc["item_id"], {}).get("reason") == "kept_internally" else "client"
    blocks = load_jsonl(build / "blocks.jsonl")
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        if block["status"] in VISIBLE:
            by_doc[block["doc_id"]].append(block)

    # The names the guard learned (customers, denied words): no title, so no file name, may carry one.
    guard_terms = load_json(build / "guard-terms.json") if (build / "guard-terms.json").is_file() else {}
    title_terms = compile_terms(guard_terms.get("hard", []) + guard_terms.get("soft", []))
    # Saved-again copies differ in bytes but say the same thing; publish one.
    same_content, fingerprints, names_seen = {}, {}, Counter()
    order = []
    for doc_id, doc in docs.items():
        members = by_doc.get(doc_id, [])
        if not members:
            continue
        neutral_title(doc, title_terms)
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
    published_ids = set(order)
    for doc_id, doc in docs.items():
        # Ledgers then name it instead of linking a page that is not there.
        doc["unpublished"] = "" if doc_id in published_ids else (
            "和另一份内容相同，只发布了一份" if doc["item_id"] in same_content else "全部内容按规则删除，没有入库")
    styles = product_master([b for b in visible if b["status"] != "pending_client"], docs)
    status_path = build / "package-text" / "extract-status.json"
    status_info = load_json(status_path)["items"] if status_path.is_file() else {}
    # Ledgers name a file by its knowledge-base name, or by its number when it has none;
    # the original name, often a customer's, stays in the package's name map.
    by_name = {}
    for item_id, route in routes.items():
        doc = next((docs[d] for d in order if docs[d]["item_id"] == item_id), None)
        by_name.setdefault(route.get("display_name") or "", doc["title"] if doc else f"资料编号 {item_id}")
    gov = build_governance(config.get("project_id", "P-000"), build, routes, docs, blocks, styles,
                           load_json(build / "sheet-report.json"), load_jsonl(build / "review-queue.jsonl"),
                           status_info, (passed or args.skip_t3_gate, reason), progress_of(run), effective,
                           show=lambda name: by_name.get(name, "（原名见初筛包对照表）"))
    cleanup_record(build, blocks, docs)
    issues: dict[str, list[str]] = defaultdict(list)
    for item_key, record_id in gov["cus_items"].items():
        if record_id not in issues[item_key.split(":")[0]]:
            issues[item_key.split(":")[0]].append(record_id)
    checks = page_check_notes(build, gov)
    published, statuses = {}, {}
    # A page picture shows the whole page, so only pages nothing was removed from or held on get one.
    unsafe = {(b["doc_id"], b["group"]) for b in blocks if b["status"] in {"removed", "pending_client"}}
    originals = make_aliases(routes, docs, order)
    twins = twin_links(routes, docs, order)
    # Pictures are named before pages are written, so every page links the same name.
    terms = load_json(build / "guard-terms.json") if (build / "guard-terms.json").is_file() else {}
    forbidden = compile_terms(terms.get("hard", []) + terms.get("soft", []))
    picture_status = {doc_id: doc_status(docs[doc_id], by_doc[doc_id], effective) for doc_id in order}
    held = {(b["doc_id"], b["locator"]) for b in blocks if b["status"] in {"removed", "pending_client"}}
    # Which product or solution each picture and video belongs to, by its folder names.
    media = load_media(build)
    products = product_catalog(config, media, {d: docs[d] for d in order}, forbidden)
    match_product = Matcher(products)
    places = place_media(media, match_product)
    pictures_named = build_pictures(build, config, docs, by_doc, order, picture_status, unsafe, status_info, forbidden,
                                    MAX_STYLE_IMAGES, held, places)
    PICTURE_FILES.clear()
    PICTURE_FILES.update({picture_id: Path(entry["file"]).name for picture_id, entry in pictures_named.items()})
    for entry in load_media(build):
        # Material kept by folder has no page; the name map says where it went instead.
        route = routes.get(entry["item_id"])
        if not route:
            continue
        if entry.get("drawing") and entry.get("id") in pictures_named:
            route["fate"] = f"图纸，按素材处理，每页一张图：90_素材/{pictures_named[entry['id']]['file']} 等"
        elif entry.get("id") in pictures_named:
            route["fate"] = f"进素材库：90_素材/{pictures_named[entry['id']]['file']}"
        elif entry["status"] == "media_awaiting_client":
            route["fate"] = "整个文件夹待客户确认，回复前不进素材库"
        elif entry["status"] == "drawing_unrendered":
            route["fate"] = "图纸，按素材处理；没能转成图片，原件留在初筛包"
        elif entry["kind"] == "image" and Path(entry.get("relpath") or "").suffix.casefold() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            suffix = Path(entry.get("relpath") or "").suffix or "这种格式"
            route["fate"] = f"{suffix} 设计稿或特殊格式图片，知识库里显示不了，原件留在初筛包，素材索引里有文件夹链接"
        elif entry["kind"] == "image":
            route["fate"] = "图片较大，原图留在初筛包，素材索引里有文件夹链接"
        else:
            route["fate"] = "视频或音频，原件留在初筛包，内容待转写"
    for doc_id in order:
        doc, members = docs[doc_id], by_doc[doc_id]
        status = doc_status(doc, members, effective)
        links = sorted(set(issues.get(doc["item_id"], [])) | {gov["anm_ids"][f["type"]] for b in members for f in b.get("flags", []) if f["type"] in gov["anm_ids"]})
        card = source_card(doc, status, links, status_info.get(doc["item_id"], {}).get("layout", ""), originals.get(doc_id, ""),
                           twins.get(doc_id))
        info = status_info.get(doc["item_id"], {})
        pictures = {page_group(info, page): name for page, name in info.get("page_images", {}).items()
                    if (doc_id, page_group(info, page)) not in unsafe and name in PICTURE_FILES}
        write_text(clean / doc_path(doc), render_doc(doc, members, status, card, gov["anm_ids"], gov["cus_items"], pictures,
                                                     checks.get(doc["item_id"], {})))
        published[doc_id], statuses[doc_id] = doc_path(doc), status

    write_table_summary(clean, visible, docs, statuses)
    # The site view also helps while only part is extracted: it shows what is there and what still waits.
    write_site(build, config, routes, docs, order, statuses, originals, media, pictures_named, forbidden, escape_cell)
    counts = {"kept": Counter(b["doc_id"] for b in visible), "removed": Counter(b["doc_id"] for b in blocks if b["status"] == "removed")}
    write_register(build, routes, docs, published, statuses, status_info, issues, counts, originals)
    write_name_maps(routes, docs, published, statuses, same_content)
    write_json(build / "samples-waiting.json", write_plan_page(build, routes, docs, published, statuses, originals, config, status_info))
    write_picture_index(build, pictures_named, docs, escape_cell)
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
