#!/usr/bin/env python3
"""External research: public industry knowledge for gaps the client's own material cannot fill.

Works on the published vault alone (--vault), so a writer on another computer can use it in
the middle of an article; no run folder and no package are needed.

plan --from-gaps     reads 05_数据与审核/20_知识库覆盖与缺口, keeps the 缺 / 薄弱 sub-sections that
                     are public knowledge, and writes research/plan.md for a person to tick.
plan --from-outline  takes the outline sections a writer could not find; client-only ones are
                     reported (ask the client, never research), public ones already in
                     20_外部公共知识 are reused, the rest go straight to the to-do list, up to the
                     per-article budget.
approve              moves ticked plan items (or --gaps / --all) to the to-do list.
collect              reads the model's answer-*.jsonl, checks every source and every line, and
                     writes evidence into 02_源资料/50_外部调研证据 and pages into
                     03_正式知识/20_外部公共知识; then refreshes the index and the writing catalog.
status / mark        what has been researched, what waits for a spot check, what is overdue.

Research never touches the client's own facts (capacity, certificates, cases, lead time, price):
those are client-only and go to CUS. Publishing never removes these two folders: they are
research's own, recorded in their own receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from curate import CHECKED_TOKEN, squeezed
from kb_common import MODULES, frontmatter, load_json, parse_frontmatter, safe_name, vault_lock, write_json, write_text
from kb_index import build as build_index, tokens
from kb_lookup import classify, load_pages, load_rules, score, write_catalog
from refresh_navigation import obsidian_prefix


STATE = Path("05_数据与审核/70_构建状态/research")
COVERAGE = Path("05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md")
EVIDENCE = Path("02_源资料/50_外部调研证据")
PAGES = Path("03_正式知识/20_外部公共知识")
ARCHIVE = Path("05_数据与审核/60_版本归档/10_正式知识历史")
RECEIPT = Path("05_数据与审核/50_运行记录/research-receipt.json")
LOG = Path("05_数据与审核/50_运行记录/外部调研记录.jsonl")
EXT = re.compile(r"\{\{ext:(S\d+)-(\d+)\}\}")
REUSE_SCORE = 0.35
EXCERPT_MAX = 1500
# Where an external page goes, by module key; industry knowledge is the usual one.
FOLDERS = {key: value.split("/")[0] for key, value in MODULES.items()}
README = """# 外部调研：怎么做

`todo.jsonl` 里一行一个缺口。逐个调研，结果写进本目录的 `answer-<任意名字>.jsonl`（一行一个缺口），再运行
`research.py collect --vault <知识库>`。脚本会逐条核对，过不了的写清原因，改完再 collect。

## 只查公共知识
- 只查行业公共知识：标准含义、材料特性、工作原理、选型原则、行业工况、术语、认证和法规本身的含义。
- **不查客户自己的事**：产能、厂房、员工、证书、案例、交期、MOQ、价格、保修。这些只能问客户。
- 页面里不写客户名字，不写“我们”“本公司”，不把行业做法写成客户的能力。

## 来源
- 每个缺口最多搜 {searches} 次、用 {pages} 个网页（todo 里的 budget）。
- 优先：标准组织、行业协会、政府机构、学术资料，其次专业技术媒体、工程手册；百科只作辅助。
- **不用**：同行业其他制造商、品牌商、经销商的网站（竞品宣传）、电商平台、论坛、AI 生成内容，以及 kb-config.json 里 deny_terms 的名称。
- 摘录要逐字抄网页原文（可以是英文），每段不超过 1500 字；不要改写。
- 写进页面的数字至少要有 {numbers} 个不同来源（百科不算）对得上；对不上就并列写出来，标“需核实”。

