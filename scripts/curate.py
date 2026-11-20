#!/usr/bin/env python3
"""The knowledge layer: pages a person can read, each line tied to its source.

prepare  gives every topic all of its own source blocks (its "home" blocks), in batches,
         plus a capped set of passages from other topics' documents to cite, with style
         codes the script found itself, and writes the writing rules for the model.
         Obvious noise and exact repeats are settled by the script first.
check    refuses any line without a citation, any number or style code the cited source
         does not contain, any line that cites a flagged passage without carrying ⚠, a
         page that cites fewer than half of the documents it was given, and — the
         coverage ledger — any home block that has no place: written, kept as a table,
         pointed to, or recorded in ledger.jsonl as a repeat or as not worth writing.
build    turns citations into links, tables into the source's own table, and writes the pages.

The model organizes and summarizes; it does not add facts. The checker is what holds it
to that, and the ledger is what shows nothing was left out.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from guard_blocks import HARD_PATTERNS
from kb_common import BUILD_DIRECTORY, MODULES, SKILL_VERSION, frontmatter, load_config, load_json, load_jsonl, run_lock, safe_name, write_json, write_text
from products import Matcher, parts_of, squeeze, usable_alias
from sections import load_sections


CURATION = "curation"
KNOWLEDGE = "vault/03_正式知识/10_客户知识"
CITATION = re.compile(r"\{\{src:([0-9a-f]{12})\}\}")
# Three ways a page gives a block its place: written ({{src}}), the source's own table ({{表}}),
# or a one-line pointer to the source ({{见原文}}).
REF = re.compile(r"\{\{(src|表|见原文):([0-9a-f]{12})\}\}")
TABLE_REF = re.compile(r"^\{\{表:([0-9a-f]{12})\}\}$")
ALIAS_LINE = re.compile(r"^别名[:：]\s*(.*)$")
STYLE_TABLE = "{{款号表}}"
# Numbers, percentages, weights, years and model codes must be found in the cited text.
CHECKED_TOKEN = re.compile(r"[A-Za-z]*\d+(?:[.,]\d+)?[A-Za-z%㎡]*(?:-[A-Za-z0-9]+)*")
MIN_CATEGORY_PAGES = 3
USABLE = {"kept", "flagged"}
USABLE_DOCS = {"有效", "部分待客户确认", "参考稿"}
# Passages from other topics' documents shown per sub-section; a topic's own passages are never capped.
SECTION_MAX = 40
LINE_MAX = 30
# A product gathers catalogs, spec sheets, manuals and website pages: more to choose from.
PRODUCT_MAX = 60
# A sub-section with this much source text becomes a page of its own.
SPLIT_AT = 120
SPLIT_MAX = 150
OUTSIDE_HITS = 2
# A page has to cite at least this share of the documents its input.md came from.
COVERAGE = 0.5
WEBSITE_PRODUCTS = "官网页面/"
WEBSITE_COMPANY = "官网页面/公司与服务"
# One batch of home blocks for the model to read at a time.
BATCH_BLOCKS = 60
BATCH_CHARS = 24000
# Longer tables are shown as header, row count and a link to the source.
TABLE_ROWS = 25
BRIEF_CHARS = 2500
BRIEF_MAX = 80
LEDGER = "ledger.jsonl"
UNSORTED = "unsorted"
AUTO = "ledger-auto.json"
# Where every home passage went, totalled for the coverage page.
LEDGER_REPORT = "curation-ledger.json"
LEDGER_ACTIONS = {"重复", "不需要写"}
UNSORTED_ACTIONS = LEDGER_ACTIONS | {"归到"}
# Short text that repeats this often across the documents is a running header, not knowledge.
NOISE_CHARS = 24
NOISE_REPEAT = 3
NOISE = re.compile(r"(?i)^(?:(?:https?://|www\.)\S+|\S+@\S+\.\S+|第?\s*\d{1,3}\s*页?|page\s*\d{1,3}|.*\blogo\b.*)$")
# A block carrying any of these says something checkable; it cannot be set aside as not worth writing.
VALUE = re.compile(
    r"(?i)\d+(?:[.,]\d+)?\s*(?:%|°\s?[cf]|℃|mm\b|cm\b|kg\b|gsm\b|mpa\b|kpa\b|bar\b|psi\b|n·?m\b|nm\b|inch|v\b|w\b|kw\b|hz\b|"
    r"m2\b|㎡|pcs\b|天|日|个月|年|小时|吨|件|台|套|米|毫米|公斤|克|千克)"
    r"|\b(?:dn|pn|nps|class|iso|api|din|ansi|asme|jis|gb|en|bs|awwa|astm|iec|ul|ce|fda|sgs|oeko|gots|bsci|wras|ped|atex|sil|ip)\s?-?\s?\d"
    r"|\b(?:19|20)\d{2}\b")
NOTICE = ("> 整理页：由 AI 按原文归纳，每条后面的〔n〕链接到原文；⚠ 和“需核实”的内容使用前要核对。"
          "未另外注明的内容都是客户资料自己的说法，不等于第三方核实。")

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
# The one-screen card every article starts from: written from what the other pages already cite.
BRIEF = {"slug": "brief", "title": "公司速查卡", "file": "公司-速查卡", "module": "company", "brief": True,
         "from": ["company", "products", "custom", "faq", "other"]}
TEMPLATE = """# 整理页写作规则

你在给企业知识库写一页给人看的整理页；写文章时也直接从这一页取资料，平时不再回原文。
只能用 input.md（和 input-02.md、input-03.md……）里的原文块，不加任何原文没有的事实。

## 一、先看要做多少，一批一批写

- input.md 开头写着：本主题有多少段原文要有去处、已经处理了多少、还剩多少、分几批。
- 一批一批读。读完一批就把内容补进 page.md（已经写好的内容保留，在对应小板块里补），再读下一批。
- input.md 末尾的“参考段”来自别的主题的资料：可以引用，不用登记去处。
- 很明显的噪音（反复出现的页眉、网址、logo 描述、很短的重复标题）和完全重复的段，脚本已经处理，不在 input 里。

## 二、每段原文都要有去处（脚本逐段核对，没有去处就不通过）

1. **写进**：写成要点，结尾标出处 `{{src:块编号}}`，一条可以标多个。
2. **原样表**：原文里的表格不要自己重抄，单独一行写 `{{表:块编号}}`，脚本把原表原样放进来；
   超过 25 行的长表会自动变成“表头 + 行数 + 原文位置”。表的上一行用一句话说明这是什么表（也标出处）。
3. **指针**：写文章很少用到的长内容（超长工程尺寸表、法兰孔位、长篇历史叙述、隐私和 Cookie 政策），
   写一句话说明这段讲什么，结尾标 `{{见原文:块编号}}`。
