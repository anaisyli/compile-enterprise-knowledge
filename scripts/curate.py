#!/usr/bin/env python3
"""The knowledge layer: pages a person can read, each line tied to its source.

prepare  packs the source blocks for each topic, deduplicated, with style codes the script
         found itself, and writes the writing rules for the model.
check    refuses any line without a citation, any number or style code the cited source
         does not contain, any line that cites a flagged passage without carrying ⚠, and a
         page that cites fewer than half of the documents it was given.
build    turns citations into links and writes the pages.

The model organizes and summarizes; it does not add facts. The checker is what holds it
to that.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from guard_blocks import HARD_PATTERNS
from kb_common import BUILD_DIRECTORY, MODULES, SKILL_VERSION, frontmatter, load_config, load_json, load_jsonl, safe_name, write_json, write_text
from sections import load_sections


CURATION = "curation"
KNOWLEDGE = "vault/03_正式知识/10_客户知识"
CITATION = re.compile(r"\{\{src:([0-9a-f]{12})\}\}")
STYLE_TABLE = "{{款号表}}"
# Numbers, percentages, weights, years and model codes must be found in the cited text.
CHECKED_TOKEN = re.compile(r"[A-Za-z]*\d+(?:[.,]\d+)?[A-Za-z%㎡]*(?:-[A-Za-z0-9]+)*")
MIN_CATEGORY_PAGES = 3
USABLE = {"kept", "flagged"}
USABLE_DOCS = {"有效", "部分待客户确认", "参考稿"}
# Each sub-section gets its own share, so one busy sub-section cannot fill the page.
SECTION_MAX = 40
LINE_MAX = 30
# A sub-section with this much source text becomes a page of its own.
SPLIT_AT = 120
SPLIT_MAX = 150
OUTSIDE_HITS = 2
# A page has to cite at least this share of the documents its input.md came from.
COVERAGE = 0.5
WEBSITE_PRODUCTS = "官网页面/"
WEBSITE_COMPANY = "官网页面/公司与服务"

# One page per module; its sub-sections come from assets/sections-default.json.
# "gate": a passage from outside the module's own documents must also match this.
# "file" is the page's file name, always 类别-名称; the heading inside may say more.
TOPICS = [
    # Catalogs often open with a line about the company, so one keyword is enough here.
    {"slug": "company", "title": "公司概况", "file": "公司-概况", "module": "company", "sections": "company",
     "modules": ["10_公司概述"], "outside_hits": 1},
    {"slug": "custom", "title": "解决方案与定制服务", "file": "解决方案-定制服务", "module": "solution", "sections": "solution",
     "modules": ["30_解决方案"]},
    {"slug": "cases", "title": "合作案例", "file": "案例-合作案例", "module": "case", "sections": "case", "modules": ["40_合作案例"], "gate": r"(?!)"},
    {"slug": "fabric", "title": "行业知识与洞察", "file": "行业-知识与洞察", "module": "industry", "sections": "industry",
     "modules": ["50_行业知识与洞察/10_企业提供"]},
    {"slug": "faq", "title": "常见问题", "file": "FAQ-常见问题", "module": "faq", "sections": "faq", "modules": ["60_FAQ"],
     "gate": r"(?i)^q[:：]|faq|\?|？"},
    {"slug": "other", "title": "服务与其他", "file": "其他-服务与其他", "module": "other", "sections": "other", "modules": ["70_其他"]},
]
TEMPLATE = """# 整理页写作规则

你在给企业知识库写一页给人看的整理页。只能用 input.md 里的原文块，不加任何原文没有的事实。

1. 先写 `# 标题`，再写 `## 一句话说明`。然后每个小板块一个 `## 小板块名`，名字和 input.md 里
   “小板块：”后面的完全一样，脚本靠它核对哪一节写了；下面可以再分 `###` 写最小的内容单元。
   原文不够写的小板块就不写，不要编；缺口页会把它列出来。
2. 每条要点、每个表格行、每段话的结尾都要标出处：`{{src:块编号}}`，一条可以标多个。
3. 数字、克重、成分、年份、款号必须原样出现在你引用的原文块里；不确定就不写。
4. 引用了标 ⚠ 的原文块，这一条也要写 ⚠，并说明是什么风险。
   标“参考整理稿”的块是从别家公开资料整理的参考，不是公司事实：只能写成“参考资料显示……（待公司确认）”，
   不能写成公司的参数或能力；同一点有公司自己的资料时，用公司自己的。
