#!/usr/bin/env python3
"""After writing: are the article's hard facts in the material pack? No model, a second or two.

It pulls the facts an article most often gets wrong — model codes, standard numbers,
certifications, sizes and ratings, numbers with units, years, percentages — and looks each one
up in the pack kb_lookup.py made for this article. What counts as such a fact comes from
assets/check-rules.json: a common layer, the project's industries (kb-config.json vocabularies,
all of them when none is named), the project's own "check" entries, and units and standard
prefixes its own knowledge pages use at least three times. What it reports:

  not in the pack          the writer may have invented it, or taken it from memory
  only in 口径避坑          the pack said not to state it as fact
  only in 原文里搜到的       taken from material nobody has curated yet; check it
  competitor names          kb-config.json deny_terms appearing in the article

A fact found elsewhere in the knowledge base (with --vault) is marked so: the pack simply did
not include it. The result is a short list of reminders for the delivery note; it does not
block delivery unless --strict is given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from html import unescape
from pathlib import Path
from typing import Any

from kb_common import write_text


ZONES = (("三、口径避坑", "需核实"), ("原文里搜到的", "未整理"))
RULES = Path(__file__).resolve().parents[1] / "assets" / "check-rules.json"
KINDS = ("units", "standards", "certificates", "ratings", "patterns", "codes")
YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# Words that follow numbers in prose and are not units, when collecting a project's own units.
NOT_UNITS = {"to", "and", "or", "of", "for", "the", "with", "at", "by", "x", "a", "an", "is", "are", "from", "per", "page",
             "pages", "series", "type", "types", "no", "nos", "pc", "th", "st", "nd", "rd", "vs", "times", "models", "items"}
HARVEST_MIN = 3
# Equivalent Chinese / English / plural units after a digit, so Chinese packs match English articles.
UNIT_ALIASES = (
    ("个月", "month"), ("months", "month"),
    ("years", "year"), ("年", "year"),
    ("days", "day"), ("天", "day"),
    ("小时", "hour"), ("hours", "hour"),
    ("分钟", "min"),
    ("毫米", "mm"), ("厘米", "cm"), ("米", "m"),
    ("千克", "kg"), ("公斤", "kg"), ("克", "g"),
    ("tons", "ton"), ("吨", "ton"),
    ("inches", "inch"), ("sets", "set"),
)
IN_PROSE = r"(?!\s+(?:the|a|an|\d+|stock|total|our|this|that|service|use|operation|general|fact|addition|practice)\b)"


def vault_body(vault: Path) -> str:
    text = []
    for root in ("03_正式知识/10_客户知识", "03_正式知识/20_外部公共知识"):
        for page in sorted((vault / root).rglob("*.md")) if (vault / root).is_dir() else []:
            text.append(re.sub(r"!?\[\[[^\]]*\]\]", " ", page.read_text(encoding="utf-8", errors="replace")))
    return "\n".join(text)


def detect_industries(body: str, table: dict[str, dict[str, Any]]) -> list[str]:
    """Pick the industry groups whose distinctive markers appear in this vault (at least 2)."""
    matched = []
    for name, group in table.items():
        hits = 0
        for rating in group.get("ratings") or []:
            if re.search(rf"(?i)\b{fragment(rating)}\s?\d", body):
                hits += 1
        for std in group.get("standards") or []:
            if re.search(rf"\b{fragment(std)}\s?-?\s?[A-Z]{{0,2}}\d", body):
                hits += 1
        for cert in group.get("certificates") or []:
            if re.search(rf"(?<![A-Za-z])(?:{fragment(cert)})(?![A-Za-z])", body):
                hits += 1
        for unit in group.get("units") or []:
            if len(unit) >= 2 and re.search(rf"(?i)(?<![\w.])\d+(?:[.,]\d+)?\s?{fragment(unit)}(?![A-Za-z])", body):
                hits += 1
        if hits >= 2:
            matched.append(name)
    return matched


def load_rules(config: dict[str, Any] | None = None, vault: Path | None = None) -> dict[str, list[str]]:
    """common + the project's industries (auto-detected from the vault when none is named) + own terms."""
    config = config or {}
    table = json.loads(RULES.read_text(encoding="utf-8"))
    own = config.get("check") if isinstance(config.get("check"), dict) else {}
    named = {str(v) for v in [*(config.get("vocabularies") or []), *(own.get("industries") or [])]}
    body = vault_body(vault) if vault else ""
    if not named and body:
        named = set(detect_industries(body, table["industries"]))
    industries = [v for k, v in table["industries"].items() if k in named] or list(table["industries"].values())
    rules = {kind: list(dict.fromkeys([*table["common"].get(kind, []), *(i for group in industries for i in group.get(kind, [])),
                                       *own.get(kind, [])])) for kind in KINDS}
    if vault:
        units, codes = harvest(vault, rules, config, body=body)
        rules["units"] = list(dict.fromkeys([*rules["units"], *units]))
        rules["codes"] = list(dict.fromkeys([*rules["codes"], *codes]))
    return rules