4. **重复 / 不需要写**：在本目录的 ledger.jsonl 里一行一条（JSON），比如
   `{"block": "块编号", "action": "重复", "same_as": "已经写进的块编号"}`
   `{"block": "块编号", "action": "不需要写", "reason": "目录页"}`
   含型号、数字、标准号、认证、年份的段**不能**标“不需要写”，要写进，或者用指针。

## 三、页面格式

1. 先写 `# 标题`，下一行写 `别名：`，用顿号分开写这一页的英文名、常见叫法和款号（只用来检索，不算事实；
   例如 `别名：PTFE lined butterfly valve、衬氟蝶阀、YFD71F`）。再写 `## 一句话说明`。
2. 然后每个小板块一个 `## 小板块名`，名字和 input.md 里“小板块：”后面的完全一样，脚本靠它核对哪一节写了；
   下面可以再分 `###` 写最小的内容单元。原文不够写的小板块就不写，不要编；缺口页会把它列出来。
3. 每条要点、每个表格行、每段话的结尾都要标出处。
4. 数字、克重、成分、年份、款号必须原样出现在你引用的原文块里；不确定就不写。
5. 引用了标 ⚠ 的原文块，这一条也要写 ⚠，并说明是什么风险。
   标“参考整理稿”的块是从别家公开资料整理的参考，不是公司事实：只能写成“参考资料显示……（待公司确认）”，
   不能写成公司的参数或能力；同一点有公司自己的资料时，用公司自己的。
6. 不同原文说法不一致时，并列写出来，标“需核实”，各自标出处，不要选一个。
7. 宣传语（比如“最好的”“领先的”）不当事实写，可以写“官网自述：……”。
   **不要逐条写“画册自述”“资料中的陈述”“以上为资料口径”这类说明来源的套话**——页首已经统一说明。
   但原文里真正的适用条件和限制（比如“具体介质需结合材料和工况确认”）是事实的一部分，要保留。
8. 原文是流程或时间轴的，按原来的步骤或时间顺序写编号列表。只重新排列原文里的字，不加原文没有的步骤、说明或数字。
9. 产品类页面在合适位置单独一行写 `{{款号表}}`，脚本会填入从原文抽出的款号。一页里混着别的型号的内容时，
   写清楚是哪个型号的，不要算到本页产品头上。
10. 最后写 `## 需核实`，列出有冲突、有风险或来源单一的说法。
11. 标题写这一页讲的范围：“产品总览”要概括 input.md 里每份资料的产品线，不能只写其中一类。
    想换标题就改 `# 标题`，重跑时以它为准。
12. input.md 是挑选的原文块，不是整份资料。某个块只有标题或型号时，只说明本页引用范围；
    不得据此说原资料缺少参数、尺寸或案例。完整内容应回到来源页查看。
13. 已确认的提取错误不是原件版本冲突，不并列成两种产品参数；有同页核对补录时引用补录，
    否则暂不列该数值，并注明需回原页核对。

写好后保存为同目录的 page.md，运行 `curate.py check`，按报错改。每个准备好的主题都要写；原文实在不够写的，
在 kb-config.json 的 "skip_topics" 里写明主题编号和理由，例如 {"faq": "原文里问答太少"}，否则校验不通过。
写完一个主题可以先重跑 `curate.py prepare`：别的主题里已经有去处的段会从它们的待处理里去掉。
"""
SUMMARY_TEMPLATE = """# 整理页写作规则（汇总页）

这是一页汇总页：只做概括，细节在各产品页、各主题页里。不需要给每段原文登记去处。
只能用 input.md 里的原文块，不加任何原文没有的事实；每条结尾标出处 `{{src:块编号}}`；数字、款号必须原样出现在引用的原文里。
先写 `# 标题`，下一行可以写 `别名：`；再写 `## 一句话说明`，然后按 input.md 的小板块写 `## 小板块名`，最后写 `## 需核实`。
不要写“画册自述”这类说明来源的套话，但原文里的适用条件和限制要保留。
"""
BRIEF_TEMPLATE = """# 速查卡写作规则

这是写文章时每篇都会带上的一张卡，**正文不超过 2500 字**（不算出处标记）。只能用 input.md 里的原文块，每条结尾标出处 `{{src:块编号}}`。
写：`# 公司速查卡`，下一行 `别名：`（公司中英文名、简称），然后
`## 一句话说明`、`## 公司定位`、`## 主营产品`、`## 标准与认证`、`## 核心服务`，每节 2–5 条，只写最关键、最常用的事实。
`## 需核实` 只抄各页“需核实”里和这些事实有关的条目，不另写新的判断。
"""
UNSORTED_TEMPLATE = """# 待归类原文

这些原文段按关键词分不到任何主题。逐段决定去处，写进本目录的 ledger.jsonl，一行一条（JSON）：

- 归到某个主题：`{"block": "块编号", "action": "归到", "topic": "主题编号", "section": "小板块名"}`，
  主题编号见 curation/topics.json；下次 `curate.py prepare` 会把它放进那个主题，由那个主题写进去。
- 重复：`{"block": "块编号", "action": "重复", "same_as": "已经写进的块编号"}`
- 不需要写：`{"block": "块编号", "action": "不需要写", "reason": "理由"}`（含型号、数字、标准号、认证、年份的不能这样标）

经常分错时，改 kb-config.json 的 sections 关键词，比逐段归类省事。
"""


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def near(text: str) -> str:
    """The same words with spacing, punctuation and case ignored: a repeat across catalog and website."""
    return re.sub(r"[\W_]+", "", text.casefold())


def product_lines(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The products a person confirmed in kb-config.json "products"; solutions are not product lines."""
    return [p for p in config.get("products") or [] if p.get("name") and p.get("kind", "product") == "product"]


def place_docs(docs: dict[str, dict[str, Any]], products: list[dict[str, Any]]) -> dict[str, tuple[str, str] | None]:
    """doc_id -> (大类, 产品) by names only, never by content: the deepest folder that names a product
    first, then the file's own name; a folder named after a 大类 gives the 大类 alone."""
    entries = [{"name": p["name"], "category": p.get("category") or p["name"],
                "aliases": [a for a in [p["name"], *(p.get("aliases") or [])] if usable_alias(a)]} for p in products]
    match = Matcher(entries)
    exact = {squeeze(f): e for p, e in zip(products, entries) for f in p.get("folders") or []}
    categories = {squeeze(e["category"]): e["category"] for e in entries}
    places: dict[str, tuple[str, str] | None] = {}
    for doc_id, doc in docs.items():
        if doc.get("origin") == "points":
            places[doc_id] = None
            continue
        folders = parts_of(doc.get("package_path") or "")
        found = None
        for folder in reversed(folders):
            found = exact.get(squeeze(folder)) or match(folder)
            if found:
                break
        found = found or match(doc["title"].split("-", 1)[-1], Path(doc.get("original_name") or "").stem)
        if found:
            places[doc_id] = (found["category"], found["name"])
            continue
        category = next((categories[squeeze(f)] for f in reversed(folders) if squeeze(f) in categories), None)
        places[doc_id] = (category, "") if category else None
    return places


