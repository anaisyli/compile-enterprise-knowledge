#!/usr/bin/env python3
"""Turn everything that needs a decision into manage-article-knowledge style ledgers.

Risky content is published with a mark rather than held back, so these ledgers are where
a person sees it. Each problem lives in one place: CUS holds every question for the client,
MAT holds material that needs processing, ANM holds risks the team judges itself. Items
with the same cause, owner, and closing condition are one entry. IDs are stable across
reruns, and what a person writes into a ledger survives the next build.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

from kb_common import load_json, normalize_space, remaining_ocr_pages, write_json, write_text


LEDGERS = {
    "CUS": ("05_数据与审核/30_异常与待决定/10_待客户补充/01_待客户补充事项.md", "待客户补充事项",
            "所有要客户回答的问题；客户回复核验前不得写成已确认。"),
    "MAT": ("05_数据与审核/30_异常与待决定/20_源资料处理/01_源资料处理台账.md", "源资料处理台账",
            "资料要处理的事项：要识别的图片、读不出的文件、表格问题、待判断的产品表。"),
    "ANM": ("05_数据与审核/30_异常与待决定/30_源文与事实异常/01_源文与事实异常台账.md", "源文与事实异常台账",
            "内部要判断的风险：内容已入库并标 ⚠，不静默改成正式事实。"),
}
COLUMNS = {
    "CUS": ["CUS ID", "要问客户什么", "涉及", "当前阶段", "下一责任人", "最近更新"],
    "MAT": ["MAT ID", "事项", "涉及", "当前阶段", "下一责任人", "最近更新"],
    "ANM": ["ANM ID", "发生了什么", "涉及", "当前阶段", "下一责任人", "最近更新"],
}
# A person may change these; the next build keeps what they wrote.
HUMAN_COLUMNS = ("当前阶段", "下一责任人")
RECORD_HEADING = "#### 处理记录"
SOLVED_STAGE = "系统已解决，待确认"
CLOSED_STAGES = {"已完成", "已关闭", "已解决", "无需询问", SOLVED_STAGE}
FOUNDED = re.compile(r"(?i)(?:成立于|始建于|创立于|创建于|建厂于|established(?:\s+in)?|founded(?:\s+in)?)\s*[:：]?\s*((?:19|20)\d{2})"
                     r"|((?:19|20)\d{2})\s*年\s*(?:成立|创立|创建|建厂)")
STAFF = re.compile(r"(?i)(?:员工|工人|employees|workers)\s*[:：]?\s*(?:约|超过|逾|over|about|more than)?\s*(\d{2,5})"
                   r"|(\d{2,5})\s*(?:\+\s*)?(?:名员工|名工人|余名员工|多名员工|employees|workers)")
MAX_CELL = 80
MAX_SNIPPET = 40
HEAVY_SHARE = 0.2
HEAVY_MIN = 3
STYLE_LIKE = re.compile(r"^[\w#./-]+(?:\s[\w#./-]+){0,2}$")


class Registry:
    """Stable numbering: the same problem keeps its ID from run to run."""

    def __init__(self, path: Path, project_id: str) -> None:
        self.path = path
        self.project = project_id
        self.ids: dict[str, dict[str, int]] = load_json(path) if path.is_file() else {}

    def number(self, table: str, key: str) -> int:
        entries = self.ids.setdefault(table, {})
        if key not in entries:
            entries[key] = max(entries.values(), default=0) + 1
        return entries[key]

    def id_for(self, kind: str, key: str) -> str:
        return f"{self.project}-{kind}-{self.number(kind, key):03d}"

    def save(self) -> None:
        write_json(self.path, self.ids)


def link(doc: dict[str, Any], group: str | None = None) -> str:
    stem = Path(doc["file_name"]).stem
    return f"[[{stem}#{group}|{doc['title']} · {group}]]" if group else f"[[{stem}]]"


def short(text: str, limit: int = MAX_CELL) -> str:
    text = normalize_space(text).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def company_facts(blocks: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    """Each value with a short quote and its place, so the client sees why we ask."""
    this_year = date.today().year
    found: dict[str, dict[str, list[str]]] = {"成立年份": defaultdict(list), "员工人数": defaultdict(list)}
    for block in blocks:
        doc = docs.get(block["doc_id"])
        if not doc or doc["tier"] not in {"T1", "T2"} or block["status"] not in {"kept", "flagged", "pending_client"}:
            continue
        for label, pattern in (("成立年份", FOUNDED), ("员工人数", STAFF)):
            for match in pattern.finditer(block["text"]):
                value = next(group for group in match.groups() if group)
                if label == "成立年份" and int(value) >= this_year:
                    continue  # "SINCE 2026 NEW ARRIVALS" is a collection, not a founding year.
                start = max(0, match.start() - 12)
                quote = short(block["text"][start: match.end() + 12], MAX_SNIPPET)
                found[label][value].append(f"“{quote}”（{link(doc, block['group'])}）")
    return found


def build(
    project_id: str,
    build_dir: Path,
    routes: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    blocks: list[dict[str, Any]],
    styles: list[dict[str, Any]],
    sheet_report: dict[str, Any],
    queue: list[dict[str, Any]],
    extract_status: dict[str, Any],
    t3_state: tuple[bool, str],
    cell_progress: dict[str, int],
    effective_tiers: set[str] | None = None,
    show: Callable[[str], str] = lambda name: name,
) -> dict[str, Any]:
    """show turns an original file name into what the vault may print: customer names hidden."""
    effective_tiers = effective_tiers or {"T1", "T2"}
    registry = Registry(build_dir / "governance-ids.json", project_id)
    today = date.today().isoformat()
    records: dict[str, list[dict[str, Any]]] = {"CUS": [], "MAT": [], "ANM": []}

    def add(kind: str, key: str, title: str, count: str, stage: str, owner: str, details: list[str],
            items: list[dict[str, str]] | None = None) -> str:
        record_id = registry.id_for(kind, key)
        for item in items or []:
            item["sub_id"] = f"{record_id}.{registry.number(f'sub:{key}', item['key'])}"
        records[kind].append({"id": record_id, "key": key, "row": [title, count, stage, owner], "details": details,
                              "items": items or [], "updated": today})
        return record_id

    by_item = {doc["item_id"]: doc for doc in docs.values()}

    # ---- CUS: everything the client has to answer, grouped by cause
    pending: dict[tuple[str, str], int] = defaultdict(int)
    for block in blocks:
        if block["status"] == "pending_client":
            pending[(block["doc_id"], block["client_hold"])] += 1
    cases = sorted({doc_id for (doc_id, hold) in pending if hold == "官网客户案例页"})
    if cases:
        items = [{"key": docs[d]["item_id"], "item_id": docs[d]["item_id"], "location": "",
                  "question": f"官网页面《{docs[d]['title']}》是客户案例，能否进知识库？"} for d in cases]
        add("CUS", "website_cases", f"{len(cases)} 个官网客户案例页能否进知识库", f"{len(cases)} 页", "等待客户回复",
            "内容运营 → 客户",
            ["- 为什么问：页面讲的是具体客户的合作，公开使用要客户同意。",
             "- 回复前：页面已入来源层并标 🕒，整理页不引用。", "", "| 页面 |", "| --- |"]
            + [f"| {link(docs[d])} |" for d in cases], items)
    by_doc: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (doc_id, hold), count in sorted(pending.items()):
        if hold != "官网客户案例页":
            by_doc[doc_id].append((hold, count))
    for doc_id, holds in sorted(by_doc.items()):
        doc = docs[doc_id]
        whole = len(holds) == 1 and holds[0][0].startswith("整份文件")
        items = [{"key": hold, "item_id": doc["item_id"], "location": "" if hold.startswith("整份文件") else hold,
                  "question": f"《{doc['title']}》{'整份文件' if hold.startswith('整份文件') else hold}能否公开使用？"}
                 for hold, _ in holds]
        title = f"《{doc['title']}》能否进知识库" if whole else f"《{doc['title']}》{len(holds)} 处内容能否公开"
        add("CUS", f"pending:{doc['item_id']}", short(title, 60), f"{sum(c for _, c in holds)} 段", "等待客户回复",
            "内容运营 → 客户",
            ["- 为什么问：初筛时标记为待客户确认。", f"- 回复前：已入来源层并标 🕒，整理页不引用。来源：{link(doc)}", "",
             "| 位置 | 段数 |", "| --- | ---: |"] + [f"| {short(hold)} | {count} |" for hold, count in holds], items)
    images = sorted(i for i, r in routes.items() if r["route"] == "hold" and r["reason"] == "image_needs_review")
    if images:
        items = [{"key": i, "item_id": i, "location": "", "question": f"产品图 {show(routes[i]['display_name'])} 能否公开使用？"} for i in images]
        add("CUS", "images", f"{len(images)} 张产品图能否公开使用", f"{len(images)} 张", "等待客户回复", "内容运营 → 客户",
            ["- 为什么问：图中可能有人物、球星或 IP 形象。", "- 回复前：图片不进知识库。", ""]
            + [f"- {show(routes[i]['display_name'])}" for i in images], items)

    good_styles, odd_styles = [], []
    for entry in styles:
        if entry["conflict"]:
            (good_styles if len(entry["style"]) <= 20 and STYLE_LIKE.match(entry["style"]) else odd_styles).append(entry)
    if good_styles:
        detail = ["- 为什么问：同一个款号在不同报价表里面料写法不同，写进文章前要确认以哪个为准。",
                  "- 回复前：产品主档并列保留，不写成唯一规格。", "", "| 款号 | 写法 A | 写法 B | 其他写法 | 出现在 |", "| --- | --- | --- | --- | --- |"]
        for entry in good_styles:
            values = entry["values"]["fabric"]
            others = f"另有 {len(values) - 2} 种" if len(values) > 2 else ""
            detail.append(f"| {short(entry['style'], 20)} | {short(values[0])} | {short(values[1])} | {others} | "
                          f"{'、'.join(entry['sources'][:3])}{' 等' if len(entry['sources']) > 3 else ''} |")
        add("CUS", "style_fabric", f"{len(good_styles)} 个款号的面料写法不一致，以哪个为准", f"{len(good_styles)} 个款号",
            "准备客户话术", "内容运营", detail,
            [{"key": "all", "item_id": "", "location": "", "question": f"{len(good_styles)} 个款号的面料写法不一致，请按详情表确认以哪个为准"}])
    for label, values in company_facts(blocks, docs).items():
        if len(values) > 1:
            detail = [f"- 为什么问：不同资料里的{label}写法不同，对外写错会影响可信度。", "- 回复前：整理页写“需核实”，并列出处。", "",
                      "| 写法 | 原文和出处 |", "| --- | --- |"]
            detail += [f"| {value} | {'；'.join(sorted(set(places))[:3])} |" for value, places in sorted(values.items())]
            add("CUS", f"company:{label}", f"公司的{label}以哪个为准：{'、'.join(sorted(values))}", f"{len(values)} 种写法",
                "准备客户话术", "内容运营", detail,
                [{"key": "all", "item_id": "", "location": "", "question": f"公司的{label}以哪个为准（{'、'.join(sorted(values))}）"}])

    # ---- ANM: risks inside material that is already in use
    unverified: dict[str, set[str]] = defaultdict(set)
    reports: set[str] = set()
    ai_pages: set[str] = set()
    references: set[str] = set()
    for block in blocks:
        doc = docs.get(block["doc_id"])
        if not doc or doc["tier"] not in effective_tiers or block["status"] == "pending_client":
            continue  # Pending tables are covered by the cell review; pending text by CUS.
        for flag in block.get("flags", []):
            if flag["type"] == "unverified_name":
                for term in flag["terms"]:
                    unverified[term].add(link(doc))
            elif flag["type"] == "report_page":
                reports.add(link(doc, block["group"]))
            elif flag["type"] == "ai_read":
                ai_pages.add(link(doc, block["group"]))
            elif flag["type"] == "reference_draft":
                references.add(link(doc))
    anm_ids: dict[str, str] = {}
    if references:
        anm_ids["reference_draft"] = add(
            "ANM", "reference_draft", f"{len(references)} 份参考整理稿不是公司自己的资料", f"{len(references)} 份", "待判断", "知识库专员",
            ["- 为什么有风险：这些文件自己写着“参考”“内部审核版”“待确认”，内容是从别家公开资料或旧稿整理的，参数、款式可能不是本公司的。",
             "- 现在怎么处理：入库可检索，状态“参考稿”，整理页引用时必须带 ⚠，只能写成“参考资料显示……（待公司确认）”。",
             "- 怎么关闭：请公司确认里面哪些是自己的；确认是公司原始资料的，在 kb-config.json 的 doc_roles 里写 \"original\"。", ""]
            + [f"- {page}" for page in sorted(references)])
    if ai_pages:
        anm_ids["ai_read"] = add("ANM", "ai_read", f"{len(ai_pages)} 页文字是 AI 看图转写的", f"{len(ai_pages)} 页", "待核对",
                                 "知识库专员",
                                 ["- 为什么有风险：图片页没有文字层，MinerU 没有识别，文字由 AI 看图转写，可能看错款号或数字。",
                                  "- 现在怎么处理：已入库并标 ⚠；整理页引用时必须带 ⚠。",
                                  "- 怎么关闭：对照原页核对；有 MinerU 时重新识别，MinerU 的结果会替换 AI 转写。", ""]
                                 + [f"- {page}" for page in sorted(ai_pages)])
    if unverified:
        detail = ["- 为什么有风险：可能是品牌、明星或 IP 名，对外使用可能涉及授权。",
                  "- 现在怎么处理：内容已入库，原文旁标 ⚠；整理页引用时必须带 ⚠。",
                  "- 怎么关闭：普通词写进行业词表或 allow_terms；确认是品牌、IP 的保持标记，并决定能否对外使用。", "",
                  "| 词 | 出现在 |", "| --- | --- |"]
        detail += [f"| {short(term, 30)} | {'、'.join(sorted(pages)[:3])}{' 等' if len(pages) > 3 else ''} |"
                   for term, pages in sorted(unverified.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
        anm_ids["unverified_name"] = add("ANM", "unverified_name", f"{len(unverified)} 个没核实过的词出现在已生效资料里",
                                         f"{len(unverified)} 个词", "待判断", "知识库专员", detail)
    if reports:
        anm_ids["report_page"] = add("ANM", "report_page", f"{len(reports)} 处检测或审核报告", f"{len(reports)} 处", "待判断",
                                     "知识库专员",
                                     ["- 为什么有风险：报告里有评级和其他公司名，也可能已经过期。", "- 现在怎么处理：已入库并标 ⚠，引用前核对报告日期。", ""]
                                     + [f"- {page}" for page in sorted(reports)])

    # ---- MAT: material that needs processing
    def mat(key: str, name: str, members: list[str], unit: str, stage: str, action: str, reopen: str) -> None:
        if members:
            add("MAT", key, name, f"{len(members)} {unit}", stage, action,
                [f"- 下一步：{action}", f"- 重开条件：{reopen}", ""] + [f"- {m}" for m in members])

    ocr = [f"{doc['title']}（第 {'、'.join(map(str, left))} 页尚无可检索文字）" for i, r in routes.items()
           if r["route"] == "kb" and r.get("ocr_pages") and (doc := by_item.get(i))
           and (left := remaining_ocr_pages(build_dir, i, r["ocr_pages"]))]
    unread = [show(r["display_name"]) for r in routes.values() if r["route"] == "hold" and r["reason"] in {"no_extractable_content", "unresolved_item"}]
    mat("ocr", "需要核对或补充文字提取的资料", ocr + unread, "份", "等待材料或工具",
        "核对页面内容；需要补识别时运行 mineru_ocr.py plan，批准后 run", "资料更新或 Token 失效时")
    failed = [f"{show(s['display_name'])}（{s['status']}，资料编号 `{item}`）" for item, s in extract_status.items()
              if s.get("status") in {"hash_mismatch", "missing_file", "failed", "needs_pdf_reader", "needs_libreoffice"}]
    mat("extract", "提取失败的资料", failed, "份", "待知识库专员判断", "按状态补 pypdf、LibreOffice，或换回原文件", "重新提取后仍失败")
    no_header = [f"{show(routes[i]['display_name'])}（{s}）" for i, r in sheet_report.get("sheets", {}).items() for s in r.get("sheets_without_header", [])]
    mat("no_header", "找不到表头的工作表", no_header, "张", "待知识库专员判断", "在 header_map 或模板判断里补列含义", "表格更新时")
    mat("style_column", "款号列疑似放错的产品表", [f"“{short(e['style'], 40)}”（{'、'.join(e['sources'][:2])}）" for e in odd_styles],
        "处", "待知识库专员判断", "在产品表模板判断里改正款号列", "表格更新时")
    # Rules that remove a fifth of a brochure have probably removed something they should not.
    shares: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for block in blocks:
        doc = docs.get(block["doc_id"])
        if doc and doc["tier"] in {"T1", "T2"} and block["kind"] == "text" and block["status"] != "dropped":
            shares[block["doc_id"]][block["status"] == "removed"] += 1
    heavy = [f"{link(docs[d])}：删掉 {gone} / {kept + gone} 段（{gone * 100 // (kept + gone)}%）"
             for d, (kept, gone) in shares.items() if gone >= HEAVY_MIN and gone > (kept + gone) * HEAVY_SHARE]
    mat("heavy_removal", "规则删得多的资料", heavy, "份", "待知识库专员判断",
        "看《内容清理记录》里删掉的原因；删错的词改配置（deny_terms）或在 review-decisions 里放行", "规则或资料更新时")
    english = sorted({d["folder"].split("/", 1)[1] for d in docs.values() if d.get("folder", "").startswith("官网页面/")
                      and d["folder"] != "官网页面/公司与服务" and d["folder"].split("/", 1)[1].isascii()})
    mat("category_names", "官网大类还没有中文名", english, "个", "待知识库专员判断",
        "在 kb-config.json 的 categories 里按网址第一段写中文名，例如 {\"tapestry-jacket\": \"提花夹克\"}，再重跑", "官网改版时")
    browser = [show(r["display_name"]) for r in routes.values() if r.get("reason") == "website_needs_browser"]
    mat("browser", "需要浏览器读取的官网页面", browser, "页", "等待材料或工具", "用浏览器读取后 --browser-capture 导入", "官网改版时")
    fetch_path = build_dir / "website" / "fetch-report.json"
    fetch = load_json(fetch_path) if fetch_path.is_file() else {}
    unread_site = [f"{f['url']}：{short(f['error'], 80)}" for f in fetch.get("failed", [])]
    if fetch and not fetch.get("usable_pages"):
        unread_site.insert(0, f"{'、'.join(fetch.get('sites', []))} 一页可用正文也没读到（{fetch.get('checked_at', '')[:10]}）")
    elif fetch.get("using_snapshot"):
        unread_site.insert(0, f"这次没读到新页面，沿用 {fetch['using_snapshot']} 起保存的官网快照")
    mat("website", "官网没有读到的页面", unread_site, "条", "等待材料或工具",
        "检查网址和网络后重跑 fetch_website.py；网站拦截时用浏览器保存页面，再 --browser-capture 导入", "官网恢复访问或改版时")
    removal = [show(r["display_name"]) for r in routes.values() if r["reason"] == "unreadable_removal_location"]
    mat("removal", "清理位置无法识别的产品表", removal, "份", "待知识库专员判断", "核对初筛结果，补可执行的位置", "初筛包更新时")
    if cell_progress.get("cells_total") and not t3_state[0]:
        mat("t3", "产品表逐格判断", [f"已判断 {cell_progress['cells_decided']} / {cell_progress['cells_total']} 个单元格；{t3_state[1]}"],
            "项", "知识库专员处理中", "review_cells.py 导出、判断、合并，再独立通读", "产品表更新时")

    registry.save()
    for kind, (relative, title, purpose) in LEDGERS.items():
        write_text(build_dir / "vault" / relative, render_ledger(kind, title, purpose, project_id, today, records[kind]))

    # One row per answerable item, so a grouped entry still gets a separate answer for each file.
    with (build_dir / "客户确认表.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["编号", "要确认的问题", "回答（保留 / 不用 / 文字）"])
        for record in sorted(records["CUS"], key=lambda r: r["id"]):
            for item in record["items"]:
                writer.writerow([item["sub_id"], item["question"], ""])
    write_json(build_dir / "governance.json", {kind: [{k: v for k, v in r.items() if k != "details"} for r in rs] for kind, rs in records.items()})
    return {"anm_ids": anm_ids, "cus_items": {f"{i['item_id']}:{i['location']}": r["id"] for r in records["CUS"] for i in r["items"]},
            "counts": {kind: len(rs) for kind, rs in records.items()}}


def render_ledger(kind: str, title: str, purpose: str, project_id: str, today: str, records: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", "", f"- 项目ID：{project_id}", f"- 最近检查日期：{today}", f"- 文件用途：{purpose}",
             "- 总览入口：[[00_知识库总览]]",
             "- 维护：本 Skill 自动登记；“当前阶段”“下一责任人”可以直接改，详情下可以加“#### 处理记录”，重新生成时会保留。",
             "", "## 当前事项", ""]
    columns = COLUMNS[kind]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + " --- |" * len(columns))
    for record in sorted(records, key=lambda r: r["id"]):
        lines.append("| " + " | ".join([record["id"], *(c.replace("|", "\\|") for c in record["row"]), record["updated"]]) + " |")
    if not records:
        lines.append("| 无 |" + " |" * (len(columns) - 1))
    lines.extend(["", "## 事项详情", ""])
    for record in sorted(records, key=lambda r: r["id"]):
        lines.extend([f"### {record['id']}｜{record['row'][0]}", "", *record["details"], ""])
    return "\n".join(lines) + "\n"


def parse_ledger(text: str) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Return the table header, rows by ID, and each ID's 处理记录 lines."""
    header: list[str] = []
    rows: dict[str, list[str]] = {}
    notes: dict[str, list[str]] = {}
    current, in_notes = None, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and not re.fullmatch(r"\|[\s:|-]+\|?", stripped):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not header and cells and cells[0].endswith("ID"):
                header = cells
            elif header and cells and re.match(r"^[A-Z]-\d{3}-[A-Z]{3}-\d{3}$", cells[0]):
                rows[cells[0]] = cells
        match = re.match(r"^###\s+([A-Z]-\d{3}-[A-Z]{3}-\d{3})｜", stripped)
        if match:
            current, in_notes = match.group(1), False
        elif current and stripped == RECORD_HEADING:
            in_notes = True
            notes[current] = [RECORD_HEADING]
        elif current and in_notes:
            if stripped.startswith("### "):
                in_notes = False
            else:
                notes[current].append(line)
    return header, rows, notes