## answer 的格式（一行一个 JSON）
```json
{{"gap_key": "GAP-industry-技术知识", "status": "done",
  "searches": ["butterfly valve seat material chemical resistance", "..."],
  "sources": [{{"id": "S1", "url": "https://...", "title": "网页标题", "publisher": "发布方",
               "publisher_type": "standard", "fetched_at": "{today}", "excerpts": ["原文摘录 1", "原文摘录 2"]}}],
  "page": {{"title": "阀座材料的耐化学性", "module": "industry", "aliases": ["seat material chemical resistance"],
           "markdown": "## 一句话说明\\n\\n- …… {{{{ext:S1-1}}}}\\n\\n## 技术知识\\n\\n- …… {{{{ext:S1-2}}}}{{{{ext:S2-1}}}}\\n\\n## 需核实\\n\\n- ……"}}}}
```
- `publisher_type`：{types}。
- `module`：industry（行业知识，最常用）、product、solution、faq、other。
- 页面每条要点、每个表格行结尾都标 `{{{{ext:来源编号-摘录序号}}}}`；数字、标准号必须原样出现在引用的摘录里。
- 实在查不到可靠来源：写 `{{"gap_key": "…", "status": "not_found", "note": "查了什么、为什么不能用"}}`。
"""


def read(path: Path, default: Any) -> Any:
    return load_json(path) if path.is_file() else default


def state_of(vault: Path) -> dict[str, Any]:
    return read(vault / STATE / "state.json", {"gaps": {}})


def save_state(vault: Path, state: dict[str, Any]) -> None:
    write_json(vault / STATE / "state.json", state)


def todo_of(vault: Path) -> list[dict[str, Any]]:
    path = vault / STATE / "todo.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []


def save_todo(vault: Path, items: list[dict[str, Any]], rules: dict[str, Any]) -> None:
    folder = vault / STATE
    write_text(folder / "todo.jsonl", "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
    budget = rules["budget"]
    write_text(folder / "README.md", README.format(searches=budget["searches_per_gap"], pages=budget["pages_per_gap"],
                                                   numbers=rules.get("number_sources", 2), today=date.today().isoformat(),
                                                   types="、".join(f"{k}（{v}）" for k, v in rules["source_types"].items())))


def context(vault: Path) -> dict[str, Any]:
    """What the researcher needs to know about the client's field, from the knowledge layer's page names."""
    pages = load_pages(vault)
    products = [p.title for p in pages if p.layer == "客户知识" and p.stem.startswith(("产品系列-", "产品大类-"))][:12]
    return {"products": products, "existing_external": [p.title for p in pages if p.layer == "外部公共知识"][:30]}


def todo_item(gap_key: str, question: str, origin: str, rules: dict[str, Any], **extra: Any) -> dict[str, Any]:
    budget = rules["budget"]
    return {"gap_key": gap_key, "question": question, "origin": origin,
            "budget": {"searches": budget["searches_per_gap"], "pages": budget["pages_per_gap"]}, **extra}


def coverage_gaps(vault: Path) -> list[dict[str, Any]]:
    """缺 / 薄弱 rows of the coverage page: (module, topic, sub-section, status, hint)."""
    path = vault / COVERAGE
    if not path.is_file():
        return []
    names = {value.split("/")[0]: key for key, value in MODULES.items()}
    module, topic, header, rows = None, "", None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            module, topic, header = names.get(line[3:].strip()), "", None
        elif line.startswith("### "):
            topic = line[4:].strip()
        elif line.startswith("|") and module:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[0] == "小板块":
                header = cells
            elif header and len(cells) == len(header) and not set(cells[0]) <= set("-: "):
                row = dict(zip(header, cells))
                if row.get("状态") in {"缺", "薄弱"}:
                    rows.append({"module": module, "topic": topic, "section": row["小板块"], "status": row["状态"],
                                 "hint": row.get("怎么补", "")})
        elif not line.strip():
            continue
        else:
            header = None if not line.startswith("|") else header
    return rows


def plan_from_gaps(vault: Path, rules: dict[str, Any]) -> dict[str, Any]:
    state = state_of(vault)
    public = set(rules.get("public_sections", []))
    client_modules = set(rules.get("client_modules", []))
    industry = str(rules.get("industry") or "").strip()
    items, client_only = [], []
    for row in coverage_gaps(vault):
        key = "-".join(["GAP", row["module"], *([safe_name(row["topic"])] if row["topic"] else []), safe_name(row["section"])])
        if state["gaps"].get(key, {}).get("status") in {"done", "todo"}:
            continue
        # A product's own parameters or a company's own capacity are the client's facts, whatever the section is called.
        if row["module"] not in client_modules and row["section"] in public and classify(row["section"], rules) == "公共知识":
            question = f"{industry}{row['section']}" if industry else f"{row['section']}（{row['topic'] or FOLDERS[row['module']]}）"
            items.append({**row, "gap_key": key, "question": question, "approve": ""})
        else:
            client_only.append({**row, "gap_key": key})
    old = {i["gap_key"]: i for i in read(vault / STATE / "plan.json", {}).get("items", [])}
    for item in items:  # A tick or a rewritten question survives a re-plan.
        if item["gap_key"] in old:
            item["approve"] = old[item["gap_key"]].get("approve", "")
            item["question"] = old[item["gap_key"]].get("question") or item["question"]
    write_json(vault / STATE / "plan.json", {"items": items, "client_only": client_only, "at": datetime.now().isoformat(timespec="seconds")})
    lines = ["# 建议外部调研清单", "", "<!-- compile-enterprise-knowledge research -->",
             "按知识库覆盖与缺口里“缺 / 薄弱”的小板块生成。只列网上能查到的公共知识；客户自己的事在最后，要问客户。",
             "要调研的在“要调研”列写“是”，“调研问题”可以改得更具体，然后运行 `research.py approve --vault <知识库>`。", "",
             "| 编号 | 模块 | 小板块 | 状态 | 调研问题 | 要调研 |", "| --- | --- | --- | --- | --- | --- |"]
    lines.extend(f"| {i['gap_key']} | {MODULES[i['module']].split('/')[0]} | {i['section']} | {i['status']} | {i['question']} | {i['approve']} |"
                 for i in items)
    if client_only:
        lines.extend(["", "## 客户自己的事（不外部调研，要问客户）", "", "| 模块 | 页面 | 小板块 | 状态 | 怎么补 |", "| --- | --- | --- | --- | --- |"])
        lines.extend(f"| {MODULES[c['module']].split('/')[0]} | {c['topic']} | {c['section']} | {c['status']} | {c['hint']} |" for c in client_only)
    write_text(vault / STATE / "plan.md", "\n".join(lines) + "\n")
    return {"plan": str(vault / STATE / "plan.md"), "public": len(items), "client_only": len(client_only),
            "next": "在 plan.md 的“要调研”列写“是”（或运行 approve --gaps 编号 / --all），再运行 research.py approve"}


def approve(vault: Path, rules: dict[str, Any], keys: list[str], everything: bool) -> dict[str, Any]:
    plan = read(vault / STATE / "plan.json", {"items": []})
    ticked: dict[str, str] = {}
    page = vault / STATE / "plan.md"
    if page.is_file():
        for line in page.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 6 and cells[0].startswith("GAP-"):
                ticked[cells[0]] = cells[4] if cells[5] in {"是", "要", "yes", "y", "Y", "✓", "√"} else ""
    chosen = [i for i in plan["items"] if everything or i["gap_key"] in keys or ticked.get(i["gap_key"])]
    state, todo = state_of(vault), todo_of(vault)
    waiting = {t["gap_key"] for t in todo}
    hints = context(vault)
    for item in chosen:
        question = ticked.get(item["gap_key"]) or item["question"]
        if item["gap_key"] not in waiting:
            todo.append(todo_item(item["gap_key"], question, "gaps", rules, module=item["module"], section=item["section"], context=hints))
        state["gaps"][item["gap_key"]] = {**state["gaps"].get(item["gap_key"], {}), "status": "todo", "question": question,
                                          "origin": "gaps", "planned_at": date.today().isoformat()}
    save_todo(vault, todo, rules)
    save_state(vault, state)
    return {"approved": [i["gap_key"] for i in chosen], "todo": len(todo), "next": f"按 {vault / STATE / 'README.md'} 调研，写 answer-*.jsonl，再运行 research.py collect"}


def plan_from_outline(vault: Path, rules: dict[str, Any], sections: list[str], article: str | None) -> dict[str, Any]:
    pages = [p for p in load_pages(vault) if p.layer == "外部公共知识"]
    state, todo = state_of(vault), todo_of(vault)
    waiting = {t["gap_key"] for t in todo}
    limit = rules["budget"]["gaps_per_article"]
    hints = context(vault)
    result: dict[str, list[Any]] = {"client_only": [], "reused": [], "todo": [], "over_budget": [], "already": []}
    for text in sections:
        key = "OL-" + hashlib.sha256(re.sub(r"[\W_]+", "", text.casefold()).encode("utf-8")).hexdigest()[:10]
        if classify(text, rules) == "客户专属":
            result["client_only"].append(text)
            continue
        wanted = set(tokens(text).split())
        best = max(((score(s, wanted), s) for p in pages for s in p.sections if s.text), key=lambda x: x[0], default=(0, None))
        if best[1] is not None and best[0] >= REUSE_SCORE:
            result["reused"].append({"section": text, "page": f"{best[1].page.stem}#{best[1].name}"})
            continue
        known = state["gaps"].get(key, {})
        if key in waiting or known.get("status") in {"todo", "done"}:
            result["already"].append({"section": text, "gap_key": key, "status": known.get("status", "todo")})
            continue
        if len(result["todo"]) >= limit:
            result["over_budget"].append(text)
            continue
        todo.append(todo_item(key, text, "outline", rules, article=article or "", module="industry", context=hints))
        state["gaps"][key] = {"status": "todo", "question": text, "origin": "outline", "article": article or "",
                              "planned_at": date.today().isoformat()}
        result["todo"].append({"section": text, "gap_key": key})
    save_todo(vault, todo, rules)
    save_state(vault, state)
    return {**result, "next": (f"按 {vault / STATE / 'README.md'} 调研 todo 里的缺口，写 answer-*.jsonl，再运行 research.py collect"
                               if result["todo"] else "没有要调研的缺口")}


def domain(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.")


def check_answer(answer: dict[str, Any], item: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    budget = item.get("budget", {})
    sources = answer.get("sources") or []
    if len(answer.get("searches") or []) > budget.get("searches", 99):
        errors.append(f"搜了 {len(answer['searches'])} 次，超过上限 {budget['searches']} 次")
    if not sources:
        errors.append("没有来源")
    if len(sources) > budget.get("pages", 99):
        errors.append(f"用了 {len(sources)} 个网页，超过上限 {budget['pages']} 个")
    deny = [t.casefold() for t in rules.get("_deny", []) if t]
    own_domains = [d.casefold().removeprefix("www.") for d in rules.get("_own_domains", []) if d]
    ids = set()
    for source in sources:
        name = source.get("id") or "?"
        ids.add(name)
        url = str(source.get("url") or "")
        if not re.match(r"https?://", url):
            errors.append(f"{name}：网址要以 http(s):// 开头")
        if source.get("publisher_type") not in rules["source_types"]:
            errors.append(f"{name}：publisher_type 只能是 {'、'.join(rules['source_types'])}")
        if source.get("publisher_type") in rules.get("excluded_publishers", []):
            errors.append(f"{name}：{source.get('publisher_type')} 类来源不能用")
        if any(domain(url) == d or domain(url).endswith("." + d) for d in own_domains):
            errors.append(f"{name}：客户自己的网站已经在知识库里，不算外部资料")
        blob = " ".join([url, str(source.get("title", "")), str(source.get("publisher", "")), *map(str, source.get("excerpts") or [])]).casefold()
        hit = [t for t in deny if t in blob]
        if hit:
            errors.append(f"{name}：出现了竞品或第三方名称 {'、'.join(hit[:3])}，不能用")
        if not source.get("excerpts"):
            errors.append(f"{name}：没有原文摘录")
        if any(len(str(e)) > EXCERPT_MAX for e in source.get("excerpts") or []):
            errors.append(f"{name}：摘录每段不超过 {EXCERPT_MAX} 字")
        try:
            date.fromisoformat(str(source.get("fetched_at", "")))
        except ValueError:
            errors.append(f"{name}：fetched_at 写成 YYYY-MM-DD")
    if len(ids) != len(sources):
        errors.append("来源编号（id）不能重复")
    page = answer.get("page") or {}
    markdown = str(page.get("markdown") or "")
    if not page.get("title"):
        errors.append("页面没有 title")
    if page.get("module", "industry") not in MODULES:
        errors.append(f"module 只能是 {'、'.join(MODULES)}")
    if "## 一句话说明" not in markdown:
        errors.append("页面要有 ## 一句话说明")
    excerpts = {(s.get("id"), str(i)): str(e) for s in sources for i, e in enumerate(s.get("excerpts") or [], start=1)}
    kinds = {s.get("id"): s.get("publisher_type") for s in sources}
    own = [t.casefold() for t in rules.get("_own", []) if len(t) >= 2]
    for number, line in enumerate(markdown.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#") or re.fullmatch(r"\|?\s*:?-{3,}.*", text):
            continue
        cited = EXT.findall(text)
        if not cited:
            errors.append(f"第 {number} 行没有标出处 {{{{ext:来源-摘录}}}}")
            continue
        missing = [f"{s}-{i}" for s, i in cited if (s, i) not in excerpts]
        if missing:
            errors.append(f"第 {number} 行引用的摘录不存在：{'、'.join(missing)}")
            continue
        bare = EXT.sub("", text)
        if any(t in bare.casefold() for t in own) or re.search(r"(?i)我们|本公司|我司|\bour company\b", bare):
            errors.append(f"第 {number} 行写到了客户自己；外部公共知识页不写客户")
        numbers = CHECKED_TOKEN.findall(bare)
        source_text = squeezed(" ".join(excerpts[c] for c in cited))
        absent = [t for t in numbers if squeezed(t) not in source_text]
        if absent:
            errors.append(f"第 {number} 行的 {'、'.join(absent[:5])} 在引用的摘录里找不到")
        elif numbers and "需核实" not in bare:
            needed = rules.get("number_sources", 2)
            by_source: dict[str, str] = {}
            for s, i in cited:
                if kinds.get(s) != "reference":
                    by_source[s] = by_source.get(s, "") + " " + excerpts[(s, i)]
            squeezed_by_source = {s: squeezed(blob) for s, blob in by_source.items()}
            counts = {t: sum(1 for blob in squeezed_by_source.values() if squeezed(t) in blob) for t in dict.fromkeys(numbers)}
            weak = [t for t, c in counts.items() if c < needed]
            if weak:
                fewest = min(counts[t] for t in weak)
                errors.append(f"第 {number} 行的数字只有 {fewest} 个来源支撑（{'、'.join(weak[:5])}），至少要 {needed} 个；"
                              "对不上就并列写并标“需核实”")
    return errors


def evidence_path(gap_key: str, source: dict[str, Any]) -> Path:
    digest = hashlib.sha256(str(source["url"]).encode("utf-8")).hexdigest()[:8]
    return EVIDENCE / safe_name(gap_key) / f"证据-{safe_name(str(source.get('publisher') or domain(source['url'])))[:40]}-{digest}.md"


def evidence_text(gap_key: str, source: dict[str, Any], question: str) -> str:
    digest = hashlib.sha256(str(source["url"]).encode("utf-8")).hexdigest()[:10]
    title = f"外部证据-{source.get('publisher') or domain(source['url'])}-{source.get('title', '')}".strip("-")
    fields = {"doc_id": f"EXT-EV-{digest}", "title": title[:120], "layer": "外部证据", "tier": "外部", "visibility": "internal",
              "uses": ["agent"], "status": "有效", "url": source["url"], "publisher": source.get("publisher", ""),
              "publisher_type": source.get("publisher_type", ""), "fetched_at": source.get("fetched_at", ""), "gap_key": gap_key}
    lines = [frontmatter(fields), "", f"# {fields['title']}", "",
             f"> **来源**：[{source.get('title') or source['url']}]({source['url']})",
             f"> - 发布方：{source.get('publisher', '')}（{source.get('publisher_type', '')}）",
             f"> - 抓取日期：{source.get('fetched_at', '')}",
             f"> - 调研问题：{question}",
             "> - 摘录逐字来自网页原文，只作行业公共知识的证据，不代表客户。", ""]
    for index, excerpt in enumerate(source.get("excerpts") or [], start=1):
        lines.extend([f"## 摘录 {index}", "", str(excerpt).strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def page_text(vault: Path, gap_key: str, answer: dict[str, Any], files: dict[str, str], rules: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    page = answer["page"]
    prefix = obsidian_prefix(vault)
    numbers: dict[tuple[str, str], int] = {}

    def cite(match: re.Match[str]) -> str:
        key = (match.group(1), match.group(2))
        number = numbers.setdefault(key, len(numbers) + 1)
        target = files[match.group(1)].removesuffix(".md")
        return f"[[{prefix}{target}#摘录 {match.group(2)}|〔{number}〕]]"

    today = date.today()
    due = today + timedelta(days=rules["budget"].get("review_days", 180))
    fields = {"doc_id": f"EXT-{safe_name(gap_key)}", "title": f"外部-{page['title']}", "layer": "外部公共知识", "tier": "外部",
              "origin": "external", "visibility": "internal", "uses": ["article", "agent"], "status": "待抽检",
              "module": f"{PAGES.name}/{FOLDERS[page.get('module', 'industry')]}", "gap_key": gap_key,
              "researched_at": today.isoformat(), "review_due": due.isoformat(), "sources": len(answer["sources"]),
              "generated_by": "compile-enterprise-knowledge research"}
    if page.get("aliases"):
        fields["aliases"] = [str(a) for a in page["aliases"]]
    body = EXT.sub(cite, str(page["markdown"]).strip())
    notice = (f"> 外部公共知识：由 AI 按外部资料整理，每条〔n〕链接到证据摘录；只作行业背景和论据，不代表客户的能力、承诺或认证。"
              f"状态：待抽检；复查日期 {due.isoformat()}。")
    sources = ["", "## 来源", ""] + [f"- [{s.get('title') or s['url']}]({s['url']})（{s.get('publisher', '')}，{s.get('publisher_type', '')}，"
                                     f"抓取 {s.get('fetched_at', '')}）" for s in answer["sources"]]
    text = "\n".join([frontmatter(fields), "", f"# {page['title']}", "", notice, "", body, *sources]) + "\n"
    return text, fields


def archive(vault: Path, key: str, stamp: str) -> None:
    target = vault / key
    if target.is_file():
        backup = vault / ARCHIVE / stamp / key
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)


def collect(vault: Path, rules: dict[str, Any]) -> dict[str, Any]:
    folder = vault / STATE
    todo = {t["gap_key"]: t for t in todo_of(vault)}
    state = state_of(vault)
    receipt = read(vault / RECEIPT, {"files": {}})
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    done, failed, not_found = [], {}, []
    answers = sorted(folder.glob("answer-*.jsonl"))
    for answer_file in answers:
        keep_lines = []
        for line in answer_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                answer = json.loads(line)
            except ValueError:
                failed[f"{answer_file.name}"] = ["有一行不是 JSON"]
                keep_lines.append(line)
                continue
            key = answer.get("gap_key")
            item = todo.get(key)
            if not item:
                failed[str(key)] = ["不在 todo.jsonl 里（已经收过，或编号写错）"]
                continue
            if answer.get("status") == "not_found":
                state["gaps"][key] = {**state["gaps"].get(key, {}), "status": "not_found", "note": answer.get("note", ""),
                                      "at": date.today().isoformat()}
                not_found.append(key)
                todo.pop(key)
                continue
            errors = check_answer(answer, item, rules)
            if errors:
                failed[key] = errors[:12]
                keep_lines.append(line)
                continue
            files: dict[str, str] = {}
            for source in answer["sources"]:
                relative = evidence_path(key, source)
                text = evidence_text(key, source, item["question"])
                archive(vault, relative.as_posix(), stamp)
                write_text(vault / relative, text)
                files[source["id"]] = relative.as_posix()
                receipt["files"][relative.as_posix()] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            text, fields = page_text(vault, key, answer, files, rules)
            previous = state["gaps"].get(key, {}).get("page")
            relative = (PAGES / FOLDERS[answer["page"].get("module", "industry")] / f"{safe_name(fields['title'])}.md").as_posix()
            for old in {previous, relative} - {None}:
                archive(vault, old, stamp)
            if previous and previous != relative and (vault / previous).is_file():
                (vault / previous).unlink()
            write_text(vault / relative, text)
            receipt["files"][relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            state["gaps"][key] = {**state["gaps"].get(key, {}), "status": "done", "page": relative, "evidence": list(files.values()),
                                  "researched_at": fields["researched_at"], "review_due": fields["review_due"], "question": item["question"]}
            done.append({"gap_key": key, "page": relative})
            todo.pop(key)
            (vault / LOG).parent.mkdir(parents=True, exist_ok=True)
            with (vault / LOG).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps({"at": stamp, "gap_key": key, "origin": item.get("origin"), "article": item.get("article", ""),
                                         "page": relative, "sources": [s["url"] for s in answer["sources"]]}, ensure_ascii=False) + "\n")
        collected = folder / "collected"
        collected.mkdir(exist_ok=True)
        shutil.move(str(answer_file), collected / f"{stamp}-{answer_file.name}")
        if keep_lines:  # What did not pass stays where the model can fix it.
            write_text(answer_file, "\n".join(keep_lines) + "\n")
    save_todo(vault, list(todo.values()), rules)
    save_state(vault, state)
    write_json(vault / RECEIPT, receipt)
    index = None
    if done:
        index = build_index(vault)["chunks"]
        write_catalog(vault)
    return {"done": done, "not_found": not_found, "failed": failed, "todo_left": len(todo), "index_chunks": index,
            "next": "改好 failed 里列出的问题再运行 collect" if failed else "完成；写作时用 kb_lookup.py pack 取用"}


def external_pages(vault: Path) -> list[dict[str, Any]]:
    found = []
    for path in sorted((vault / PAGES).rglob("*.md")) if (vault / PAGES).is_dir() else []:
        fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fields.get("origin") == "external":
            found.append({"page": path.relative_to(vault).as_posix(), "title": fields.get("title", path.stem),
                          "status": fields.get("status", ""), "review_due": fields.get("review_due", "")})
    return found


def status(vault: Path) -> dict[str, Any]:
    state = state_of(vault)
    pages = external_pages(vault)
    today = date.today().isoformat()
    counts: dict[str, int] = {}
    for gap in state["gaps"].values():
        counts[gap.get("status", "")] = counts.get(gap.get("status", ""), 0) + 1
    return {"gaps": counts, "todo": len(todo_of(vault)), "pages": len(pages),
            "to_spot_check": [p for p in pages if p["status"] == "待抽检"],
            "overdue": [p for p in pages if p["review_due"] and p["review_due"] < today and p["status"] != "已失效"]}


def mark(vault: Path, page: str, new_status: str) -> dict[str, Any]:
    """A spot check's result: 有效 (checked), 待抽检, or 已失效 (writers stop seeing it)."""
    matches = [p for p in external_pages(vault) if Path(p["page"]).stem == page or p["page"] == page or p["title"] == page]
    if len(matches) != 1:
        raise SystemExit(f"找不到唯一的外部公共知识页：{page}")
    target = vault / matches[0]["page"]
    text = target.read_text(encoding="utf-8")
    archive(vault, matches[0]["page"], datetime.now().strftime("%Y%m%d-%H%M%S"))
    text = re.sub(r'(?m)^status: ".*"$', f'status: "{new_status}"', text, count=1)
    text = re.sub(r"状态：[^；]*；", f"状态：{new_status}；", text, count=1)
    if new_status == "有效":
        if re.search(r'(?m)^checked_at:', text):
            text = re.sub(r'(?m)^checked_at: ".*"$', f'checked_at: "{date.today().isoformat()}"', text, count=1)
        else:
            text = re.sub(r'(?m)^researched_at: ', f'checked_at: "{date.today().isoformat()}"\nresearched_at: ', text, count=1)
    write_text(target, text)
    fields = parse_frontmatter(text)
    state = state_of(vault)
    gap_key = fields.get("gap_key") or next((k for k, v in state["gaps"].items() if v.get("page") == matches[0]["page"]), None)
    if gap_key and gap_key in state["gaps"]:
        if new_status == "已失效":
            state["gaps"][gap_key]["status"] = "invalidated"
            state["gaps"][gap_key]["invalidated_at"] = date.today().isoformat()
            save_state(vault, state)
        elif state["gaps"][gap_key].get("status") == "invalidated":
            state["gaps"][gap_key]["status"] = "done"
            save_state(vault, state)
    build_index(vault)
    write_catalog(vault)
    return {"page": matches[0]["page"], "status": new_status}