def topics_for(config: dict[str, Any], docs: dict[str, dict[str, Any]], statuses: dict[str, str]) -> list[dict[str, Any]]:
    topics = [dict(t) for t in config.get("topics", TOPICS)]
    # The overview has one sub-section per product line, so every catalog is read.
    topics.append({"slug": "products", "title": "产品总览", "file": "产品-总览", "module": "product", "by_line": True,
                   "modules": ["20_产品介绍"]})
    products = product_lines(config)
    if products:
        # A confirmed product list decides: one page per 大类, one section per product, whatever
        # document a passage came from — catalog in either language, spec sheet, manual, website page.
        slugs = {t["slug"] for t in topics}
        for category in dict.fromkeys(p.get("category") or p["name"] for p in products):
            slug = f"cat-{safe_name(category)}"
            if slug not in slugs:
                slugs.add(slug)
                topics.append({"slug": slug, "title": category, "file": f"产品大类-{category}", "module": "product",
                               "by_product": True, "category": category,
                               "order": [p["name"] for p in products if (p.get("category") or p["name"]) == category]})
        topics.append({"slug": "cat-other", "title": "其他产品资料", "file": "产品大类-其他产品资料", "module": "product",
                       "by_product": True, "category": None})
        topics.append(dict(BRIEF))
        return topics
    # Product pages follow the categories the website and the catalogs actually use.
    counts: dict[str, int] = defaultdict(int)
    for d in docs.values():
        if d.get("folder", "").startswith("官网页面/") and d.get("module", MODULES["product"]) == MODULES["product"]:
            counts[d["folder"].split("/", 1)[1]] += 1
    categories = sorted(c for c, n in counts.items() if n >= MIN_CATEGORY_PAGES and c != "公司与服务")
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
    # Product documents no series or category page reads (spec sheets, manuals, product notes)
    # still need a page of their own: one section per document.
    if "cat-other" not in slugs:
        covered = {d for t in topics for d in t.get("doc_ids", [])}
        folders = {f for t in topics for f in t.get("folders", [])}
        loose = sorted(d["doc_id"] for d in docs.values()
                       if d.get("module") == MODULES["product"] and d["doc_id"] not in covered
                       and d.get("folder") not in folders and statuses.get(d["doc_id"]) in USABLE_DOCS
                       and not d.get("folder", "").startswith(WEBSITE_PRODUCTS) and d.get("origin") != "points")
        if loose:
            topics.append({"slug": "cat-other", "title": "其他产品资料", "file": "产品大类-其他产品资料", "module": "product",
                           "by_doc": True, "doc_ids": loose, "gate": r"(?!)"})
    topics.append(dict(BRIEF))
    return topics


def line_name(doc: dict[str, Any]) -> str:
    place = doc.get("_product")
    if place and place[0]:
        return place[0]
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


def is_summary(topic: dict[str, Any]) -> bool:
    """Pages that only sum up: their passages are not theirs to account for."""
    return bool(topic.get("by_line") or topic.get("brief"))


def in_scope(doc: dict[str, Any], topic: dict[str, Any]) -> bool:
    """The topic reads this document as its own, not as a passing mention."""
    if topic.get("by_product"):
        if doc["module"] == MODULES["company"]:
            return False
        place = doc.get("_product")
        if topic.get("category") is None:
            return not place and doc["module"] == MODULES["product"]
        return bool(place) and place[0] == topic["category"]
    return doc.get("folder") in topic.get("folders", []) or doc["module"] in topic.get("modules", []) \
        or doc["doc_id"] in topic.get("doc_ids", [])


def assign(block: dict[str, Any], doc: dict[str, Any], topic: dict[str, Any], sections: list[dict[str, Any]]) -> str | None:
    """The sub-section a passage belongs to in this topic, or None when it does not belong here."""
    scope = in_scope(doc, topic)
    if topic.get("by_product"):
        # The product list decides; company and solution documents stay on their own pages.
        if not scope:
            return None
        place = doc.get("_product")
        if topic.get("category") is None:
            return line_name(doc)
        return place[1] or f"其他{topic['category']}资料"
    if topic.get("by_line"):
        return line_name(doc) if scope else None
    if topic.get("by_doc"):
        return doc["title"] if scope else None
    if not scope and doc.get("folder", "").startswith(WEBSITE_PRODUCTS) and doc["folder"] != WEBSITE_COMPANY:
        # A product page belongs to its own category page; its titles ("Factory Direct ... OEM")
        # would otherwise fill every other page with the same listings.
        return None
    folded = block["text"].casefold()
    scores = [(len(set(s["pattern"].findall(folded))) if s.get("pattern") else 0, -i, s["name"]) for i, s in enumerate(sections)]
    best = max(scores, default=(0, 0, None))
    # A passage from another module's documents needs a clearer signal than one from the module's own.
    if best[0] >= (1 if scope else topic.get("outside_hits", OUTSIDE_HITS)):
        if not scope and topic.get("gate") and not re.search(topic["gate"], block["text"]):
            return None
        return best[2]
    # A passage of the module's own documents that names no sub-section is about the module itself.
    return sections[0]["name"] if scope and sections else None


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


def usable_block(block: dict[str, Any], docs: dict[str, dict[str, Any]], statuses: dict[str, str]) -> bool:
    return (block["doc_id"] in docs and not block.get("figure_description") and block["status"] in USABLE
            and statuses.get(block["doc_id"]) in USABLE_DOCS)


def auto_settled(blocks: list[dict[str, Any]]) -> tuple[set[str], dict[str, str]]:
    """(noise, repeat -> its first copy): what the script settles before the model reads anything.

    Noise is only what is obviously not knowledge: no letter or digit at all, or short text without
    a number or standard that repeats across documents (running headers) or is a bare address or
    logo description. Short items that occur once ("- PTFE" in a list of seat materials) still go
    to the model."""
    counts = Counter(near(b["text"]) for b in blocks)
    noise: set[str] = set()
    repeats: dict[str, str] = {}
    first: dict[str, str] = {}
    for block in blocks:
        text, key = block["text"].strip(), near(block["text"])
        short = len(squeezed(text)) < NOISE_CHARS and not VALUE.search(text)
        if not key or (short and (counts[key] >= NOISE_REPEAT or NOISE.match(text))):
            noise.add(block["block_id"])
        elif key in first:
            repeats[block["block_id"]] = first[key]
        else:
            first[key] = block["block_id"]
    return noise, repeats


