#!/usr/bin/env python3
"""Keep strange content out: scan every block before anything is rendered.

A hit removes the sentence it is in, or the whole row of a table; nothing is masked
inside a sentence, because a masked sentence can still identify a customer. Words only
a person can judge are grouped by term, so one decision settles every block that uses it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from kb_common import (
    BUILD_DIRECTORY,
    ROLE_ORDER,
    short_hash,
    load_config,
    load_json,
    load_jsonl,
    normalize_space,
    write_json,
    write_jsonl,
    write_text,
)


# Prices and payment terms are never kept, whoever asks. A price word only counts with an
# amount beside it: "价格有竞争力" is marketing, "单价 6.8" is a quote. MOQ and lead time are
# what companies print on their own websites, so they stay in prose; in a quotation's rows
# they were offered to one customer, and ROW_PATTERNS removes them there.
HARD_PATTERNS = {
    "price": re.compile(
        r"(?i)(?:\b(?:usd|rmb|cny|eur|gbp)\s*[:：]?\s*\d|\bus\$\s*\d|[$€£¥￥]\s*\d|"
        r"\d\s*(?:usd|rmb|eur|元/|美元)|"
        # The amount follows the word closely; "报价单-3f2a" is a file title, not a quote.
        r"(?:单价|报价(?!单)|价格|含税|unit\s*price|(?<![a-z])price)[\s:：为是约在]{0,3}(?:usd|rmb|cny|us\$|\$|¥|￥)?\s*\d)"
    ),
    # \b fails beside Chinese, so Latin terms are bounded by letters only.
    "payment_term": re.compile(r"(?i)(?<![a-z])(?:t/t|l/c|d/p|d/a)(?![a-z])|付款方式|预付款|定金|尾款|deposit\s+\d|balance\s+before"),
    "order_number": re.compile(r"(?i)\bP\.?O\.?\s*(?:#|no\.?|number)?\s*[:：]?\s*\d{3,}|\bFPO\d{3,}|订单号"),
    "bare_cost": re.compile(
        r"(?i)\d\s*(?:/\s*(?:m|米|kg|公斤|件|码|yd|yard|pcs?)\b|/\s*(?:米|公斤|件|码)|元|块|毛钱|毛\b|一米|每件|每米|一条|一件)"
    ),
}
# Salespeople write notes into product columns, so table cells get stricter rules.
ROW_PATTERNS = {
    "quantity": re.compile(
        r"(?i)\d+\s*(?:pcs?\b|sets?\b|套|箱)|"
        r"\d+\s*(?:副|颗|个|条|粒|枚|双|根|片)\s*/\s*(?:件|条|个|双|套)|"
        r"数量|外箱|内盒|/\s*ctn|\bcarton|五层|无订"
    ),
    # In a quotation these were offered to one customer.
    "quoted_terms": re.compile(r"(?i)(?<![a-z])(?:moq|fob|cif|exw|ddp)(?![a-z])|起订量?|交期|交货期|lead\s*time|价格|报价(?!单)|单价"),
    # These words are ordinary in company prose ("控制成本"), so they only apply to cells.
    "cost_word": re.compile(r"(?i)\bcost\b|目标价|成本|加工费|制版|样品费|打样费|计费|收费|运费"),
    "material_usage": re.compile(r"门幅|单耗|用料|用量|\d\s*(?:米|码|个|条)?\s*/\s*件|净\s*\d"),
    "internal_note": re.compile(
        r"客人|客户|客供|我司|自编|样衣房|不能通过|指定|按照|同款|原样|代工|投诉|返工|大货|清单$|"
        r"师傅|报给|\bQQ\b|之前|同上|不要|小单|包装|PP\s*袋|市场"
    ),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
MOBILE_CN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# The number must follow its label, or any spec on the same page would count as a phone.
LABELLED_PHONE = re.compile(r"(?i)(?:\b(?:tel|phone|mobile|whatsapp|fax)\b|电话|手机)[:：\s.]*\+?\d[\d\s().\-]{6,}")

FREE_MAIL = {
    "gmail.com", "qq.com", "163.com", "126.com", "hotmail.com", "outlook.com", "yahoo.com",
    "foxmail.com", "sina.com", "icloud.com", "live.com", "msn.com", "aol.com",
}
COMPANY_EN = re.compile(
    r"\b([A-Z][A-Za-z&.'\-]+(?:\s+[A-Z][A-Za-z&.'\-]+){0,4}\s+"
    r"(?:Ltd\.?|Limited|LLC|Inc\.?|GmbH|S\.?p\.?A\.?|Corp\.?|Co\.,?|S\.?r\.?l\.?|B\.?V\.?))"
)
COMPANY_CN = re.compile(r"([一-龥]{2,14}(?:有限公司|实业|贸易公司|商贸|洋行))")
SENDER = re.compile(r"(?im)^(?:from|发件人)[:：]\s*\"?([^<\n\"]{3,40})\"?\s*<")

# Ordinary trade vocabulary cannot name a customer on its own.
GENERIC_WORDS = set(
    """
    garments garment fabric fabrics polyester cotton fleece polar sherpa jacket jackets shirt shirts
    pants textile textiles apparel clothing fashion trading company group limited ltd co inc date
    quotation quote sales manager account prepared for your the and of to price address tel fax
    mobile email website china zhejiang shaoxing floor building road code size color style item
    quatation quoattion quoation list copy styles all new order orders cost costing sheet winter summer
    spring fall autumn version update updated final customer client sample samples program rfq factory
    single format foreign linking updating suppliers clients product products details design designs
    tech pack hoodie shirt fishing page xlsx xls csv pdf docx doc pptx ppt png jpg jpeg zip rar
    mail web http https www phone whatsapp wechat
    """.split()
)
SKIP_TERM = re.compile(r"^[\d\s.,:/\-+#()%]+$")
WORD = re.compile(r"[A-Za-z][A-Za-z'&\-]{2,}")
CN_COMPANY_TAIL = re.compile(r"(?:香港|国际|集团)?(?:有限公司|贸易公司|贸易|实业|商贸|洋行|服饰|纺织)+$")
CN_REGION_HEAD = re.compile(r"^(?:中国|香港|浙江|江苏|上海|广东|绍兴|杭州|宁波|深圳|柯桥)+")
# Audit and test reports carry ratings, logos, and other companies' names.
REPORT_PAGE = re.compile(r"(?i)test report|audit report|inspection report|certificate no|检测报告|审核报告|验厂报告|综合评级|证书编号")
QUESTION = re.compile(r"(?i)(?:[?？]\s*$|^\s*q\d*[:：.\s])")
# A label name: capitalised, no digits (style codes have them).
BRAND_LIKE = re.compile(r"[A-Z][A-Za-z&'\-]{2,}")
BRAND_ROLES = ("style", "name", "description")
SENTENCE_END = re.compile(r"[。！？；;!?]+|\.(?=\s)|\n")
# Only a real page or slide is marked whole; a Word section that mentions a report is not one.
PAGED = re.compile(r"^(?:page|slide) \d+")
VOCABULARY_DIRECTORY = Path(__file__).resolve().parents[1] / "assets"
MESSAGE_ID_DOMAIN = re.compile(r"^[0-9a-f]{8}\.[0-9a-f]{8}$")
TWO_PART_SUFFIX = {"com.cn", "com.au", "co.uk", "co.jp", "com.hk", "com.tw", "com.br", "com.mx", "co.kr"}
NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z&'\'.\-]{3,}")
SPREADSHEET_NAME = re.compile(r"(?i)\.xlsx?$")

MIN_REPEAT_PAGES = 3
REPEAT_SHARE = 0.6
GARBLED_SHARE = 0.5
SITE_MIN_PAGES = 8
SITE_COMMON_MIN = 3
SITE_COMMON_SHARE = 0.05
SITE_REPEAT_SHARE = 0.8
# Repetition alone proves nothing: catalogs repeat real specifications on every page.
# Only a repeated line that also looks like a footer counts as page furniture.
FURNITURE = re.compile(r"(?i)https?:|www\.|@|\b(?:tel|fax|whatsapp)\b|电话|^\d{1,3}$|^page\s*\d+$")


def word_pattern(term: str) -> str:
    escaped = re.escape(term)
    return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])" if term.isascii() else escaped


def usable_term(term: str, own: set[str]) -> bool:
    text = normalize_space(term)
    if len(text) < 3 or len(text) > 60 or SKIP_TERM.match(text):
        return False
    if text.isascii() and len(text) < 4:
        return False
    lowered = text.casefold()
    if any(mark in lowered or lowered in mark for mark in own):
        return False
    return not all(word in GENERIC_WORDS for word in re.findall(r"[a-z]+", lowered) or ["-"])


def registered_domain(domain: str) -> str:
    labels = domain.split(".")
    keep = 3 if ".".join(labels[-2:]) in TWO_PART_SUFFIX else 2
    return ".".join(labels[-keep:])


def terms_from_file_names(routes: dict[str, Any]) -> Counter:
    """Quotations are often named after the customer they went to."""
    found: Counter = Counter()
    for route in routes.values():
        name = route.get("display_name") or ""
        if SPREADSHEET_NAME.search(name):
            found.update(NAME_TOKEN.findall(Path(name).stem))
    return found


def terms_from_transactions(routes: dict[str, Any], text_dirs: list[Path], own_domains: set[str]) -> Counter:
    """Learn names from the emails that were excluded: they say who the customers are."""
    found: Counter = Counter()
    for item_id, route in routes.items():
        if route.get("resource_type") != "transaction":
            continue
        path = next((d / f"{item_id}.json" for d in text_dirs if (d / f"{item_id}.json").is_file()), None)
        if path is None:
            continue
        for unit in load_json(path).get("units", []):
            text = unit["text"]
            for address in {match.group(1).casefold() for match in EMAIL.finditer(text)}:
                domain = registered_domain(address)
                if not MESSAGE_ID_DOMAIN.match(address) and domain not in FREE_MAIL and domain not in own_domains:
                    found[domain.split(".")[0]] += 1
            for pattern in (COMPANY_EN, COMPANY_CN, SENDER):
                for term in {normalize_space(match.group(1)) for match in pattern.finditer(text)}:
                    found[term] += 1
            # Notes use the short name: 谷威, not 谷威贸易香港有限公司.
            for match in COMPANY_CN.finditer(text):
                short = CN_REGION_HEAD.sub("", CN_COMPANY_TAIL.sub("", match.group(1)))
                if len(short) >= 2:
                    found[short] += 1
    return found


def build_terms(
    build: Path, routes: dict[str, Any], text_dirs: list[Path], config: dict[str, Any], decisions: dict[str, Any],
    vocabulary: set[str],
) -> dict[str, list[str]]:
    own = {normalize_space(term).casefold() for term in config["own_terms"]}
    own_domains = {domain.casefold() for domain in config["own_domains"]}
    allowed = {term.casefold() for term in decisions.get("allow_terms", [])}
    hard = {term for term in config["deny_terms"]} | set(decisions.get("deny_terms", []))

    soft: Counter = Counter()
    for record in load_jsonl(build / "removed-terms.jsonl"):
        for line in record["term"].split(" / "):
            soft[normalize_space(line)] += 1
    soft.update(terms_from_transactions(routes, text_dirs, own_domains))
    soft.update(terms_from_file_names(routes))

    kept = sorted(
        term for term in soft
        if usable_term(term, own) and term.casefold() not in allowed and term not in hard
        and not (term.isascii() and all(word in vocabulary for word in term.casefold().split()))
    )
    return {"hard": sorted(hard), "soft": kept}


def compile_terms(terms: list[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    ordered = sorted(terms, key=len, reverse=True)
    return re.compile("|".join(word_pattern(term) for term in ordered), re.IGNORECASE)


def contact_hits(text: str, own_domains: set[str], own_contacts: list[str]) -> list[str]:
    hits = []
    for match in EMAIL.finditer(text):
        if match.group(1).casefold() not in own_domains:
            hits.append("email")
    remainder = text
    for contact in own_contacts:
        remainder = remainder.replace(contact, " ")
    if MOBILE_CN.search(remainder) or LABELLED_PHONE.search(remainder):
        hits.append("phone")
    return hits


def load_vocabulary(config: dict[str, Any], decisions: dict[str, Any]) -> set[str]:
    words = set(GENERIC_WORDS)
    for name in config.get("vocabularies", []):
        path = VOCABULARY_DIRECTORY / f"vocabulary-{name}.txt"
        words.update(line.strip().casefold() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    # The company's own style prefixes (ANST in ANST-001) are its words, not unknown names.
    for term in list(decisions.get("allow_terms", [])) + list(config["own_terms"]) + list(config.get("style_prefixes", [])):
        words.add(term.casefold())
        words.update(word.casefold() for word in WORD.findall(term))
    return words


def unknown_words(text: str, vocabulary: set[str]) -> set[str]:
    """Return words nobody vetted. Compounds pass only when every part is known."""
    found = set()
    for raw in WORD.findall(text):
        word = raw.strip("-&'")
        parts = [part for part in word.casefold().split("-") if part]
        if len(word) >= 2 and not all(part in vocabulary or len(part) == 1 for part in parts):
            found.add(word)
    return found


def apply_cell_decisions(block: dict[str, Any], decisions: dict[str, dict[str, Any]]) -> None:
    """Rewrite a row from its judged cells. Only deletions happen: keep, drop, or a verbatim part."""
    cells, unjudged = {}, []
    for role, value in block["cells"].items():
        kept = []
        for piece in value.split(" / "):
            decision = decisions.get(short_hash(normalize_space(piece), 16))
            if decision is None:
                kept.append(piece)
                unjudged.append(piece)
            elif decision["decision"] == "keep":
                kept.append(piece)
            elif decision["decision"] == "part":
                kept.append(decision["keep_text"])
        if kept:
            cells[role] = " / ".join(kept)
    block["cells"] = cells
    block["text"] = " | ".join(cells[role] for role in ROLE_ORDER if role in cells)
    block["unjudged_text"] = " ".join(unjudged)
    block["judged"] = not unjudged


def hide_brand_cells(block: dict[str, Any], unvetted: list[str]) -> bool:
    """In a product table a capitalised word nobody knows is usually the customer's label
    (a retailer's own label). The cell holding it is taken out; the rest of the row stays.

    Returns False when nothing worth keeping is left."""
    brands = [word for word in unvetted if BRAND_LIKE.fullmatch(word)]
    if not brands:
        return True
    pattern = re.compile("|".join(word_pattern(word) for word in brands))
    hidden = [role for role in BRAND_ROLES if role in block["cells"] and pattern.search(block["cells"][role])]
    if not hidden:
        return True
    for role in hidden:
        block["cells"].pop(role)
    block["text"] = " | ".join(block["cells"][role] for role in ROLE_ORDER if role in block["cells"])
    block["hidden_cells"] = {"roles": hidden, "terms": brands}
    return bool({"description", "fabric", "style"} & set(block["cells"]))


def hard_hits(text: str, hard_terms: re.Pattern[str] | None, own_domains: set[str], own_contacts: list[str]) -> list[str]:
    hits = [name for name, pattern in HARD_PATTERNS.items() if pattern.search(text)]
    hits.extend(contact_hits(text, own_domains, own_contacts))
    if hard_terms and hard_terms.search(text):
        hits.append("denied_term")
    return hits


def sentences(text: str) -> list[str]:
    """Pieces that join back into the text; decimals like 9.20 are not sentence ends."""
    pieces, start = [], 0
    for match in SENTENCE_END.finditer(text):
        pieces.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        pieces.append(text[start:])
    return [piece for piece in pieces if piece.strip()]


def split_on_hits(blocks: list[dict[str, Any]], hard_terms: re.Pattern[str] | None, own_domains: set[str],
                  own_contacts: list[str]) -> list[dict[str, Any]]:
    """Remove the sentence that quotes a price, not the whole paragraph around it.

    The pieces stay in order, so together they are still the source line word for word."""
    result = []
    for block in blocks:
        if block["kind"] != "text" or not hard_hits(block["text"], hard_terms, own_domains, own_contacts) \
                or hard_hits(block["group"], hard_terms, own_domains, own_contacts):
            result.append(block)
            continue
        runs: list[list[Any]] = []
        for piece in sentences(block["text"]):
            dirty = bool(hard_hits(piece, hard_terms, own_domains, own_contacts))
            if runs and runs[-1][1] == dirty:
                runs[-1][0] += piece
            else:
                runs.append([piece, dirty])
        if len(runs) < 2 or not any(dirty for _, dirty in runs) or all(dirty for _, dirty in runs):
            result.append(block)  # No clean sentence to keep, or the hit spans sentences.
            continue
        for index, (text, _) in enumerate(runs):
            result.append({**block, "block_id": short_hash(f"{block['block_id']}:{index}", 12), "text": text.strip(),
                           "part_of": block["block_id"]})
    return result


def flag_report_pages(blocks: list[dict[str, Any]]) -> int:
    """Audit and test reports name other companies and ratings: kept, but marked for review.

    A PDF page or a slide is marked as a whole; in Word and other unpaged files only the
    passage itself, or one mention of a report would mark a whole chapter."""
    hits = {(b["doc_id"], b["group"]) if PAGED.match(b["locator"]) else b["block_id"]
            for b in blocks if b["kind"] == "text" and REPORT_PAGE.search(b["text"])}
    flagged = 0
    for block in blocks:
        if ((block["doc_id"], block["group"]) in hits or block["block_id"] in hits) and block["status"] in {"kept", "flagged"}:
            block["status"] = "flagged"
            block["flags"].append({"type": "report_page", "terms": []})
            flagged += 1
    return flagged


def strip_repeated_lines(blocks: list[dict[str, Any]]) -> int:
    """Drop page furniture: a line on most pages of a document is not knowledge."""
    pages: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        if block["kind"] == "text" and block["locator"].startswith("page "):
            pages.setdefault(block["doc_id"], []).append(block)
    removed = 0
    for members in pages.values():
        # A page read in columns has several blocks; the share is of pages, not of blocks.
        lines_by_page: dict[str, set[str]] = {}
        for member in members:
            page = re.split(r" (?:col|table) ", member["locator"])[0]
            lines_by_page.setdefault(page, set()).update(normalize_space(raw) for raw in member["text"].splitlines())
        if len(lines_by_page) < MIN_REPEAT_PAGES:
            continue
        counts = Counter(line for lines in lines_by_page.values() for line in lines if line)
        repeated = {
            line for line, count in counts.items()
            if count / len(lines_by_page) >= REPEAT_SHARE and FURNITURE.search(line)
        }
        if not repeated:
            continue
        for member in members:
            lines = member["text"].splitlines()
            kept = [line for line in lines if normalize_space(line) not in repeated]
            removed += len(lines) - len(kept)
            member["text"] = "\n".join(kept).strip()
    return removed


def strip_site_furniture(blocks: list[dict[str, Any]]) -> int:
    """Menus and footers outside <nav>/<footer> repeat on nearly every page of a website."""
    pages: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        if block["doc_id"].startswith("WEB-"):
            pages.setdefault(block["doc_id"], []).append(block)
    if len(pages) < SITE_MIN_PAGES:
        return 0
    counts = Counter(
        line for members in pages.values()
        for line in {normalize_space(raw) for b in members for raw in b["text"].splitlines()} if line
    )
    repeated = {line for line, count in counts.items() if count / len(pages) >= SITE_REPEAT_SHARE}
    removed = 0
    for members in pages.values():
        for block in members:
            lines = block["text"].splitlines()
            kept = [line for line in lines if normalize_space(line) not in repeated]
            removed += len(lines) - len(kept)
            block["text"] = "\n".join(kept).strip()
    return removed


def garbled(text: str) -> bool:
    # A Markdown table's bars and rules are layout, not unreadable characters.
    text = "\n".join(re.sub(r"[|:\-]", "", line) if line.lstrip().startswith("|") else line for line in text.splitlines())
    compact = re.sub(r"\s", "", text)
    if len(compact) < 2:
        return True
    readable = len(re.findall(r"[0-9A-Za-z一-鿿]", compact))
    return readable / len(compact) < GARBLED_SHARE


def scan_block(
    block: dict[str, Any],
    hard_terms: re.Pattern[str] | None,
    soft_terms: re.Pattern[str] | None,
    own_domains: set[str],
    own_contacts: list[str],
    vocabulary: set[str],
) -> tuple[list[str], list[str], list[str]]:
    """Return (removal reasons, likely customer names, unvetted words)."""
    # Section titles are published too, so they are scanned with the text.
    text = f"{block['group']}\n{block['text']}"
    # Cells a reviewer already judged are trusted for notes and words, never for prices.
    unjudged = block.get("unjudged_text", block["text"])
    hard = hard_hits(text, hard_terms, own_domains, own_contacts)
    if block["kind"] == "row":
        style = normalize_space(block.get("cells", {}).get("style", ""))
        if style.casefold() in {"尺寸表如下", "尺码表如下", "size chart as below", "size chart as follows"}:
            hard.append("invalid_style_label")
        hard.extend(name for name, pattern in ROW_PATTERNS.items() if pattern.search(unjudged))
    customers = sorted({normalize_space(m.group(0)) for m in soft_terms.finditer(text)}) if soft_terms else []
    unvetted: list[str] = []
    if block["kind"] == "row" or block.get("ocr"):
        # Brands, celebrities, and IP turn up as words nobody has vetted yet.
        unvetted = sorted(unknown_words(unjudged, vocabulary))
    elif block["doc_id"].startswith("WEB-"):
        # Shop copy is ordinary English; a name is capitalised or has digits (2Pac).
        unvetted = sorted(w for w in unknown_words(unjudged, vocabulary) if w[:1].isupper() or any(c.isdigit() for c in w))
    return hard, customers, unvetted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan blocks and hold back anything suspicious.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--text-dir", action="append", required=True, help="Extracted text units; repeat")
    parser.add_argument("--config", help="kb-config.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    config = load_config(Path(args.config) if args.config else None)
    routes = load_json(build / "routes.json")
    decisions_path = build / "review-decisions.json"
    decisions = load_json(decisions_path) if decisions_path.is_file() else {}
    keep_blocks = set(decisions.get("keep_blocks", []))
    drop_blocks = set(decisions.get("drop_blocks", []))

    blocks = load_jsonl(build / "blocks-raw.jsonl")
    cell_decisions = {r["cell_key"]: r for r in load_jsonl(build / "cell-review" / "decisions.jsonl")}
    for block in blocks:
        if block["kind"] == "row" and cell_decisions:
            apply_cell_decisions(block, cell_decisions)
    repeated = strip_repeated_lines(blocks) + strip_site_furniture(blocks)
    vocabulary = load_vocabulary(config, decisions)
    terms = build_terms(build, routes, [Path(raw).resolve() for raw in args.text_dir], config, decisions, vocabulary)
    write_json(build / "guard-terms.json", terms)
    hard_terms = compile_terms(terms["hard"])
    soft_terms = compile_terms(terms["soft"])
    own_domains = {domain.casefold() for domain in config["own_domains"]}

    # Words in the company's own printed material count as known; website text does not,
    # because shop pages are where licensed and celebrity names appear.
    vocabulary.update(
        word.casefold() for block in blocks
        if block["kind"] == "text" and block.get("tier") == "T1" and not block.get("ocr") and not block["doc_id"].startswith("WEB-")
        for word in WORD.findall(block["text"])
    )
    # A word on many pages of the site is the site's own vocabulary; names sit on one or two.
    site_pages: dict[str, set[str]] = {}
    for block in blocks:
        if block["doc_id"].startswith("WEB-"):
            for word in WORD.findall(block["text"]):
                site_pages.setdefault(word.casefold(), set()).add(block["doc_id"])
    site_count = len({b["doc_id"] for b in blocks if b["doc_id"].startswith("WEB-")})
    vocabulary.update(word for word, pages in site_pages.items() if len(pages) >= max(SITE_COMMON_MIN, site_count * SITE_COMMON_SHARE))
    blocks = split_on_hits(blocks, hard_terms, own_domains, config["own_contacts"])
    results, queue = [], {}
    counts: Counter = Counter()

    def enqueue(term: str, kind: str, block: dict[str, Any]) -> None:
        entry = queue.setdefault((kind, term.casefold()), {"term": term, "kind": kind, "blocks": 0, "examples": []})
        entry["blocks"] += 1
        if len(entry["examples"]) < 2:
            entry["examples"].append(block["text"][:120])

    for block in blocks:
        hard, customers, unvetted = scan_block(block, hard_terms, soft_terms, own_domains, config["own_contacts"], vocabulary)
        block["hold_reasons"], block["flags"] = [], []
        trusted = block["block_id"] in keep_blocks or block.get("part_of") in keep_blocks
        if block["kind"] == "row" and not ({"description", "fabric", "style"} & set(block["cells"])):
            block["status"], block["hold_reasons"] = "dropped", ["reviewer_dropped_cells"]
        elif not block["text"] or (block["kind"] == "text" and garbled(block["text"])):
            block["status"], block["hold_reasons"] = "dropped", ["empty_or_garbled"]
        elif block["block_id"] in drop_blocks or block.get("part_of") in drop_blocks:
            block["status"], block["hold_reasons"] = "removed", ["reviewer_dropped"]
        elif hard:
            # Prices, contacts, orders, and competitors are cleaned out, as screening decided.
            block["status"], block["hold_reasons"] = "removed", hard
        elif customers and not trusted:
            block["status"], block["hold_reasons"] = "removed", [f"customer_name:{term}" for term in customers]
            for term in customers:
                enqueue(term, "customer_name", block)
        elif block["kind"] == "row" and block.get("tier") == "T3" and not trusted and not hide_brand_cells(block, unvetted):
            # Only the brand was left: the row named a customer's label and nothing else.
            block["status"], block["hold_reasons"] = "removed", ["customer_brand"]
        else:
            if block.get("hidden_cells"):
                hidden = block["hidden_cells"]["terms"]
                block["flags"].append({"type": "brand_hidden", "terms": hidden})
                for term in hidden:
                    enqueue(term, "unverified_name", block)
                unvetted = [word for word in unvetted if word not in hidden]
            if unvetted and not trusted:
                # Risky but not secret: published with a mark and listed for review.
                block["flags"].append({"type": "unverified_name", "terms": unvetted})
                for term in unvetted:
                    enqueue(term, "unverified_name", block)
            if block.get("screening_hint"):
                # Screening wanted this gone; the rules found nothing to remove, so it stays, marked.
                block["flags"].append({"type": "screening_hint", "terms": block["screening_hint"]})
            if block.get("reference"):
                # A compilation from other suppliers' listings: it can guide, not state the company's facts.
                block["flags"].append({"type": "reference_draft", "terms": []})
            if block.get("ai_read") and not trusted:
                # The model read it from a picture; nobody has compared it with the page yet.
                block["flags"].append({"type": "ai_added" if block.get("ai_checked") else "ai_read", "terms": []})
            if block.get("client_hold"):
                block["status"] = "pending_client"
            else:
                block["status"] = "flagged" if block["flags"] else "kept"
        counts[block["status"]] += 1
        for reason in block["hold_reasons"]:
            counts[reason.split(":")[0]] += 1
        results.append(block)

    counts["report_page"] += flag_report_pages(results)

    # An answer is only safe if its question was: remove them together.
    for question, answer in zip(results, results[1:]):
        removed = question["status"] == "removed" and question["kind"] == "text"
        if removed and question["doc_id"] == answer["doc_id"] and QUESTION.search(question["text"]):
            if answer["status"] in {"kept", "flagged"}:
                answer["status"] = "removed"
                answer["hold_reasons"].append("paired_answer")
                counts["paired_answer"] += 1

    write_jsonl(build / "blocks.jsonl", results)
    ordered = sorted(queue.values(), key=lambda entry: entry["blocks"], reverse=True)
    write_jsonl(build / "review-queue.jsonl", ordered)
    lines = ["# 待判断的词", "",
             "判断一次即可处理所有相关内容。普通词放行写进词表或 allow_terms；确认是客户或竞品的写进 deny_terms。", ""]
    for kind, title in (("customer_name", "## 可能是客户名（相关内容已删除，普通词请放行）"),
                        ("unverified_name", "## 没核实过的词（相关内容已入库并标 ⚠，确认是品牌、明星、IP 时登记处理）")):
        members = [entry for entry in ordered if entry["kind"] == kind]
        lines.extend(["", title, ""])
        lines.extend(f"- {entry['term']}（{entry['blocks']} 处）：{entry['examples'][0]}" for entry in members)
    write_text(build / "review-queue.md", "\n".join(lines) + "\n")
    print(
        json.dumps(
            {"counts": dict(counts), "repeated_lines_removed": repeated, "terms_to_review": len(ordered),
             "term_dictionary": {key: len(value) for key, value in terms.items()}},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