def main() -> int:
    parser = argparse.ArgumentParser(description="External research into 20_外部公共知识, on the vault alone.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--vault", required=True)
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-gaps", action="store_true", help="From the coverage page's 缺 / 薄弱 sub-sections")
    source.add_argument("--from-outline", action="append", help="Outline sections the knowledge base has nothing for; ; separated")
    plan.add_argument("--article", help="Article title, for the record")
    ok = sub.add_parser("approve")
    ok.add_argument("--vault", required=True)
    ok.add_argument("--gaps", default="", help="Comma separated gap keys; without it, the ticks in plan.md")
    ok.add_argument("--all", action="store_true")
    for name in ("collect", "status"):
        sub.add_parser(name).add_argument("--vault", required=True)
    flag = sub.add_parser("mark")
    flag.add_argument("--vault", required=True)
    flag.add_argument("--page", required=True)
    flag.add_argument("--status", required=True, choices=["有效", "待抽检", "已失效"])
    args = parser.parse_args()
    vault = Path(args.vault).resolve()
    if not (vault / "01_工作台").is_dir():
        print(f"{vault} 不是知识库项目（没有 01_工作台）。")
        return 2
    rules = load_rules(vault)
    if args.command == "status":
        print(json.dumps(status(vault), ensure_ascii=False, indent=2))
        return 0
    if not vault_lock(vault, f"research.py {args.command}"):
        return 3
    if args.command == "plan" and args.from_gaps:
        result = plan_from_gaps(vault, rules)
    elif args.command == "plan":
        parts = [p.strip() for value in args.from_outline for p in re.split(r"[;；\n]", value) if p.strip()]
        result = plan_from_outline(vault, rules, parts, args.article)
    elif args.command == "approve":
        result = approve(vault, rules, [k.strip() for k in args.gaps.split(",") if k.strip()], args.all)
    elif args.command == "collect":
        result = collect(vault, rules)
    else:
        result = mark(vault, args.page, args.status)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