def refs_of(page: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = defaultdict(set)
    for kind, block_id in REF.findall(page):
        found[block_id].add(kind)
    return found


def read_ledger(folder: Path) -> list[dict[str, Any]]:
    """The model's entries; a line that is not JSON is reported by check, not lost."""
    path = folder / LEDGER
    if not path.is_file():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = {"_bad": line}
        entries.append({**(entry if isinstance(entry, dict) else {"_bad": line}), "_line": number})
    return entries


def plan(topic: dict[str, Any], blocks: list[dict[str, Any]], docs: dict[str, dict[str, Any]], statuses: dict[str, str],
         table: dict[str, list[dict[str, Any]]], keep: set[str] = frozenset(), homes: dict[str, str] | None = None,
         skip: set[str] = frozenset(), moves: dict[str, tuple[str, str]] | None = None,
         noise: set[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """Sub-section -> what is available and what goes to the writer.

    A passage of the topic's own documents is a home passage: it always goes to the writer, and
    the ledger asks where it went. Passages from other documents are a capped reference; so are
    repeats (`skip`), which are settled by their first copy but may still be cited here. Noise is
    not shown at all. Passages the written page already cites always stay, so a rerun cannot pull
    them out from under it."""
    sections = topic_sections(topic, table)
    names = list(topic.get("order") or []) or [s["name"] for s in sections]
    summary = is_summary(topic)
    homes = {} if homes is None else homes
    moves = moves or {}
    available: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    own: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cited: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unique: set[str] = set()
    for block in blocks:
        doc = docs.get(block["doc_id"])
        if not doc or block.get("figure_description") or block["status"] not in USABLE or statuses.get(block["doc_id"]) not in USABLE_DOCS:
            continue
        moved = moves.get(block["block_id"])
        if moved and moved[0] == topic["slug"]:
            name, scope = (moved[1] if moved[1] else (names[0] if names else line_name(doc))), True
        else:
            name = assign(block, doc, topic, sections)
            scope = name is not None and in_scope(doc, topic)
        home = scope and not summary and block["block_id"] not in skip
        if home and name and homes.setdefault(block["block_id"], topic["slug"]) != topic["slug"]:
            home = False  # An earlier topic reads this document as its own.
        if block["block_id"] in keep:
            cited[name or (names[0] if names else line_name(doc))].append(block)
            continue
        # Repeated text counts once, so the numbers on the gap page mean distinct passages.
        key = squeezed(block["text"])
        # A topic's own short items count; very short passages from elsewhere are not worth a reference slot.
        if name and key not in unique and (len(key) >= 6 or (home and near(key))):
            unique.add(key)
            if home:
                own[name].append(block)
            elif block["block_id"] not in noise:
                available[name][block["doc_id"]].append(block)
    limit = topic.get("max") or (LINE_MAX if topic.get("by_line") else PRODUCT_MAX if topic.get("by_product") else SECTION_MAX)
    position = {b["block_id"]: i for i, b in enumerate(blocks)}
    seen = {squeezed(b["text"]) for group in [*cited.values(), *own.values()] for b in group}
    result: dict[str, dict[str, Any]] = {}
    for name in [*names, *sorted((set(available) | set(cited) | set(own)) - set(names))]:
        pool = available.get(name, {})
        mine = own.get(name, [])
        taken = cited.get(name, []) + mine
        chosen = taken + share(pool, docs, max(0, limit - (len(cited.get(name, [])) if summary else 0)), seen)
        documents = set(pool) | {b["doc_id"] for b in taken}
        result[name] = {"chosen": sorted(chosen, key=lambda b: position[b["block_id"]]),
                        "available": sum(len(v) for v in pool.values()) + len(taken),
                        "documents": len(documents), "pool": pool, "own": [b["block_id"] for b in mine]}
    return result


def page_title(page: str) -> str | None:
    match = re.search(r"(?m)^# (.+)$", page)
    return match.group(1).strip() if match else None


def page_sections(page: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"(?m)^#{2,3} (.+)$", page)}


def page_aliases(page: str) -> list[str]:
    found: list[str] = []
    for line in page.splitlines():
        match = ALIAS_LINE.match(line.strip())
        if match:
            found.extend(a.strip() for a in re.split(r"[、,，;；/|]", match.group(1)) if a.strip())
    return list(dict.fromkeys(found))


def style_codes(blocks: list[dict[str, Any]], prefixes: list[str]) -> dict[str, list[str]]:
    if not prefixes:
        return {}
    pattern = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")[\s-]?[A-Z]?-?\d{2,4}[A-Z]{0,4}(?:-\d+)?\b")
    found: dict[str, list[str]] = defaultdict(list)
    for block in blocks:
        if block.get("figure_description"):
            continue
        for match in pattern.finditer(block["text"]):
            code = re.sub(r"\s+", "-", match.group(0).upper())
            if block["block_id"] not in found[code]:
                found[code].append(block["block_id"])
    return dict(sorted(found.items()))


def tables_in(text: str) -> list[str]:
    """The tables a block carries: HTML tables whole, Markdown tables as their run of | lines."""
    found: list[tuple[int, str]] = [(m.start(), m.group(0)) for m in re.finditer(r"(?is)<table\b.*?</table>", text)]
    lines, start, run = text.splitlines(keepends=True), 0, []
    offset = 0
    for line in [*lines, ""]:
        if line.lstrip().startswith("|"):
            if not run:
                start = offset
            run.append(line)
        else:
            if len(run) >= 2 and any(re.match(r"^\s*\|?\s*:?-{3,}", r) for r in run):
                found.append((start, "".join(run).rstrip("\n")))
            run = []
        offset += len(line)
    return [table for _, table in sorted(found)]


def table_shape(table: str) -> tuple[int, list[str]]:
    """(data rows, column headings) of one table."""
    if table.lstrip().startswith("<"):
        rows = re.findall(r"(?is)<tr\b.*?</tr>", table)
        head = [re.sub(r"(?s)<[^>]+>", "", c).strip() for c in re.findall(r"(?is)<t[hd]\b[^>]*>(.*?)</t[hd]>", rows[0])] if rows else []
        return max(0, len(rows) - 1), head
    lines = [line for line in table.splitlines() if line.strip()]
    head = [c.strip() for c in lines[0].strip().strip("|").split("|")] if lines else []
    return max(0, len(lines) - 2), head


def render_table(text: str, link: str, flagged: bool = False) -> str:
    """The source's own tables; a long one becomes its header, row count and a link."""
    mark = "⚠ " if flagged else ""
    parts = []
    for table in tables_in(text):
        rows, head = table_shape(table)
        if rows > TABLE_ROWS:
            columns = " / ".join(h for h in head if h)[:200]
            parts.append(f"> {mark}表格较长（{rows} 行{'；列：' + columns if columns else ''}），完整表见原文 {link}")
        else:
            parts.append(f"{table.strip()}\n\n{mark}表格出处：{link}")
    return "\n\n".join(parts)


def write_inputs(folder: Path, topic: dict[str, Any], sections: dict[str, dict[str, Any]], docs: dict[str, dict[str, Any]],
                 home: list[str], todo: set[str]) -> int:
    """input.md (and input-02.md …): the topic's unplaced home passages in batches, then the reference.
    Returns the number of batch files."""
    for old in folder.glob("input-*.md"):
        old.unlink()

    def entry(block: dict[str, Any]) -> list[str]:
        doc = docs[block["doc_id"]]
        mark = " ⚠" if block.get("flags") else ""
        role = "（参考整理稿，不是公司事实）" if doc.get("role") == "reference" else ""
        return [f"### [{block['block_id']}]{mark} {doc['title']}{role} · {block['group']}", "", block["text"], ""]

    homes = set(home)
    if is_summary(topic):
        chosen = sum(len(s["chosen"]) for s in sections.values())
        lines = [f"# {topic['title']}：原文块", "", f"共 {chosen} 块，已去重，按小板块分组。每块开头是编号、来源和位置。", ""]
        for name, section in sections.items():
            if not section["chosen"]:
                continue
            lines.extend([f"## 小板块：{name}", "", f"{len(section['chosen'])} 块（共 {section['available']} 块可用，来自 {section['documents']} 份资料）。", ""])
            for block in section["chosen"]:
                lines.extend(entry(block))
        write_text(folder / "input.md", "\n".join(lines))
        return 1
    batches: list[list[tuple[str, dict[str, Any]]]] = [[]]
    size = 0
    for name, section in sections.items():
        if name in topic.get("split", {}):
            continue
        for block in section["chosen"]:
            if block["block_id"] not in todo:
                continue
            if batches[-1] and (len(batches[-1]) >= BATCH_BLOCKS or size + len(block["text"]) > BATCH_CHARS):
                batches.append([])
                size = 0
            batches[-1].append((name, block))
            size += len(block["text"])
    reference = [(name, b) for name, s in sections.items() if name not in topic.get("split", {})
                 for b in s["chosen"] if b["block_id"] not in homes]
    count = len(batches) if batches[0] else 0
    files = ["input.md", *(f"input-{i:02d}.md" for i in range(2, count + 1))]
    done = len(homes) - len(todo)
    summary = [f"本主题要有去处的原文 {len(homes)} 段：已处理 {done} 段，还剩 {len(todo)} 段"
               + (f"，分 {count} 批：{'、'.join(files)}。" if count else "。没有待处理的段。"),
               f"参考段 {len(reference)} 段（来自别的主题的资料，可以引用，不用登记去处）在 input.md 末尾。"]
    for name, child in topic.get("split", {}).items():
        summary.append(f"小板块“{name}”原文很多，单独成页（主题 `{child}`），这里不用写。")
    for index, batch in enumerate(batches if count else [[]], start=1):
        lines = [f"# {topic['title']}：原文块（第 {index} 批，共 {max(count, 1)} 批）", "", *summary, ""]
        current = None
        for name, block in batch:
            if name != current:
                lines.extend([f"## 小板块：{name}", ""])
                current = name
            lines.extend(entry(block))
        if index == 1 and reference:
            lines.extend(["## 参考段（可以引用，不用登记去处）", ""])
            current = None
            for name, block in reference:
                if name != current:
                    lines.extend([f"### 小板块：{name}", ""])
                    current = name
                lines.extend(entry(block))
        write_text(folder / files[index - 1] if count else folder / "input.md", "\n".join(lines))
    return count


def prepare(out: Path, config: dict[str, Any]) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    statuses = load_json(build / "statuses.json")
    blocks = load_jsonl(build / "blocks.jsonl")
    table = load_sections(config)
    report = []
    for doc_id, place in place_docs(docs, product_lines(config)).items():
        docs[doc_id]["_product"] = place
    queue = topics_for(config, docs, statuses)
    usable = [b for b in blocks if usable_block(b, docs, statuses)]
    noise, repeats = auto_settled(usable)
    write_json(build / CURATION / AUTO, {"noise": sorted(noise), "repeats": dict(sorted(repeats.items()))})
    # Where each block already went: cited by some page, or settled in some ledger.
    placed: set[str] = set()
    for page_path in (build / CURATION).glob("*/page.md"):
        placed |= set(refs_of(page_path.read_text(encoding="utf-8")))
    for folder in (build / CURATION).glob("*"):
        placed |= {e["block"] for e in read_ledger(folder) if e.get("block") and e.get("action") in LEDGER_ACTIONS}
    moves = {e["block"]: (e.get("topic"), e.get("section") or "") for e in read_ledger(build / CURATION / UNSORTED)
             if e.get("block") and e.get("action") == "归到"}
    skip = noise | set(repeats)
    homes: dict[str, str] = {}
    by_id = {b["block_id"]: b for b in blocks}
    position = {b["block_id"]: i for i, b in enumerate(blocks)}
    while queue:
        topic = queue.pop(0)
        folder = build / CURATION / topic["slug"]
        page = (folder / "page.md").read_text(encoding="utf-8") if (folder / "page.md").is_file() else ""
        keep = set(refs_of(page))
        if topic.get("brief"):
            # Only what the other pages already wrote: the card condenses the knowledge layer.
            cited_ids: list[str] = []
            for slug in topic.get("from", []):
                other = build / CURATION / slug / "page.md"
                if other.is_file():
                    cited_ids.extend(CITATION.findall(other.read_text(encoding="utf-8")))
            ids = [i for i in dict.fromkeys([*keep, *cited_ids]) if i in by_id and usable_block(by_id[i], docs, statuses)]
            chosen_brief = sorted((by_id[i] for i in ids[:BRIEF_MAX] + [i for i in keep if i in ids[BRIEF_MAX:]]),
                                  key=lambda b: position[b["block_id"]])
            sections = {"速查": {"chosen": chosen_brief, "available": len(ids), "documents": len({by_id[i]["doc_id"] for i in ids}),
                                 "pool": {}, "own": []}} if chosen_brief else {}
        elif topic.get("given"):
            # A sub-section split off its parent arrives with its passages already found.
            pool, own_ids = topic.pop("given"), set(topic.pop("given_home", []))
            cited = [b for group in pool.values() for b in group if b["block_id"] in keep]
            mine = [b for group in pool.values() for b in group if b["block_id"] in own_ids and b["block_id"] not in keep]
            rest = {d: [b for b in group if b["block_id"] not in keep and b["block_id"] not in own_ids] for d, group in pool.items()}
            taken = cited + mine
            chosen = taken + share(rest, docs, max(0, SPLIT_MAX - len(taken)), {squeezed(b["text"]) for b in taken})
            sections = {topic["title"]: {"chosen": sorted(chosen, key=lambda b: position[b["block_id"]]),
                                         "available": topic.pop("available"), "documents": topic.pop("documents"),
                                         "pool": rest, "own": [b["block_id"] for b in mine]}}
        else:
            sections = plan(topic, blocks, docs, statuses, table, keep, homes, skip, moves, noise)
        topic["split"] = {}
        for name, section in list(sections.items()):
            # A 大类 page keeps its products as sections: one page per 大类 is the point of it.
            if (not topic.get("parent") and not topic.get("by_line") and not topic.get("by_product") and not topic.get("brief")
                    and not topic.get("by_doc") and section["available"] > SPLIT_AT):
                slug = f"{topic['slug']}-{safe_name(name)}"
                topic["split"][name] = slug
                parent_file = topic.get("file") or topic["title"]
                # Existing citations must stay available on both sides of a split.
                # plan() separates cited passages from pool; passing pool alone made
                # a parent's cited evidence disappear from the child's source input.
                split_pool = {doc_id: list(group) for doc_id, group in section["pool"].items()}
                for block in section["chosen"]:
                    group = split_pool.setdefault(block["doc_id"], [])
                    if not any(b["block_id"] == block["block_id"] for b in group):
                        group.append(block)
                moved_home = [b["block_id"] for b in section["chosen"] if homes.get(b["block_id"]) == topic["slug"]]
                for block_id in moved_home:
                    homes[block_id] = slug  # The child page now answers for these passages.
                queue.append({"slug": slug, "title": name, "file": f"{parent_file}-{name}", "module": topic["module"], "parent": topic["slug"],
                              "given": split_pool, "given_home": moved_home, "available": section["available"],
                              "documents": section["documents"]})
                retained = set(refs_of(page))
                section["chosen"] = [b for b in section["chosen"] if b["block_id"] in retained]
        chosen = [b for s in sections.values() for b in s["chosen"]]
        # The page's own heading wins: people rename topics by editing it.
        topic["title"] = page_title(page) or topic["title"]
        home = [b["block_id"] for b in chosen if homes.get(b["block_id"]) == topic["slug"]]
        for block_id in [i for i, slug in homes.items() if slug == topic["slug"] and i in keep and i not in home]:
            home.append(block_id)  # Cited passages kept off the input still belong here.
        todo = {i for i in home if i not in placed}
        summary = {name: {"available": s["available"], "documents": s["documents"], "blocks": [b["block_id"] for b in s["chosen"]]}
                   for name, s in sections.items()}
        record = {"slug": topic["slug"], "topic": topic["title"], "module": topic["module"], "parent": topic.get("parent"),
                  "split": topic["split"], "blocks": len(chosen), "documents": len({b["doc_id"] for b in chosen}),
                  "characters": sum(len(b["text"]) for b in chosen), "page_exists": bool(page),
                  "file": topic.get("file") or topic["title"], "by_line": bool(topic.get("by_line")),
                  "by_doc": bool(topic.get("by_doc")), "brief": bool(topic.get("brief")),
                  "summary": is_summary(topic), "home": len(home), "todo": len(todo),
                  "sections": {name: {k: v for k, v in s.items() if k != "blocks"} for name, s in summary.items()}}
        report.append(record)
        codes = style_codes(chosen, config.get("style_prefixes", []))
        record["style_codes"] = len(codes)
        write_json(folder / "topic.json", {**{k: v for k, v in topic.items() if k not in {"given", "given_home", "pool"}},
                                           "blocks": [b["block_id"] for b in chosen], "home": home, "todo": sorted(todo),
                                           "summary": is_summary(topic), "sections": summary, "style_codes": codes})
        if not chosen:
            continue
        record["batches"] = write_inputs(folder, topic, sections, docs, home, todo)
        write_text(folder / "README.md", BRIEF_TEMPLATE if topic.get("brief") else SUMMARY_TEMPLATE if is_summary(topic) else TEMPLATE)
    # Passages no topic reads as its own: the model decides where each goes.
    loose = [b for b in usable if b["block_id"] not in homes and b["block_id"] not in skip]
    unsorted = build / CURATION / UNSORTED
    if loose or unsorted.is_dir():
        todo_loose = [b for b in loose if b["block_id"] not in placed]
        write_json(unsorted / "topic.json", {"slug": UNSORTED, "title": "待归类原文", "home": [b["block_id"] for b in loose],
                                             "todo": [b["block_id"] for b in todo_loose],
                                             "topics": [r["slug"] for r in report if not r["summary"]]})
        lines = ["# 待归类原文", "", f"共 {len(loose)} 段分不到任何主题，还剩 {len(todo_loose)} 段没决定去处。规则见同目录 README.md。", "",
                 "可以归到的主题：" + "、".join(f"`{r['slug']}`（{r['topic']}）" for r in report if not r["summary"]), ""]
        for block in todo_loose:
            doc = docs[block["doc_id"]]
            lines.extend([f"### [{block['block_id']}] {doc['title']} · {block['group']}", "", block["text"], ""])
        write_text(unsorted / "input.md", "\n".join(lines))
        write_text(unsorted / "README.md", UNSORTED_TEMPLATE)
    # The topics of this run; folders left from earlier runs are not asked for.
    skipped = config.get("skip_topics", {})
    write_json(build / CURATION / "topics.json", [{**r, "skipped": skipped.get(r["slug"])} for r in report])
    return {"topics": report, "unsorted": len(loose), "auto": {"noise": len(noise), "repeats": len(repeats)},
            "folder": str(build / CURATION)}


def content_lines(page: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(page.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#") or text == STYLE_TABLE or ALIAS_LINE.match(text) or re.fullmatch(r"\|?\s*:?-{3,}.*", text):
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
    config = load_json(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    curation_setting = config.get("curation") if isinstance(config.get("curation"), dict) else {}
    strict = curation_setting.get("ledger", "required") != "warn"
    skipped = config.get("skip_topics", {}) if isinstance(config.get("skip_topics"), dict) else {}
    auto = load_json(build / CURATION / AUTO) if (build / CURATION / AUTO).is_file() else {"noise": [], "repeats": {}}
    results: dict[str, Any] = {}
    # Only this run's topics: a page drafted for a topic that no longer exists (a catalog page
    # before the product list decided the pages) is not built again.
    listed = build / CURATION / "topics.json"
    current = {t["slug"] for t in load_json(listed)} if listed.is_file() else None
    pages: dict[str, tuple[dict[str, Any], str]] = {}
    for folder in sorted((build / CURATION).glob("*")):
        page_path, topic_path = folder / "page.md", folder / "topic.json"
        if not page_path.is_file() or not topic_path.is_file() or (current is not None and folder.name not in current):
            continue
        pages[folder.name] = (load_json(topic_path), page_path.read_text(encoding="utf-8"))
    usable = lambda b: b in blocks and blocks[b]["status"] in USABLE and statuses.get(blocks[b]["doc_id"], "有效") in USABLE_DOCS
    # Where every block went, across all pages: a repeat may point at another page's passage.
    placed: dict[str, set[str]] = defaultdict(set)
    for slug, (_, page) in pages.items():
        for block_id, kinds in refs_of(page).items():
            placed[block_id] |= kinds
    settled: dict[str, str] = {}
    ledger_errors: dict[str, list[str]] = defaultdict(list)
    ledger_folders = [*pages, UNSORTED]
    for slug in ledger_folders:
        folder = build / CURATION / slug
        topic = pages[slug][0] if slug in pages else (load_json(folder / "topic.json") if (folder / "topic.json").is_file() else {})
        own = set(topic.get("home", [])) | set(topic.get("blocks", []))
        actions = UNSORTED_ACTIONS if slug == UNSORTED else LEDGER_ACTIONS
        codes = list(topic.get("style_codes", {}))
        for entry in read_ledger(folder):
            where = f"ledger.jsonl 第 {entry['_line']} 行"
            block_id, action = entry.get("block"), entry.get("action")
            if "_bad" in entry:
                ledger_errors[slug].append(f"{where}不是一条 JSON")
                continue
            if block_id not in own:
                ledger_errors[slug].append(f"{where}的段 {block_id} 不是本主题的原文")
                continue
            if action not in actions:
                ledger_errors[slug].append(f"{where}的 action 只能是 {'、'.join(sorted(actions))}")
                continue
            text = blocks[block_id]["text"] if block_id in blocks else ""
            if action == "重复" and entry.get("same_as") not in placed:
                ledger_errors[slug].append(f"{where}：same_as 指的段 {entry.get('same_as')} 没有被任何整理页引用")
                continue
            if action == "不需要写":
                if not str(entry.get("reason") or "").strip():
                    ledger_errors[slug].append(f"{where}：不需要写要写明理由")
                    continue
                if VALUE.search(text) or any(code in text.upper() for code in codes):
                    ledger_errors[slug].append(f"{where}：段 {block_id} 含型号、数字、标准号或年份，不能标不需要写，要写进或用 {{{{见原文:{block_id}}}}}")
                    continue
            if action == "归到" and entry.get("topic") not in (current or set(pages)):
                ledger_errors[slug].append(f"{where}：没有主题 {entry.get('topic')}")
                continue
            settled.setdefault(block_id, action)
    # A passage moved to a topic by 归到 still has to be written there; it only leaves the unsorted list.
    handled = set(placed) | {b for b, action in settled.items() if action != "归到"}
    for slug, (topic, page) in pages.items():
        allowed = set(topic["blocks"])
        errors: list[str] = []
        warnings: list[str] = []
        lines = page.splitlines()
        for number, line in enumerate(lines, start=1):
            if STYLE_TABLE in line and line.strip() != STYLE_TABLE:
                errors.append(f"第 {number} 行款号表占位符必须单独一行；多画册主题应明确标注款号所属资料")
            if "{{表:" in line and not TABLE_REF.match(line.strip()):
                errors.append(f"第 {number} 行表格占位符 {{{{表:块编号}}}} 必须单独一行")
        for number, line in content_lines(page):
            cited = [block_id for _, block_id in REF.findall(line)]
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
            if any(blocks[c].get("figure_description") for c in cited):
                errors.append(f"第 {number} 行引用了平台生成的图形描述，不能作为原文事实")
                continue
            table = TABLE_REF.match(line)
            if table and not tables_in(blocks[table.group(1)]["text"]):
                errors.append(f"第 {number} 行的 {table.group(1)} 里没有表格；不是表格的原文用要点写")
                continue
            source = squeezed(" ".join(blocks[c]["text"] for c in cited))
            bare = REF.sub("", line)
            missing = [t for t in CHECKED_TOKEN.findall(bare) if squeezed(t) not in source]
            if missing:
                errors.append(f"第 {number} 行的 {'、'.join(missing[:5])} 在引用的原文里找不到")
            # A table line is the placeholder alone; the built page marks a flagged table itself.
            if not table and any(blocks[c].get("flags") for c in cited) and "⚠" not in bare:
                errors.append(f"第 {number} 行引用了标 ⚠ 的原文，但自己没写 ⚠")
            for name, pattern in HARD_PATTERNS.items():
                if pattern.search(bare):
                    errors.append(f"第 {number} 行含禁止内容（{name}）")
        if topic.get("brief"):
            body = REF.sub("", "\n".join(text for _, text in content_lines(page)))
            if len(body) > BRIEF_CHARS:
                errors.append(f"速查卡正文 {len(body)} 字，超过 {BRIEF_CHARS} 字；只留最关键的事实")
        # A page that cites a handful of the documents it was given is a summary, not the knowledge.
        usable_given = {b for b in topic["blocks"] if usable(b)}
        page_cites = set(refs_of(page))
        if topic.get("brief"):
            given, used, needed, unit, left = set(), set(), 0, "份资料", []
        elif topic.get("by_line"):
            # The overview speaks for every product line once; the line pages hold the detail.
            given = {n for n, s in topic.get("sections", {}).items() if usable_given & set(s.get("blocks", []))}
            used = {n for n in given if page_cites & set(topic["sections"][n]["blocks"])}
            needed, unit, left = len(given), "条产品线", sorted(given - used)
        else:
            given = {blocks[b]["doc_id"] for b in usable_given}
            used = {blocks[c]["doc_id"] for c in page_cites if c in blocks} & given
            needed, unit = math.ceil(len(given) * COVERAGE), "份资料"
            left = sorted(docs.get(d, {}).get("title", d) for d in given - used)
        coverage = {"given": len(given), "cited": len(used)}
        if len(used) < needed:
            shown = "、".join(left[:12]) + (f" 等 {len(left)} 个" if len(left) > 12 else "")
            rule = "每条都要写到" if topic.get("by_line") else "至少要写到一半"
            errors.append(f"只写到 {len(used)}/{len(given)} {unit}，{rule}；没写到：{shown}")
        errors.extend(ledger_errors.get(slug, []))
        # The coverage ledger: every home passage has a place somewhere.
        open_ids = [b for b in topic.get("home", []) if usable(b) and b not in handled]
        if open_ids and not topic.get("summary") and slug not in skipped:
            shown = "；".join(f"{b}（{re.sub(chr(10), ' ', blocks[b]['text'])[:24]}…）" for b in open_ids[:5])
            message = (f"还有 {len(open_ids)} 段原文没有去处（写进、{{{{表:…}}}}、{{{{见原文:…}}}}，或在 ledger.jsonl 记重复、不需要写）："
                       f"{shown}{' 等' if len(open_ids) > 5 else ''}；见本主题 input.md")
            (errors if strict else warnings).append(message)
        results[slug] = {"title": page_title(page) or topic["title"], "ok": not errors, "errors": errors[:30],
                         "warnings": warnings, "coverage": coverage, "open": len(open_ids)}
    loose_topic = build / CURATION / UNSORTED / "topic.json"
    if loose_topic.is_file():
        loose = load_json(loose_topic)
        open_ids = [b for b in loose.get("home", []) if usable(b) and b not in handled and settled.get(b) != "归到"]
        errors = list(ledger_errors.get(UNSORTED, []))
        warnings = []
        if open_ids:
            message = f"还有 {len(open_ids)} 段待归类原文没决定去处，见 curation/unsorted/input.md"
            (errors if strict else warnings).append(message)
        if errors or warnings or loose.get("home"):
            results[UNSORTED] = {"title": "待归类原文", "ok": not errors, "errors": errors[:30], "warnings": warnings,
                                 "open": len(open_ids)}
    # Topics with no page yet still answer for their passages in the totals.
    counted = dict(pages)
    for folder in sorted((build / CURATION).glob("*")):
        if folder.name not in counted and (folder / "topic.json").is_file() and folder.name != UNSORTED \
                and (current is None or folder.name in current):
            counted[folder.name] = (load_json(folder / "topic.json"), "")
    write_json(build / LEDGER_REPORT, {"strict": strict, **ledger_counts(counted, placed, settled, auto, skipped, usable, loose_topic)})
    write_json(build / "curation-report.json", results)
    return results


def ledger_counts(pages: dict[str, tuple[dict[str, Any], str]], placed: dict[str, set[str]], settled: dict[str, str],
                  auto: dict[str, Any], skipped: dict[str, Any], usable, loose_topic: Path) -> dict[str, Any]:
    """Every home passage by where it went, per topic and in total, for the coverage page."""
    def where(block_id: str, slug: str) -> str:
        kinds = placed.get(block_id, set())
        if "src" in kinds:
            return "写进"
        if "表" in kinds:
            return "原样表"
        if "见原文" in kinds:
            return "指针"
        if block_id in settled:
            return {"重复": "重复", "不需要写": "不需要写", "归到": "已归类" if slug == UNSORTED else "未处理"}[settled[block_id]]
        return "决定不写" if slug in skipped else "未处理"

    order = ["写进", "原样表", "指针", "重复", "不需要写", "已归类", "决定不写", "未处理"]
    topics: dict[str, dict[str, int]] = {}
    total: Counter = Counter()
    for slug, (topic, _) in pages.items():
        if topic.get("summary"):
            continue
        counts = Counter(where(b, slug) for b in topic.get("home", []) if usable(b))
        if counts:
            topics[slug] = {"title": topic.get("title", slug), **{k: counts.get(k, 0) for k in order}}
            total.update(counts)
    if loose_topic.is_file():
        counts = Counter(where(b, UNSORTED) for b in load_json(loose_topic).get("home", []) if usable(b))
        if counts:
            topics[UNSORTED] = {"title": "待归类原文", **{k: counts.get(k, 0) for k in order}}
            total.update(counts)
    return {"total": {k: total.get(k, 0) for k in order}, "topics": topics,
            "auto": {"noise": len(auto.get("noise", [])), "repeats": len(auto.get("repeats", {}))}}


def build_pages(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = load_json(build / "published.json")
    blocks = {b["block_id"]: b for b in load_jsonl(build / "blocks.jsonl")}
    results = check(out)
    written = []
    usage: dict[str, Any] = {}
    for slug, result in results.items():
        if not result["ok"] or slug == UNSORTED:
            continue
        folder = build / CURATION / slug
        topic = load_json(folder / "topic.json")
        page = (folder / "page.md").read_text(encoding="utf-8")
        topic["title"] = page_title(page) or topic["title"]
        aliases = page_aliases(page)
        page = "\n".join(line for line in page.splitlines() if not ALIAS_LINE.match(line.strip()))
        numbers: dict[str, int] = {}
        sources: dict[str, set[str]] = defaultdict(set)

        def cite(match: re.Match[str]) -> str:
            kind, block_id = match.group(1), match.group(2)
            block = blocks[block_id]
            doc = docs[block["doc_id"]]
            number = numbers.setdefault(block_id, len(numbers) + 1)
            sources[doc["doc_id"]].add(block["group"])
            link = f"[[{Path(doc['file_name']).stem}#{block['group']}|〔{number}〕]]"
            if kind == "见原文":
                return f"[[{Path(doc['file_name']).stem}#{block['group']}|见原文〔{number}〕]]"
            if kind == "表":
                return render_table(block["text"], link, bool(block.get("flags")))
            return link

        body = REF.sub(cite, page)
        if STYLE_TABLE in body:
            rows = ["| 款号 | 出处 |", "| --- | --- |"]
            for code, ids in topic.get("style_codes", {}).items():
                links = "、".join(f"[[{Path(docs[blocks[i]['doc_id']]['file_name']).stem}#{blocks[i]['group']}|{docs[blocks[i]['doc_id']]['title']}]]"
                                 for i in ids[:3])
                rows.append(f"| {code} | {links} |")
            body = body.replace(STYLE_TABLE, "\n".join(rows) if len(rows) > 2 else "")
        used = [d for d in sources if d in published]
        public = all(docs[d]["tier"] == "T1" for d in used) and not any(blocks[i].get("flags") for i in numbers)
        # T1 and T2 are the tiers an article may read (see TIERS); anything else stays for CRM and agents.
        article = all(docs[d]["tier"] in {"T1", "T2"} for d in used)
        uses = ["article", "website", "crm", "agent"] if public else ["article", "crm", "agent"] if article else ["crm", "agent"]
        fields = {"doc_id": f"KNOW-{slug}", "title": topic["title"], "layer": "整理层", "tier": "整理",
                  "visibility": "public" if public else "internal", "uses": uses,
                  "status": "有效", "module": MODULES[topic["module"]], "source_item_id": "curation", "sources": len(used),
                  "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}
        if aliases:
            fields["aliases"] = aliases
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
        lines.insert(heading + 1, "\n" + NOTICE)
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
    if not run_lock(Path(args.out).resolve(), f"curate.py {args.command}"):
        return 3
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