def harvest(vault: Path, rules: dict[str, list[str]], config: dict[str, Any], body: str | None = None) -> tuple[list[str], list[str]]:
    """Units, and prefixes of numbered codes (model series, material grades such as SS304, local
    standards), that this client's own pages use at least three times."""
    body = vault_body(vault) if body is None else body
    known_units = {u.casefold() for u in rules["units"]}
    own = {t.casefold() for t in [*(config.get("own_terms") or []), *(config.get("style_prefixes") or [])]}
    units: dict[str, int] = {}
    for match in re.finditer(r"(?<![\w.])\d+(?:[.,]\d+)?\s?([A-Za-z][A-Za-z/²³·()]{0,6})(?![A-Za-z])", body):
        unit = match.group(1)
        # An all-capital word after a number is usually a name ("2009 YIFA"), not a unit.
        if unit.casefold() in known_units or unit.casefold() in NOT_UNITS or unit.casefold() in own or (unit.isalpha() and unit.isupper()):
            continue
        units[unit] = units.get(unit, 0) + 1
    known_prefixes = {s.casefold() for s in [*rules["standards"], *rules["ratings"], *rules["codes"]]} | own
    standards: dict[str, int] = {}
    for match in re.finditer(r"\b([A-Z][A-Z/]{1,5})\s?-?\d{2,5}\b", body):
        prefix = match.group(1).rstrip("/")
        if prefix.casefold() not in known_prefixes and not any(prefix.casefold().startswith(p) for p in own if p):
            standards[prefix] = standards.get(prefix, 0) + 1
    return ([u for u, n in units.items() if n >= HARVEST_MIN], [s for s, n in standards.items() if n >= HARVEST_MIN])


def fragment(item: str) -> str:
    """A literal term, with any space optional; an entry carrying a backslash or ? is already a regex."""
    if "\\" in item or "?" in item:
        return item
    return r"\s?".join(re.escape(part) for part in item.split(" "))


def compiled(rules: dict[str, list[str]], prefixes: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    def alternatives(items: list[str]) -> str:
        return "|".join(fragment(i) for i in sorted(items, key=len, reverse=True))

    found: list[tuple[str, re.Pattern[str]]] = []
    if prefixes:
        found.append(("型号", re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True))
                                        + r")[\s-]?[A-Z]?-?\d{2,4}[A-Z]{0,4}(?:-\d+)?\b")))
    if rules["standards"]:
        found.append(("标准", re.compile(r"\b(?:" + alternatives(rules["standards"]) + r")\s?-?\s?[A-Z]{0,2}\d[\w.]*(?:[-/]\d[\w.]*)*")))
    if rules["certificates"]:
        found.append(("认证", re.compile(r"(?<![A-Za-z])(?:" + alternatives(rules["certificates"]) + r")(?![A-Za-z])")))
    if rules["ratings"]:
        found.append(("规格", re.compile(r"(?i)\b(?:" + alternatives(rules["ratings"]) + r")\s?\d{1,4}\b")))
    found.extend(("规格", re.compile(p)) for p in rules["patterns"])
    if rules["codes"]:
        found.append(("编号", re.compile(r"\b(?:" + alternatives(rules["codes"]) + r")[\s-]?\d{2,5}[A-Z]{0,3}\b")))
    if rules["units"]:
        single = [u for u in rules["units"] if len(u) == 1 and u.isascii() and u.isalpha()]
        has_quote = '"' in rules["units"]
        has_in = any(u.casefold() == "in" for u in rules["units"])
        regular = [u for u in rules["units"] if u not in single and u != '"' and u.casefold() != "in"]
        if regular:
            found.append(("数字", re.compile(r"(?i)(?<![\w.])[-−+]?\d+(?:[.,]\d+)?[\s-]?(?:" + alternatives(regular) + r")(?![A-Za-z])")))
        if single:
            # Case-sensitive so "3 a day" does not match Amperes, and "2020s" stays a year, not seconds.
            found.append(("数字", re.compile(r"(?<![\w.])(?!(?:19|20)\d{2}s(?![A-Za-z]))[-−+]?\d+(?:[.,]\d+)?[\s-]?(?:"
                                             + alternatives(single) + r")(?![A-Za-z])")))
        if has_in:
            found.append(("数字", re.compile(r"(?<![\w.])[-−+]?\d+(?:[.,]\d+)?[\s-]?in(?![A-Za-z])" + IN_PROSE)))
        if has_quote:
            # Closing quotes around a number ("150") are not inches.
            found.append(("数字", re.compile(r'(?<![\w."])[-−+]?\d+(?:[.,]\d+)?"(?![A-Za-z0-9])')))
    found.append(("年份", YEAR))
    return found