def merge_ledger(old_text: str, new_text: str) -> str:
    """Keep what people wrote: their stage, owner, and notes win over the machine's defaults.

    Entries the machine no longer sees stay listed, marked as solved for a person to confirm.
    """
    old_header, old_rows, old_notes = parse_ledger(old_text)
    new_header, new_rows, _ = parse_ledger(new_text)
    if not new_header or not old_header:
        return new_text

    def row_line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    # 1. The table: human columns come from the old ledger; vanished entries are kept.
    out: list[str] = []
    last_row = max((i for i, line in enumerate(new_text.splitlines())
                    if line.strip().startswith("|") and line.strip().strip("|").split("|")[0].strip() in new_rows), default=None)
    for index, line in enumerate(new_text.splitlines()):
        record_id = line.strip().strip("|").split("|")[0].strip() if line.strip().startswith("|") else ""
        if record_id in new_rows and record_id in old_rows:
            cells = list(new_rows[record_id])
            for column in HUMAN_COLUMNS:
                if column in new_header and column in old_header:
                    cells[new_header.index(column)] = old_rows[record_id][old_header.index(column)]
            line = row_line(cells)
        out.append(line)
        if index == last_row:
            for gone, old in sorted(old_rows.items()):
                if gone in new_rows:
                    continue
                cells = [old[old_header.index(c)] if c in old_header and old_header.index(c) < len(old) else "" for c in new_header]
                stage = new_header.index("当前阶段") if "当前阶段" in new_header else None
                if stage is not None and cells[stage] not in CLOSED_STAGES:
                    cells[stage] = SOLVED_STAGE
                out.append(row_line(cells))

    # 2. Each entry's 处理记录 goes back at the end of that entry.
    merged: list[str] = []
    current = None
    for line in out + ["### __END__｜"]:
        match = re.match(r"^###\s+(\S+)｜", line.strip())
        if match:
            if current in old_notes:
                while merged and not merged[-1].strip():
                    merged.pop()
                merged.extend(["", *old_notes[current], ""])
            current = match.group(1)
        if line != "### __END__｜":
            merged.append(line)

    # 3. Notes on entries that left the details keep a heading of their own.
    present = {re.match(r"^###\s+(\S+)｜", l.strip()).group(1) for l in merged if re.match(r"^###\s+(\S+)｜", l.strip())}
    for record_id, note in sorted(old_notes.items()):
        if record_id not in present:
            merged.extend(["", f"### {record_id}｜（系统已不再登记，保留人工记录）", "", *note])
    return "\n".join(merged).rstrip() + "\n"