5. 不同原文说法不一致时，并列写出来，标“需核实”，各自标出处，不要选一个。
6. 宣传语（比如“最好的”“领先的”）不当事实写，可以写“官网自述：……”。
6.5 原文是表格的，整理成 Markdown 表；原文是流程或时间轴的，按原来的步骤或时间顺序写编号列表。
   只重新排列原文里的字，不加原文没有的步骤、说明或数字。
7. 产品类页面在合适位置单独一行写 `{{款号表}}`，脚本会填入从原文抽出的款号。
8. 最后写 `## 需核实`，列出有冲突、有风险或来源单一的说法。
9. 标题写这一页讲的范围：“产品总览”要概括 input.md 里每份资料的产品线，不能只写其中一类。
   想换标题就改 `# 标题`，重跑时以它为准。
10. 写全：input.md 里的每份资料至少写到一条，同一份资料里不同的款、参数、场景分开写，不要只挑几份概括。
   一页引用到的资料不到 input.md 里资料的一半，校验不通过，并列出没写到的资料。

写好后保存为同目录的 page.md。每个准备好的主题都要写；原文实在不够写的，在 kb-config.json 的
"skip_topics" 里写明主题编号和理由，例如 {"faq": "原文里问答太少"}，否则校验不通过。
"""


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def topics_for(config: dict[str, Any], docs: dict[str, dict[str, Any]], statuses: dict[str, str]) -> list[dict[str, Any]]:
    topics = [dict(t) for t in config.get("topics", TOPICS)]
    # Product pages follow the categories the website and the catalogs actually use.
    counts: dict[str, int] = defaultdict(int)
    for d in docs.values():
        if d.get("folder", "").startswith("官网页面/"):
            counts[d["folder"].split("/", 1)[1]] += 1
    categories = sorted(c for c, n in counts.items() if n >= MIN_CATEGORY_PAGES and c != "公司与服务")
    # The overview has one sub-section per product line, so every catalog is read.
    topics.append({"slug": "products", "title": "产品总览", "file": "产品-总览", "module": "product", "by_line": True,
                   "modules": ["20_产品介绍"]})
    for category in categories:
        # The whole name: "Tapestry Bag" and "Tapestry Jacket" share their first word.
        topics.append({"slug": f"cat-{safe_name(category)}", "title": category, "file": f"产品大类-{category}",
                       "module": "product", "sections": "product",
                       "folders": [f"官网页面/{category}"], "gate": rf"(?i){re.escape(category)}" if category else r"(?!)"})
    # Each catalog is a product line of its own, website or not: the category pages read the
    # website, and a catalog's own series would otherwise only get a few lines in the overview.
    slugs = {t["slug"] for t in topics}
    for doc in sorted(docs.values(), key=lambda d: d["title"]):
        if doc.get("folder") == "产品画册" and statuses.get(doc["doc_id"]) in USABLE_DOCS:
            name = doc["title"].split("-", 1)[-1]
            slug = f"line-{safe_name(name)}"
            if slug not in slugs:  # A config that lists its topics may name the series itself.
                slugs.add(slug)
                topics.append({"slug": slug, "title": f"产品系列-{name}", "file": f"产品系列-{name}", "module": "product",
                               "sections": "product", "doc_ids": [doc["doc_id"]], "gate": r"(?!)"})
    return topics


def line_name(doc: dict[str, Any]) -> str:
    folder = doc.get("folder", "")
    if folder.startswith("官网页面/"):
        return folder.split("/", 1)[1]
    if folder == "产品画册":
        return doc["title"].split("-", 1)[-1]
    return "其他产品资料"


def topic_sections(topic: dict[str, Any], table: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if topic.get("sections"):
        return table.get(topic["sections"], [])
    if topic.get("keywords"):  # A topic written in the older form: one keyword list, one section.
        return [{"name": "要点", "pattern": re.compile(topic["keywords"])}]
    return []


def assign(block: dict[str, Any], doc: dict[str, Any], topic: dict[str, Any], sections: list[dict[str, Any]]) -> str | None:
    """The sub-section a passage belongs to in this topic, or None when it does not belong here."""
    in_scope = doc.get("folder") in topic.get("folders", []) or doc["module"] in topic.get("modules", []) \
        or doc["doc_id"] in topic.get("doc_ids", [])
    if topic.get("by_line"):
        return line_name(doc) if in_scope else None
    if not in_scope and doc.get("folder", "").startswith(WEBSITE_PRODUCTS) and doc["folder"] != WEBSITE_COMPANY:
        # A product page belongs to its own category page; its titles ("Factory Direct ... OEM")
        # would otherwise fill every other page with the same listings.
        return None
    folded = block["text"].casefold()
    scores = [(len(set(s["pattern"].findall(folded))) if s.get("pattern") else 0, -i, s["name"]) for i, s in enumerate(sections)]
    best = max(scores, default=(0, 0, None))
    # A passage from another module's documents needs a clearer signal than one from the module's own.
    if best[0] >= (1 if in_scope else topic.get("outside_hits", OUTSIDE_HITS)):
        if not in_scope and topic.get("gate") and not re.search(topic["gate"], block["text"]):
            return None
        return best[2]
    # A passage of the module's own documents that names no sub-section is about the module itself.
    return sections[0]["name"] if in_scope and sections else None


def share(per_doc: dict[str, list[dict[str, Any]]], docs: dict[str, dict[str, Any]], limit: int, seen: set[str]) -> list[dict[str, Any]]:
    """Take passages document by document, official sources first, so no one document fills the share."""
    # Reference compilations come last: the company's own material speaks first.
    queues = [list(per_doc[d]) for d in sorted(per_doc, key=lambda d: (docs[d].get("role") == "reference", docs[d]["tier"], docs[d]["title"]))]
    chosen: list[dict[str, Any]] = []
    while len(chosen) < limit and any(queues):
        for queue in queues:
            while queue:
                block = queue.pop(0)
                key = squeezed(block["text"])
                if key not in seen and len(key) >= 6:
                    seen.add(key)
                    chosen.append(block)
                    break
            if len(chosen) >= limit:
                break
    return chosen


def plan(topic: dict[str, Any], blocks: list[dict[str, Any]], docs: dict[str, dict[str, Any]], statuses: dict[str, str],
         table: dict[str, list[dict[str, Any]]], keep: set[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """Sub-section -> what is available and what goes to the writer. Passages the written page
    already cites always stay, so a rerun cannot pull them out from under it."""
    sections = topic_sections(topic, table)
    names = [s["name"] for s in sections]
    available: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    cited: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unique: set[str] = set()
    for block in blocks:
        doc = docs.get(block["doc_id"])
        if not doc or block["status"] not in USABLE or statuses.get(block["doc_id"]) not in USABLE_DOCS:
            continue
        name = assign(block, doc, topic, sections)
        if block["block_id"] in keep:
            cited[name or (names[0] if names else line_name(doc))].append(block)
            continue
        # Repeated text counts once, so the numbers on the gap page mean distinct passages.
        key = squeezed(block["text"])
        if name and key not in unique and len(key) >= 6:
            unique.add(key)
            available[name][block["doc_id"]].append(block)
    limit = topic.get("max") or (LINE_MAX if topic.get("by_line") else SECTION_MAX)
    position = {b["block_id"]: i for i, b in enumerate(blocks)}
    seen = {squeezed(b["text"]) for group in cited.values() for b in group}
    result: dict[str, dict[str, Any]] = {}
    for name in [*names, *sorted((set(available) | set(cited)) - set(names))]:
        pool = available.get(name, {})
        chosen = cited.get(name, []) + share(pool, docs, max(0, limit - len(cited.get(name, []))), seen)
        documents = set(pool) | {b["doc_id"] for b in cited.get(name, [])}
        result[name] = {"chosen": sorted(chosen, key=lambda b: position[b["block_id"]]),
                        "available": sum(len(v) for v in pool.values()) + len(cited.get(name, [])),
                        "documents": len(documents), "pool": pool}
    return result


def page_title(page: str) -> str | None:
    match = re.search(r"(?m)^# (.+)$", page)
    return match.group(1).strip() if match else None


def page_sections(page: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"(?m)^#{2,3} (.+)$", page)}


def style_codes(blocks: list[dict[str, Any]], prefixes: list[str]) -> dict[str, list[str]]:
    if not prefixes:
        return {}
    pattern = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")[\s-]?[A-Z]?-?\d{2,4}(?:-\d+)?\b")
    found: dict[str, list[str]] = defaultdict(list)
    for block in blocks:
        for match in pattern.finditer(block["text"]):
            code = re.sub(r"\s+", "-", match.group(0).upper())
            if block["block_id"] not in found[code]:
                found[code].append(block["block_id"])
    return dict(sorted(found.items()))


def prepare(out: Path, config: dict[str, Any]) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    statuses = load_json(build / "statuses.json")
    blocks = load_jsonl(build / "blocks.jsonl")
    table = load_sections(config)
    report = []
    queue = topics_for(config, docs, statuses)
    while queue:
        topic = queue.pop(0)
        folder = build / CURATION / topic["slug"]
        page = (folder / "page.md").read_text(encoding="utf-8") if (folder / "page.md").is_file() else ""
        if topic.get("given"):
            # A sub-section split off its parent arrives with its passages already found.
            keep, pool = set(CITATION.findall(page)), topic.pop("given")
            cited = [b for group in pool.values() for b in group if b["block_id"] in keep]
            rest = {d: [b for b in group if b["block_id"] not in keep] for d, group in pool.items()}
            chosen = cited + share(rest, docs, max(0, SPLIT_MAX - len(cited)), {squeezed(b["text"]) for b in cited})
            position = {b["block_id"]: i for i, b in enumerate(blocks)}
            sections = {topic["title"]: {"chosen": sorted(chosen, key=lambda b: position[b["block_id"]]),
                                         "available": topic.pop("available"), "documents": topic.pop("documents")}}
        else:
            sections = plan(topic, blocks, docs, statuses, table, set(CITATION.findall(page)))
        topic["split"] = {}
        for name, section in list(sections.items()):
            if not topic.get("parent") and not topic.get("by_line") and section["available"] > SPLIT_AT:
                slug = f"{topic['slug']}-{safe_name(name)}"
                topic["split"][name] = slug
                prefix = (topic.get("file") or topic["title"]).split("-")[0]
                queue.append({"slug": slug, "title": name, "file": f"{prefix}-{name}", "module": topic["module"], "parent": topic["slug"],
                              "given": section["pool"], "available": section["available"], "documents": section["documents"]})
                section["chosen"] = []
        chosen = [b for s in sections.values() for b in s["chosen"]]
        # The page's own heading wins: people rename topics by editing it.
        topic["title"] = page_title(page) or topic["title"]
        summary = {name: {"available": s["available"], "documents": s["documents"], "blocks": [b["block_id"] for b in s["chosen"]]}
                   for name, s in sections.items()}
        record = {"slug": topic["slug"], "topic": topic["title"], "module": topic["module"], "parent": topic.get("parent"),
                  "split": topic["split"], "blocks": len(chosen), "documents": len({b["doc_id"] for b in chosen}),
                  "characters": sum(len(b["text"]) for b in chosen), "page_exists": bool(page),
                  "file": topic.get("file") or topic["title"], "by_line": bool(topic.get("by_line")),
                  "sections": {name: {k: v for k, v in s.items() if k != "blocks"} for name, s in summary.items()}}
        report.append(record)
        codes = style_codes(chosen, config.get("style_prefixes", []))
        record["style_codes"] = len(codes)
        write_json(folder / "topic.json", {**{k: v for k, v in topic.items() if k not in {"given", "pool"}},
                                           "blocks": [b["block_id"] for b in chosen], "sections": summary, "style_codes": codes})
        if not chosen:
            continue
        lines = [f"# {topic['title']}：原文块", "", f"共 {len(chosen)} 块，已去重，按小板块分组。每块开头是编号、来源和位置。", ""]
        for name, section in sections.items():
            if name in topic["split"]:
                lines.extend([f"## 小板块：{name}", "", f"原文很多，单独成页（主题 `{topic['split'][name]}`），这里不用写。", ""])
                continue
            if not section["chosen"]:
                continue
            lines.extend([f"## 小板块：{name}", "",
                          f"{len(section['chosen'])} 块（共 {section['available']} 块可用，来自 {section['documents']} 份资料）。", ""])
            for block in section["chosen"]:
                doc = docs[block["doc_id"]]
                mark = " ⚠" if block.get("flags") else ""
                role = "（参考整理稿，不是公司事实）" if doc.get("role") == "reference" else ""
                lines.extend([f"### [{block['block_id']}]{mark} {doc['title']}{role} · {block['group']}", "", block["text"], ""])
        write_text(folder / "input.md", "\n".join(lines))
        write_text(folder / "README.md", TEMPLATE)
    # The topics of this run; folders left from earlier runs are not asked for.
    skipped = config.get("skip_topics", {})
    write_json(build / CURATION / "topics.json", [{**r, "skipped": skipped.get(r["slug"])} for r in report])
    return {"topics": report, "folder": str(build / CURATION)}


def content_lines(page: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(page.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#") or text == STYLE_TABLE or re.fullmatch(r"\|?\s*:?-{3,}.*", text):
            continue
        if text.startswith("|") and number > 1 and re.fullmatch(r"\|?\s*:?-{3,}.*", page.splitlines()[number] if number < len(page.splitlines()) else ""):
            continue  # A table header row has no facts of its own.
        found.append((number, text))
    return found


def check(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    blocks = {b["block_id"]: b for b in load_jsonl(build / "blocks.jsonl")}
    statuses = load_json(build / "statuses.json") if (build / "statuses.json").is_file() else {}
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")} if (build / "docs.jsonl").is_file() else {}
    results = {}
    for folder in sorted((build / CURATION).glob("*")):
        page_path, topic_path = folder / "page.md", folder / "topic.json"
        if not page_path.is_file() or not topic_path.is_file():
            continue
        topic = load_json(topic_path)
        allowed = set(topic["blocks"])
        page = page_path.read_text(encoding="utf-8")
        errors = []
        for number, line in enumerate(page.splitlines(), start=1):
            if STYLE_TABLE in line and line.strip() != STYLE_TABLE:
                errors.append(f"第 {number} 行款号表占位符必须单独一行；多画册主题应明确标注款号所属资料")
        for number, line in content_lines(page):
            cited = CITATION.findall(line)
            if not cited:
                errors.append(f"第 {number} 行没有标出处")
                continue
            gone = [c for c in cited if c not in blocks or blocks[c]["status"] not in USABLE
                    or statuses.get(blocks[c]["doc_id"], "有效") not in USABLE_DOCS]
            if gone:
                errors.append(f"第 {number} 行引用的原文已不可用（资料删了、改了或这段被拦下）：{', '.join(gone)}；删掉或改写这一行")
                continue
            unknown = [c for c in cited if c not in allowed]
            if unknown:
                errors.append(f"第 {number} 行引用了不在本主题原文里的块：{', '.join(unknown)}")
                continue
            source = squeezed(" ".join(blocks[c]["text"] for c in cited))
            bare = CITATION.sub("", line)
            missing = [t for t in CHECKED_TOKEN.findall(bare) if squeezed(t) not in source]
            if missing:
                errors.append(f"第 {number} 行的 {'、'.join(missing[:5])} 在引用的原文里找不到")
            if any(blocks[c].get("flags") for c in cited) and "⚠" not in bare:
                errors.append(f"第 {number} 行引用了标 ⚠ 的原文，但自己没写 ⚠")
            for name, pattern in HARD_PATTERNS.items():
                if pattern.search(bare):
                    errors.append(f"第 {number} 行含禁止内容（{name}）")
        # A page that cites a handful of the documents it was given is a summary, not the knowledge.
        usable = {b for b in topic["blocks"] if b in blocks and blocks[b]["status"] in USABLE
                  and statuses.get(blocks[b]["doc_id"], "有效") in USABLE_DOCS}
        page_cites = set(CITATION.findall(page))
        if topic.get("by_line"):
            # The overview speaks for every product line once; the line pages hold the detail.
            given = {n for n, s in topic.get("sections", {}).items() if usable & set(s.get("blocks", []))}
            used = {n for n in given if page_cites & set(topic["sections"][n]["blocks"])}
            needed, unit, left = len(given), "条产品线", sorted(given - used)
        else:
            given = {blocks[b]["doc_id"] for b in usable}
            used = {blocks[c]["doc_id"] for c in page_cites if c in blocks} & given
            needed, unit = math.ceil(len(given) * COVERAGE), "份资料"
            left = sorted(docs.get(d, {}).get("title", d) for d in given - used)
        coverage = {"given": len(given), "cited": len(used)}
        if len(used) < needed:
            shown = "、".join(left[:12]) + (f" 等 {len(left)} 个" if len(left) > 12 else "")
            rule = "每条都要写到" if topic.get("by_line") else "至少要写到一半"
            errors.append(f"只写到 {len(used)}/{len(given)} {unit}，{rule}；没写到：{shown}")
        results[folder.name] = {"title": page_title(page) or topic["title"], "ok": not errors, "errors": errors[:30],
                                "coverage": coverage}
    write_json(build / "curation-report.json", results)
    return results


def build_pages(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = load_json(build / "published.json")
    blocks = {b["block_id"]: b for b in load_jsonl(build / "blocks.jsonl")}
    results = check(out)
    written = []
    usage: dict[str, Any] = {}
    for slug, result in results.items():
        if not result["ok"]:
            continue
        folder = build / CURATION / slug
        topic = load_json(folder / "topic.json")
        page = (folder / "page.md").read_text(encoding="utf-8")
        topic["title"] = page_title(page) or topic["title"]
        numbers: dict[str, int] = {}
        sources: dict[str, set[str]] = defaultdict(set)

        def cite(match: re.Match[str]) -> str:
            block = blocks[match.group(1)]
            doc = docs[block["doc_id"]]
            number = numbers.setdefault(match.group(1), len(numbers) + 1)
            sources[doc["doc_id"]].add(block["group"])
            return f"[[{Path(doc['file_name']).stem}#{block['group']}|〔{number}〕]]"

        body = CITATION.sub(cite, page)
        if STYLE_TABLE in body:
            rows = ["| 款号 | 出处 |", "| --- | --- |"]
            for code, ids in topic.get("style_codes", {}).items():
                links = "、".join(f"[[{Path(docs[blocks[i]['doc_id']]['file_name']).stem}#{blocks[i]['group']}|{docs[blocks[i]['doc_id']]['title']}]]"
                                 for i in ids[:3])
                rows.append(f"| {code} | {links} |")
            body = body.replace(STYLE_TABLE, "\n".join(rows) if len(rows) > 2 else "（原文里没有找到款号）")
        used = [d for d in sources if d in published]
        public = all(docs[d]["tier"] == "T1" for d in used) and not any(blocks[i].get("flags") for i in numbers)
        fields = {"doc_id": f"KNOW-{slug}", "title": topic["title"], "layer": "整理层", "tier": "整理",
                  "visibility": "public" if public else "internal", "uses": ["article", "website", "crm", "agent"] if public else ["crm", "agent"],
                  "status": "有效", "module": MODULES[topic["module"]], "source_item_id": "curation", "sources": len(used),
                  "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}
        notice = "> 整理页：由 AI 按原文归纳，每条后面的〔n〕链接到原文；⚠ 和“需核实”的内容使用前要核对。"
        source_list = ["", "## 来源", ""] + [f"- [[{Path(docs[d]['file_name']).stem}]]（{docs[d]['tier']}）" for d in used]
        split = []
        for name, child in topic.get("split", {}).items():
            child_topic = build / CURATION / child / "topic.json"
            if (build / CURATION / child / "page.md").is_file() and child_topic.is_file():
                split.append(f"- {name}：[[{safe_name(load_json(child_topic).get('file') or name)}]]")
        if split:
            # Sub-sections with too much text for this page have pages of their own.
            source_list = ["", "## 另见", "", *split] + source_list
        lines = body.splitlines()
        heading = next((i for i, line in enumerate(lines) if line.startswith("# ")), -1)
        lines.insert(heading + 1, "\n" + notice)
        text = frontmatter(fields) + "\n\n" + "\n".join(lines).rstrip() + "\n" + "\n".join(source_list) + "\n"
        # The file name keeps the 类别-名称 form whatever the heading says, so pages sort and link alike.
        target = build / KNOWLEDGE / MODULES[topic["module"]] / f"{safe_name(topic.get('file') or topic['title'])}.md"
        write_text(target, text)
        written.append(str(target.relative_to(build / "vault")))
        usage[target.stem] = {"module": MODULES[topic["module"]], "sources": used, "slug": slug, "title": topic["title"]}
    write_json(build / "curation-used.json", usage)
    return {"written": written, "failed": {k: v["errors"][:5] for k, v in results.items() if not v["ok"]}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Knowledge layer: prepare topics, check pages, build them.")
    parser.add_argument("command", choices=["prepare", "check", "build"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", help="kb-config.json")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    config = load_config(Path(args.config) if args.config else None)
    if args.command == "prepare":
        report: Any = prepare(out, config)
    elif args.command == "check":
        report = check(out)
    else:
        report = build_pages(out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.command == "check":
        return 0 if all(r["ok"] for r in report.values()) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