def read_article(path: Path) -> str:
    """Markdown or text as is; a .docx through its document XML, tables included."""
    if path.suffix.casefold() != ".docx":
        return path.read_text(encoding="utf-8", errors="replace")
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    paragraphs = re.findall(r"(?s)<w:p[ >].*?</w:p>", xml)
    return "\n".join(unescape("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p))) for p in paragraphs)


def norm(text: str) -> str:
    text = text.replace("℃", "°c").replace("℉", "°f").replace("−", "-").replace("–", "-").replace("—", "-").replace("～", "~")
    text = re.sub(r"(\d)-(?=months?|years?|days?|hours?)", r"\1", text, flags=re.I)  # "24-month" is "24 month".
    text = re.sub(r"\s+", "", text).casefold()
    for old, new in UNIT_ALIASES:
        text = re.sub(r"(\d)" + re.escape(old) + (r"(?![a-z])" if old.isascii() else ""), r"\1" + new, text)
    return text


def split_zones(pack: str) -> dict[str, str]:
    """The pack's facts, and the two zones the writer must not lean on."""
    zones = {"资料": [], "需核实": [], "未整理": []}
    current = "资料"
    for line in pack.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            heading = line.lstrip("#").strip()
            hit = next((zone for marker, zone in ZONES if marker in heading), None)
            if hit:
                current = hit
            elif line.startswith("## "):
                current = "资料"
        zones[current].append(line)
    return {k: norm("\n".join(v)) for k, v in zones.items()}


def facts(article: str, prefixes: list[str], rules: dict[str, list[str]] | None = None) -> list[tuple[str, str]]:
    """(kind, text) for every hard fact, once each, in order of appearance."""
    found: list[tuple[int, str, str]] = []
    for kind, pattern in compiled(rules or load_rules(), prefixes):
        found += [(m.start(), kind, m.group(0)) for m in pattern.finditer(article)]
    seen: dict[str, tuple[str, str]] = {}
    covered: list[tuple[int, int]] = []
    for start, kind, text in sorted(found, key=lambda f: (f[0], -len(f[2]))):
        text = text.rstrip(".,;:")  # "API 598." at the end of a sentence is API 598.
        end = start + len(text)
        if any(a <= start and end <= b for a, b in covered):
            continue  # "API 609" already holds the "609"; "DN50" is not also a bare number.
        covered.append((start, end))
        seen.setdefault(norm(text), (kind, text.strip()))
    return list(seen.values())


def present(fact: str, zone: str) -> bool:
    """In the zone as written, or as part of a range: "DN300" in "DN50-300", "180°C" in "-10 to 180°C"."""
    key = norm(fact)
    if key in zone:
        return True
    match = re.fullmatch(r"([a-z]+)(\d+)", key)
    if match:  # A rating: the prefix, then the number within a short list after it, or inside a stated range.
        prefix, number = match.groups()
        if re.search(re.escape(prefix) + r"[\d.,~/-]{0,20}?(?<!\d)" + number + r"(?!\d)", zone):
            return True
        ranges = re.findall(re.escape(prefix) + r"(\d+)(?:[~-]|to|至)(?:" + re.escape(prefix) + r")?(\d+)", zone)
        return any(int(low) <= int(number) <= int(high) for low, high in ranges)
    match = re.fullmatch(r"([-+]?\d+(?:[.,]\d+)?)(.+)", key)
    if match:  # A number and its unit, with a range or "to" between: "-10°cto+180°c".
        number, unit = match.groups()
        return bool(re.search(r"(?<![\d.])" + re.escape(number.lstrip("+")) + r"[^a-z\u4e00-\u9fff]{0,4}(?:to|至|~|-)?[^\n]{0,14}?" + re.escape(unit), zone))
    return False


def check(article: str, pack: str | list[str], prefixes: list[str], deny: list[str], vault: Path | None,
          rules: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """One pack, or the main pack and its 补读 packs: each is split into zones on its own."""
    zones = {"资料": "", "需核实": "", "未整理": ""}
    for one in [pack] if isinstance(pack, str) else pack:
        for name, text in split_zones(one).items():
            zones[name] += "\n" + text
    missing, risky, uncurated, ok = [], [], [], 0
    for kind, text in facts(article, prefixes, rules):
        if present(text, zones["资料"]):
            ok += 1
        elif present(text, zones["需核实"]):
            risky.append({"kind": kind, "text": text})
        elif present(text, zones["未整理"]):
            uncurated.append({"kind": kind, "text": text})
        else:
            missing.append({"kind": kind, "text": text})
    if vault and missing:
        from kb_index import INDEX, search
        if (vault / INDEX).is_file():
            for item in missing:
                hits = [h for h in search(vault, item["text"], 5, None, None) if present(item["text"], norm(h["text"]))]
                if hits:
                    item["elsewhere"] = f"{Path(hits[0]['path']).stem} · {hits[0]['section']}（{hits[0]['layer']}）"
    folded = article.casefold()
    competitors = sorted({t for t in deny if t and len(t) >= 2 and re.search(r"(?<![a-z0-9])" + re.escape(t.casefold()) + r"(?![a-z0-9])", folded)})
    return {"checked": ok + len(missing) + len(risky) + len(uncurated), "in_pack": ok, "missing": missing, "risky": risky,
            "uncurated": uncurated, "competitors": competitors}


def report(result: dict[str, Any]) -> str:
    lines = ["# 文章硬事实核对", "",
             f"核对了 {result['checked']} 处型号、标准、认证、规格和数字：资料包里有 {result['in_pack']} 处。"]
    if not (result["missing"] or result["risky"] or result["uncurated"] or result["competitors"]):
        return "\n".join(lines + ["没有发现问题。"]) + "\n"
    lines.append("下面的请写作方确认：改成资料包里的说法、改写成通用表述，或删掉。这只是提醒，不挡交付。")
    if result["missing"]:
        lines.extend(["", "## 资料包里没有"])
        for item in result["missing"]:
            where = (f"——知识库别处出现过（{item['elsewhere']}），先确认说的是不是同一个产品或同一件事，是的话补取这一节"
                     if item.get("elsewhere") else "——知识库里也没有，可能是编的或凭印象写的")
            lines.append(f"- {item['kind']}：{item['text']}{where}")
    if result["risky"]:
        lines.extend(["", "## 只出现在“口径避坑”（不要写成确定的事实）"])
        lines.extend(f"- {item['kind']}：{item['text']}" for item in result["risky"])
    if result["uncurated"]:
        lines.extend(["", "## 只出现在“原文里搜到的”（还没整理，用前核对）"])
        lines.extend(f"- {item['kind']}：{item['text']}" for item in result["uncurated"])
    if result["competitors"]:
        lines.extend(["", "## 出现了竞品或第三方名称", f"- {'、'.join(result['competitors'])}"])
    return "\n".join(lines) + "\n"


def vault_config(vault: Path | None) -> dict[str, Any]:
    path = vault / "05_数据与审核/70_构建状态/kb-config.json" if vault else None
    return json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check an article's hard facts against its kb_lookup material pack.")
    parser.add_argument("--article", required=True, help="The article: .md, .txt or .docx")
    parser.add_argument("--pack", required=True, action="append",
                        help="The material pack kb_lookup.py pack wrote for this article; repeat for its 补读 packs")
    parser.add_argument("--vault", help="The knowledge base: model prefixes, competitor names, and where a missing fact is")
    parser.add_argument("--output", help="Write the reminders to this file")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when something is not in the pack")
    args = parser.parse_args()
    vault = Path(args.vault).resolve() if args.vault else None
    config = vault_config(vault)
    result = check(read_article(Path(args.article)), [Path(p).read_text(encoding="utf-8") for p in args.pack],
                   list(config.get("style_prefixes") or []), list(config.get("deny_terms") or []), vault,
                   load_rules(config, vault))
    text = report(result)
    if args.output:
        write_text(Path(args.output), text)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else text)
    flagged = result["missing"] or result["risky"] or result["competitors"]
    return 1 if args.strict and flagged else 0


if __name__ == "__main__":
    sys.exit(main())