def cleanup_record(build_dir: Path, blocks: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> None:
    """What was removed and where, without repeating the removed text."""
    labels = {"price": "报价金额", "bare_cost": "成本", "cost_word": "成本用语", "payment_term": "付款条件",
              "quoted_terms": "给客户的报价条件",
              "order_number": "订单号", "email": "外部邮箱", "phone": "电话", "denied_term": "竞品或禁用词",
              "quantity": "数量或包装", "material_usage": "单耗或用料", "internal_note": "业务备注",
              "customer_name": "可能的客户名", "paired_answer": "被删问题的答案", "reviewer_dropped": "人工判断删除"}
    totals: dict[str, int] = defaultdict(int)
    per_doc: dict[str, list[str]] = defaultdict(list)
    for block in blocks:
        if block["status"] != "removed":
            continue
        reasons = sorted({labels.get(r.split(":")[0], r.split(":")[0]) for r in block["hold_reasons"]})
        for reason in reasons:
            totals[reason] += 1
        per_doc[block["doc_id"]].append(f"{block['locator']}（{'、'.join(reasons)}）")
    lines = ["# 内容清理记录", "", "按初筛规则删除的内容只记原因和位置，不重复原文。", "", "## 按原因", "",
             "| 原因 | 段数 |", "| --- | ---: |"]
    lines.extend(f"| {reason} | {count} |" for reason, count in sorted(totals.items(), key=lambda kv: -kv[1]))
    lines.extend(["", "## 按资料", ""])
    for doc_id, places in sorted(per_doc.items(), key=lambda kv: -len(kv[1])):
        doc = docs[doc_id]
        lines.append(f"- {link(doc)}：{len(places)} 段 — " + "；".join(places[:8]) + (" 等" if len(places) > 8 else ""))
    write_text(build_dir / "vault" / "05_数据与审核/50_运行记录/内容清理记录.md", "\n".join(lines) + "\n")
