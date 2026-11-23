#!/usr/bin/env python3
"""What a writing skill calls to take material from the knowledge base. Read-only.

catalog  the writing catalog: every knowledge page with its aliases and its ## / ### sections
         and their sizes. The writer reads it once and picks the sections an article needs.
pack     the picked sections as one material pack, slimmed on the way out (links, source lists
         and picture embeds removed, long tables folded), in four zones: the client's own facts,
         public industry knowledge, the "需核实" items of the pages used, and the gaps. Gaps are
         sorted into client-only (ask the client, never research) and public (may be researched),
         and the source layer is searched for them as a last resort.

Pages are read straight from the Markdown in the vault: no index, no lock, many writers at once.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from kb_common import parse_frontmatter, vault_lock, write_json, write_text
from kb_index import aliases_of, search, tokens
from sections import keyword_pattern


CUSTOMER = Path("03_正式知识/10_客户知识")
EXTERNAL = Path("03_正式知识/20_外部公共知识")
CATALOG = Path("01_工作台/写作目录.md")
STATE = Path("05_数据与审核/70_构建状态")
TASK_LEDGER = Path("04_文章任务/00_文章台账.md")
RULES = Path(__file__).resolve().parents[1] / "assets" / "research-rules.json"
BRIEF_STEM = "公司-速查卡"
CHECK_HEADING = "需核实"
SKIP = {"来源", "另见"}
TABLE_ROWS = 25
WARN_CHARS = 15000
# With --budget, tables longer than this fold too.
BUDGET_ROWS = 10
FALLBACK_HITS = 3
MARKER = "<!-- compile-enterprise-knowledge -->"
CONFIRMED_OUTLINE = {"已确认", "locked", "confirmed"}
ITEM_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}(?:-[A-Z]{1,4})?-?[0-9][A-Z0-9-]{0,18}(?![A-Za-z0-9])")


def item_codes(text: str) -> set[str]:
    return {m.group(0).upper() for m in ITEM_CODE_RE.finditer(text)}


def filter_page_checks(page: Page, picked_on_page: list[Section], all_picked_codes: set[str], lines: list[str]) -> list[str]:
    """On a multi-section page where only some sections were picked, drop 需核实 lines
    that exclusively name item/model codes belonging to unpicked sections of that page."""
    picked_ids = {id(s) for s in picked_on_page}
    unpicked = [s for s in page.sections if s.h2 != CHECK_HEADING and s.text and id(s) not in picked_ids]
    if not unpicked:
        return lines
    unpicked_codes: set[str] = set()
    for s in unpicked:
        unpicked_codes |= item_codes(f"{s.name}\n{s.text}")
    exclusive_unpicked = unpicked_codes - all_picked_codes
    if not exclusive_unpicked:
        return lines
    exclusive_picked = all_picked_codes - unpicked_codes
    kept = []
    for line in lines:
        codes = item_codes(line)
        if (codes & exclusive_unpicked) and not (codes & exclusive_picked):
            continue
        kept.append(line)
    return kept


@dataclass(eq=False)
class Section:
    page: "Page"
    h2: str
    h3: str | None
    text: str

    @property
    def name(self) -> str:
        return f"{self.h2} > {self.h3}" if self.h3 else self.h2


@dataclass(eq=False)
class Page:
    path: Path
    stem: str
    title: str
    layer: str
    aliases: list[str]
    fields: dict[str, str]
    sections: list[Section] = field(default_factory=list)


def squeeze(text: str) -> str:
    return re.sub(r"[\s_\-·]+", "", text).casefold()


def load_rules(vault: Path | None = None) -> dict[str, Any]:
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    config_path = vault / STATE / "kb-config.json" if vault else None
    if config_path and config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        rules.update((config.get("research") or {}) if isinstance(config.get("research"), dict) else {})
        rules["_deny"] = list(config.get("deny_terms") or [])
        rules["_own"] = list(config.get("own_terms") or [])
        rules["_own_domains"] = list(config.get("own_domains") or [])
    return rules


def parse_page(path: Path, layer: str) -> Page | None:
    text = path.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)
    if fields.get("status") == "已失效":
        return None
    body = text.split("\n---\n", 1)[-1] if text.startswith("---\n") else text
    title = next((m.group(1).strip() for m in re.finditer(r"(?m)^# (.+)$", body)), fields.get("title") or path.stem)
    page = Page(path, path.stem, title, layer, aliases_of(fields), fields)
    h2, h3, lines = None, None, []

    def flush() -> None:
        if h2 and h2 not in SKIP:
            content = "\n".join(lines).strip()
            if content or h3 is None:
                page.sections.append(Section(page, h2, h3, content))

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            h2, h3, lines = line[3:].strip(), None, []
        elif line.startswith("### ") and h2:
            flush()
            h3, lines = line[4:].strip(), []
        elif not line.startswith("# "):
            lines.append(line)
    flush()
    return page


def load_pages(vault: Path) -> list[Page]:
    pages = []
    for root, layer in ((CUSTOMER, "客户知识"), (EXTERNAL, "外部公共知识")):
        for path in sorted((vault / root).rglob("*.md")) if (vault / root).is_dir() else []:
            page = parse_page(path, layer)
            if page:
                pages.append(page)
    return pages


LINK = re.compile(r"!?\[\[([^\]]+?)\]\]")


def slim(text: str, with_sources: bool, full_tables: bool, where: str, notes: dict[str, str],
         rows: int = TABLE_ROWS) -> str:
    """The text a writer needs: no picture embeds, no link syntax, the notice line gone, long tables folded."""
    def link(match: re.Match[str]) -> str:
        if match.group(0).startswith("!"):
            return ""
        parts = re.split(r"\\?\|", match.group(1), maxsplit=1)
        target = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else Path(target.split("#", 1)[0]).name
        source = Path(target.split("#", 1)[0]).name + ("#" + target.split("#", 1)[1] if "#" in target else "")
        numbered = re.fullmatch(r"(见原文)?〔(\d+)〕", label)
        if numbered:
            if numbered.group(1):
                return f"（见原文：{source}）"
            if with_sources:
                notes[numbered.group(2)] = source
                return f"〔{numbered.group(2)}〕"
            return ""
        if label.startswith("见原文"):
            return f"（见原文：{source}）"
        return label

    lines = []
    for line in text.splitlines():
        if line.startswith("> 整理页：") or line.startswith("> 外部公共知识："):
            continue
        lines.append(LINK.sub(link, line).rstrip())
    out = drop_empty_columns("\n".join(lines))
    if not full_tables:
        out = fold_tables(out, where, rows)
    out = re.sub(r"表格出处：\s*$", "", out, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def drop_empty_columns(text: str) -> str:
    """A 出处 column is empty once its links are gone; it only costs tokens."""
    def cells(line: str) -> list[str]:
        return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]

    out, run = [], []
    for line in [*text.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            run.append(line)
            continue
        if len(run) >= 2:
            rows = [cells(r) for r in run]
            width = len(rows[0])
            empty = [i for i in range(width) if rows[0][i] in {"出处", "来源"}
                     and all(i >= len(r) or not r[i] or re.fullmatch(r":?-{3,}:?", r[i]) for r in rows[1:])]
            if empty:
                run = ["| " + " | ".join(c for i, c in enumerate(r) if i not in empty) + " |" for r in rows]
        out.extend(run)
        run = []
        out.append(line)
    return "\n".join(out[:-1])


def refetch(where: str) -> str:
    """The command that brings one folded table back whole, ready to copy."""
    return (f"要写表里的具体数值时，先补读整张表：`kb_lookup.py pack --vault <知识库> --sections \"{where}\" --full-tables "
            f"--no-brief --id-prefix R --output <本篇工作目录>/资料包-补读.md`，只写表里有的数")


def fold_tables(text: str, where: str, limit: int = TABLE_ROWS) -> str:
    def html(match: re.Match[str]) -> str:
        rows = re.findall(r"(?is)<tr\b.*?</tr>", match.group(0))
        if len(rows) - 1 <= limit:
            return match.group(0)
        head = [re.sub(r"(?s)<[^>]+>", "", c).strip() for c in re.findall(r"(?is)<t[hd]\b[^>]*>(.*?)</t[hd]>", rows[0])]
        return f"（表格共 {len(rows) - 1} 行，已折叠；列：{' / '.join(h for h in head if h)[:200]}。{refetch(where)}）"

    text = re.sub(r"(?is)<table\b.*?</table>", html, text)
    out, run = [], []
    for line in [*text.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            run.append(line)
            continue
        if len(run) - 2 > limit:
            out.extend([run[0], run[1], f"（表格共 {len(run) - 2} 行，已折叠，这里只列表头。{refetch(where)}）"])
        else:
            out.extend(run)
        run = []
        out.append(line)
    return "\n".join(out[:-1])


def catalog(vault: Path) -> str:
    pages = load_pages(vault)
    lines = ["# 写作目录", "", MARKER,
             "> **写作 Agent 先看这里**：完整规则在 compile-enterprise-knowledge 的 `references/writer-guide.md`，写之前读一遍。要点：",
             "> 1. 先按读者需要独立定大纲（不看知识库），落盘到 `04_文章任务/10_写作记录/<task_key>/大纲.md` 并标 `outline_status: 已确认`。",
             "> 2. 再对照下面的目录给 `大纲.md` 填好 `## 资料对应`（页名#小板块 或 页名#小板块 > 子节）和 `## 缺口`，不要整页读、不要读 `02_源资料`。",
             "> 3. 运行 `kb_lookup.py pack --task \"<大纲.md 路径>\"` 在同目录生成带 `[K#]` 编号的 `资料包.md` 和 `资料包.json`（也可直传 `--vault` + `--sections`）。",
             "> 4. 客户专属事实可以代表公司写；外部公共知识只作背景和论据；口径避坑里的说法不要写成确定的事实；资料包里没有的公司事实不要编。",
             "> 5. 写完在同目录交极简 `引用表.md`，运行 `check_article.py --article <终稿> --pack <资料包> --citations <引用表> --vault <知识库>` 生成 `引用追溯.md` 并按提示改。",
             "> 除 `04_文章任务/10_写作记录/<task_key>/` 和 `research.py` 外，知识库只读。括号里是大约字数。", ""]
    sizes = {id(s): len(slim(s.text, False, False, "", {})) for p in pages for s in p.sections}
    for layer in ("客户知识", "外部公共知识"):
        mine = [p for p in pages if p.layer == layer]
        lines.extend([f"## {layer}", ""])
        if not mine:
            lines.extend(["- （暂无）", ""])
            continue
        for page in mine:
            size = sum(sizes[id(s)] for s in page.sections)
            extra = []
            if page.aliases:
                extra.append("别名：" + "、".join(page.aliases))
            if page.fields.get("status") and page.fields["status"] != "有效":
                extra.append(f"状态：{page.fields['status']}")
            lines.append(f"### {page.stem}（{page.title}；约 {size} 字{'；' + '；'.join(extra) if extra else ''}）")
            tree: dict[str, list[Section]] = {}
            for section in page.sections:
                tree.setdefault(section.h2, []).append(section)
            for h2, parts in tree.items():
                total = sum(sizes[id(s)] for s in parts)
                children = [f"{s.h3}（{sizes[id(s)]}）" for s in parts if s.h3]
                lines.append(f"- {h2}（{total}）" + (f"：{'、'.join(children)}" if children else ""))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_catalog(vault: Path) -> Path | None:
    """01_工作台/写作目录.md, refreshed by publish and by research; a page a person wrote there is left alone."""
    target = vault / CATALOG
    if target.is_file() and MARKER not in target.read_text(encoding="utf-8"):
        return None
    write_text(target, catalog(vault))
    return target


def find_page(pages: list[Page], name: str) -> Page | None:
    key = squeeze(name)
    for page in pages:
        if key in {squeeze(page.stem), squeeze(page.title), *(squeeze(a) for a in page.aliases)}:
            return page
    matches = [p for p in pages if key and (key in squeeze(p.stem) or key in squeeze(p.title))]
    return matches[0] if len(matches) == 1 else None


def pick(pages: list[Page], spec: str) -> tuple[list[Section], str | None]:
    """"页名", "页名#小板块" or "页名#小板块 > 子节" -> sections, or an error message."""
    name, _, part = spec.partition("#")
    page = find_page(pages, name.strip())
    if not page:
        return [], f"找不到页面：{name.strip()}"
    content = [s for s in page.sections if s.h2 != CHECK_HEADING]
    if not part.strip():
        return content, None
    wanted = [squeeze(p) for p in re.split(r"\s*[>/／]\s*", part.strip()) if p.strip()]
    if len(wanted) == 1:
        found = [s for s in content if squeeze(s.h2) == wanted[0] or (s.h3 and squeeze(s.h3) == wanted[0])]
        # A ## heading brings its ### children along.
        if any(squeeze(s.h2) == wanted[0] for s in found):
            found = [s for s in content if squeeze(s.h2) == wanted[0]]
    else:
        found = [s for s in content if squeeze(s.h2) == wanted[0] and s.h3 and squeeze(s.h3) == wanted[1]]
    return (found, None) if found else ([], f"{page.stem} 里没有小板块：{part.strip()}")


def score(section: Section, query: set[str]) -> float:
    if not query:
        return 0.0
    head = set(tokens(" ".join([section.page.title, *section.page.aliases, section.h2, section.h3 or ""])).split())
    body = set(tokens(section.text).split())
    hits = sum(1.0 if q in head else 0.7 for q in query if q in head or q in body)
    return hits / len(query)


def classify(gap: str, rules: dict[str, Any]) -> str:
    pattern = keyword_pattern(rules.get("client_terms", []))
    padded = f" {gap.casefold()} "
    return "客户专属" if pattern and pattern.search(padded) else "公共知识"


def line_sources(raw: str) -> list[str]:
    """Source file#section references extracted from Obsidian links on one raw line."""
    found = []
    for match in LINK.finditer(raw):
        if match.group(0).startswith("!"):
            continue
        target = re.split(r"\\?\|", match.group(1), maxsplit=1)[0].strip()
        src = Path(target.split("#", 1)[0]).name + ("#" + target.split("#", 1)[1] if "#" in target else "")
        if src and src not in found:
            found.append(src)
    return found


def source_map(raw_text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Map stripped line text -> its source refs, plus all unique sources in the section."""
    by_line: dict[str, list[str]] = {}
    all_sources: list[str] = []
    pending_table_rows: list[str] = []
    for raw in raw_text.splitlines():
        srcs = line_sources(raw)
        for s in srcs:
            if s not in all_sources:
                all_sources.append(s)
        stripped = LINK.sub("", raw).strip()
        if raw.lstrip().startswith("|"):
            key = squeeze(stripped)
            if key:
                by_line[key] = srcs
                if not srcs:
                    pending_table_rows.append(key)
        elif raw.strip().startswith("表格出处：") and srcs and pending_table_rows:
            for key in pending_table_rows:
                by_line[key] = list(srcs)
            pending_table_rows = []
        else:
            pending_table_rows = []
            key = squeeze(stripped)
            if key and srcs:
                by_line[key] = srcs
    return by_line, all_sources


def number_block(raw_text: str, slimmed: str, layer: str, page: str, section: str,
                 counter: list[int], prefix: str, facts: list[dict[str, Any]],
                 outline: str = "") -> str:
    """Prefix each fact bullet, paragraph, or table row with [K1], [K2]… and record it for 资料包.json."""
    by_line, fallback = source_map(raw_text)

    def next_id() -> str:
        counter[0] += 1
        return f"{prefix}{counter[0]}"

    def lookup_src(line: str) -> list[str]:
        return by_line.get(squeeze(line)) or fallback[:3]

    out: list[str] = []
    lines = slimmed.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            out.append(line)
            i += 1
            continue
        # Markdown table block
        if line.lstrip().startswith("|"):
            run = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                run.append(lines[i])
                i += 1
            if len(run) >= 3 and re.match(r"^\s*\|?\s*:?-{3,}", run[1]):
                out.append("| 编号 " + run[0].lstrip())
                out.append("| --- " + run[1].lstrip())
                for row in run[2:]:
                    if row.strip().startswith("（表格共"):
                        out.append(row)
                        continue
                    fid = next_id()
                    out.append(f"| [{fid}] " + row.lstrip())
                    facts.append({"id": fid, "text": row.strip(), "layer": layer, "page": page, "section": section,
                                  "sources": lookup_src(row), "risky": ("⚠" in row or "需核实" in row), "outline": outline})
            else:
                out.extend(run)
            continue
        # HTML table block
        if stripped.lower().startswith("<table"):
            html_lines = [line]
            i += 1
            while i < len(lines) and "</table>" not in html_lines[-1].lower():
                html_lines.append(lines[i])
                i += 1
            block = "\n".join(html_lines)
            fid = next_id()
            out.append(f"[{fid}]")
            out.append(block)
            facts.append({"id": fid, "text": re.sub(r"(?s)<[^>]+>", " ", block).strip(), "layer": layer, "page": page,
                          "section": section, "sources": fallback[:3], "risky": ("⚠" in block or "需核实" in block),
                          "outline": outline})
            continue
        # Notices / headings / quotes are not numbered as facts
        if stripped.startswith(("#", ">", "（表格共", "（为了控制")):
            out.append(line)
            i += 1
            continue
        fid = next_id()
        bullet = re.match(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$", line)
        if bullet:
            out.append(f"{bullet.group(1)}[{fid}] {bullet.group(2)}")
            body_text = bullet.group(2).strip()
        else:
            out.append(f"[{fid}] {line}")
            body_text = stripped
        facts.append({"id": fid, "text": body_text, "layer": layer, "page": page, "section": section,
                      "sources": lookup_src(body_text), "risky": ("⚠" in body_text or "需核实" in body_text),
                      "outline": outline})
        i += 1
    return "\n".join(out)


def parse_task(path: Path) -> dict[str, Any]:
    """Read an outline task file (大纲.md): frontmatter, outline headings, 资料对应, and 缺口."""
    text = path.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)
    body = text.split("\n---\n", 1)[-1] if text.startswith("---\n") else text
    buckets: dict[str, list[str]] = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            buckets.setdefault(current, [])
        elif current:
            buckets[current].append(line)
    outline = [re.sub(r"^\s*[-*+]\s+", "", line).strip()
               for line in buckets.get("大纲", []) if re.match(r"^\s*[-*+]\s+", line)]
    mapping: list[tuple[str, list[str]]] = []
    for raw in buckets.get("资料对应", []):
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] not in {"大纲节", "大纲", "章节"} and not set(cells[0]) <= set("-: "):
                specs = split_list([cells[1]])
                if specs:
                    mapping.append((cells[0], specs))
            continue
        item = re.sub(r"^\s*[-*+]\s+", "", line).strip()
        if not item:
            continue
        parts = re.split(r"\s*(?:->|→|=>)\s*", item, maxsplit=1)
        if len(parts) == 2:
            heading, right = parts[0].strip(), parts[1].strip()
        elif "：" in item and "#" in item.split("：", 1)[1]:
            heading, right = (p.strip() for p in item.split("：", 1))
        elif ": " in item and "#" in item.split(": ", 1)[1]:
            heading, right = (p.strip() for p in item.split(": ", 1))
        else:
            heading, right = "", item
        specs = split_list([right])
        if specs:
            mapping.append((heading, specs))
    gaps = [re.sub(r"^\s*[-*+]\s+", "", line).strip()
            for line in buckets.get("缺口", [])
            if line.strip() and not line.strip().startswith(("<!--", "|"))]
    return {"fields": fields, "outline": outline, "mapping": mapping, "gaps": [g for g in gaps if g]}


def resolve_vault(vault_arg: str | None, task_path: Path | None, fields: dict[str, str]) -> Path:
    if vault_arg:
        return Path(vault_arg).resolve()
    if fields.get("vault"):
        return Path(fields["vault"]).resolve()
    if task_path:
        for parent in task_path.resolve().parents:
            if (parent / "01_工作台").is_dir():
                return parent
    raise SystemExit("请传 --vault，或在 大纲.md 的 vault 字段里写明知识库路径。")


def update_article_ledger(vault: Path, task_key: str, title: str, writing_skill: str, status: str,
                          coverage: str = "-", task_path: Path | None = None) -> Path | None:
    """Upsert one row in 04_文章任务/00_文章台账.md under the vault lock; never fails a read-only lookup."""
    if not task_key or not (vault / "01_工作台").is_dir():
        return None
    if not vault_lock(vault, "article-ledger"):
        return None
    ledger = vault / TASK_LEDGER
    rows: dict[str, list[str]] = {}
    order: list[str] = []
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("|"):
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
                if len(cells) >= 7 and cells[0] != "任务编号" and not set(cells[0]) <= set("-: "):
                    rows[cells[0]] = cells[:7]
                    if cells[0] not in order:
                        order.append(cells[0])
    rel = ""
    if task_path:
        try:
            rel = task_path.resolve().relative_to(vault.resolve()).as_posix().removesuffix(".md")
        except ValueError:
            rel = str(task_path)
    link = f"[[{rel}\\|大纲]]" if rel and not Path(rel).is_absolute() else (rel or "-")
    existing = rows.get(task_key)
    when = existing[1] if existing else date.today().isoformat()
    safe = lambda s: str(s or "-").replace("|", "｜").replace("\n", " ").strip() or "-"
    rows[task_key] = [
        safe(task_key), when, safe(title or (existing[2] if existing else "-")),
        safe(writing_skill or (existing[3] if existing else "-")),
        safe(status),
        safe(coverage if coverage != "-" else (existing[5] if existing else "-")),
        link if task_path else (existing[6] if existing else "-"),
    ]
    if task_key not in order:
        order.append(task_key)
    lines = ["# 文章写作台账", "", MARKER, "",
             "| 任务编号 | 日期 | 标题 | 写作 Skill | 状态 | 引用覆盖率 | 记录目录 |",
             "| --- | --- | --- | --- | --- | --- | --- |",
             *("| " + " | ".join(rows[k]) + " |" for k in order), ""]
    write_text(ledger, "\n".join(lines))
    return ledger


def pack(vault: Path, specs: list[str], gaps: list[str], query: str | None, with_sources: bool, full_tables: bool,
         brief: bool = True, budget: int | None = None, task_groups: list[tuple[str, list[str]]] | None = None,
         id_prefix: str = "K") -> dict[str, Any]:
    pages = load_pages(vault)
    rules = load_rules(vault)
    chosen: list[Section] = []
    problems: list[str] = []
    grouped: list[tuple[str, list[Section]]] = []
    if task_groups:
        for heading, group_specs in task_groups:
            group_sections: list[Section] = []
            for spec in group_specs:
                found, problem = pick(pages, spec)
                if problem:
                    problems.append(problem)
                for s in found:
                    if s not in group_sections:
                        group_sections.append(s)
                    if s not in chosen:
                        chosen.append(s)
            if group_sections:
                grouped.append((heading or "未命名章节", group_sections))
    for spec in specs:
        found, problem = pick(pages, spec)
        if problem:
            problems.append(problem)
        chosen.extend(s for s in found if s not in chosen)
    if query:
        wanted = set(tokens(query).split())
        ranked = sorted((s for p in pages for s in p.sections if s.h2 != CHECK_HEADING and s.text),
                        key=lambda s: score(s, wanted), reverse=True)
        chosen.extend(s for s in ranked[:8] if score(s, wanted) >= 0.2 and s not in chosen)
    gap_rows = []
    reused_sections: list[Section] = []
    for gap in gaps:
        wanted = set(tokens(gap).split())
        external = sorted((s for p in pages if p.layer == "外部公共知识" for s in p.sections if s.h2 != CHECK_HEADING and s.text),
                          key=lambda s: score(s, wanted), reverse=True)
        reuse = [s for s in external[:2] if score(s, wanted) >= 0.35]
        for s in reuse:
            if s not in chosen:
                chosen.append(s)
            if s not in reused_sections:
                reused_sections.append(s)
        kind = classify(gap, rules)
        hits = []
        if not reuse and (vault / "02_源资料/knowledge-index.sqlite").is_file():
            # Material waiting for the client's answer is not used before the answer comes.
            hits = [h for h in search(vault, gap, FALLBACK_HITS * 3, None, None, ["原文"])
                    if h["status"] not in {"待客户确认", "已失效"}][:FALLBACK_HITS]
        gap_rows.append({"gap": gap, "kind": kind, "reused": [f"{s.page.stem}#{s.name}" for s in reuse], "source_hits": hits})
    used_pages = list(dict.fromkeys(s.page for s in chosen))
    brief_page = next((p for p in pages if p.stem == BRIEF_STEM), None)

    def render(rows: int, with_hits: bool) -> tuple[str, list[dict[str, Any]]]:
        notes: dict[str, dict[str, str]] = {}
        facts: list[dict[str, Any]] = []
        counter = [0]
        lines = []
        if brief and brief_page:
            page_notes: dict[str, str] = {}
            parts = []
            for s in brief_page.sections:
                if s.h2 == CHECK_HEADING:
                    continue
                slimmed = slim(s.text, with_sources, full_tables, f"{brief_page.stem}#{s.name}", page_notes, rows)
                numbered = number_block(s.text, slimmed, "速查卡", brief_page.stem, s.name, counter, id_prefix, facts)
                parts.append(f"### {s.name}\n\n{numbered}")
            lines.extend(["## 速查卡", "", "\n\n".join(parts), ""])
            if page_notes:
                notes[brief_page.stem] = page_notes

        def emit_section(section: Section, outline: str = "") -> None:
            page = section.page
            meta = ""
            if page.layer == "外部公共知识":
                meta = f"（{page.fields.get('status', '待抽检')}；复查日期 {page.fields.get('review_due', '未写')}）"
            tag = "【行业公共知识：只作背景和论据，不能写成客户能力】" if page.layer == "外部公共知识" else ""
            page_notes = notes.setdefault(page.stem, {})
            where = f"{page.stem}#{section.name}"
            slimmed = slim(section.text, with_sources, full_tables, where, page_notes, rows)
            numbered = number_block(section.text, slimmed, page.layer, page.stem, section.name, counter, id_prefix, facts, outline)
            heading_prefix = "####" if outline else "###"
            lines.extend([f"{heading_prefix} {page.stem} · {section.name}{meta}{tag}", "", numbered, ""])
            if not page_notes:
                notes.pop(page.stem, None)

        if grouped:
            lines.extend(["## 一、按大纲组织的资料", ""])
            shown: set[int] = set()
            for heading, group_sections in grouped:
                used_labels = "、".join(f"{s.page.stem}#{s.name}" for s in group_sections)
                lines.extend([f"### 大纲节：{heading}（用了：{used_labels}）", ""])
                for s in group_sections:
                    emit_section(s, outline=heading)
                    shown.add(id(s))
            extra_external = [s for s in reused_sections if id(s) not in shown and s.page is not brief_page]
            if extra_external:
                lines.extend(["## 二、行业公共知识（只作背景和论据，不能写成客户的能力、承诺或认证）", ""])
                for s in extra_external:
                    emit_section(s)
        else:
            def zone(title: str, layer: str) -> None:
                mine = [s for s in chosen if s.page.layer == layer and s.page is not brief_page]
                if not mine:
                    return
                lines.extend([f"## {title}", ""])
                for page in dict.fromkeys(s.page for s in mine):
                    for section in [s for s in mine if s.page is page]:
                        emit_section(section)

            zone("一、客户专属事实（可以代表公司写）", "客户知识")
            zone("二、行业公共知识（只作背景和论据，不能写成客户的能力、承诺或认证）", "外部公共知识")
        all_picked_codes: set[str] = set()
        for s in chosen:
            all_picked_codes |= item_codes(f"{s.page.stem}\n{s.name}\n{s.text}")
        checks = []
        for page in [*([brief_page] if brief and brief_page else []), *used_pages]:
            picked_on_page = [s for s in chosen if s.page is page]
            for section in page.sections:
                if section.h2 == CHECK_HEADING and section.text:
                    body = slim(section.text, False, True, page.stem, {})
                    raw_lines = [line.lstrip("- ").strip() for line in body.splitlines() if line.strip()]
                    if page is not brief_page:
                        raw_lines = filter_page_checks(page, picked_on_page, all_picked_codes, raw_lines)
                    checks.extend(f"- 〔{page.stem}〕{line}" for line in raw_lines)
        if checks:
            lines.extend(["## 三、口径避坑（下面的说法有冲突或待核实：不要写成确定的事实，不写具体数字）", "", *dict.fromkeys(checks), ""])
        if gap_rows or problems:
            lines.extend(["## 四、缺口", ""])
            for problem in problems:
                lines.append(f"- {problem}（看写作目录核对页名和小板块名）")
            if gap_rows:
                lines.extend(["| 大纲节 | 判断 | 怎么办 |", "| --- | --- | --- |"])
                for row in gap_rows:
                    if row["reused"]:
                        advice = "外部公共知识里已有，已放进第二部分：" + "、".join(row["reused"])
                    elif row["kind"] == "客户专属":
                        advice = "客户专属，不外部调研：文章里中性表述或不写；需要时登记 CUS 问客户"
                    else:
                        advice = f"可以外部调研：`research.py plan --vault <知识库> --from-outline \"{row['gap']}\"`"
                    lines.append(f"| {row['gap']} | {row['kind']} | {advice} |")
                lines.append("")
                hits = [(row["gap"], h) for row in gap_rows for h in row["source_hits"]] if with_hits else []
                if hits:
                    lines.extend(["### 原文里搜到的（还没整理进知识页，用前核对，客户专属内容不要据此写成承诺）", ""])
                    for gap, hit in hits:
                        text = slim(hit["text"], False, False, Path(hit["path"]).stem, {})[:600]
                        pending = "（待审资料，还没人看过）" if hit["status"] == "待审" else ""
                        lines.extend([f"- 〔{gap}〕{Path(hit['path']).stem}{pending} · {hit['section']}：{text}", ""])
                elif not with_hits and any(row["source_hits"] for row in gap_rows):
                    lines.extend(["（为了控制篇幅，原文里搜到的线索没有放进来；需要时去掉 --budget 再取一次。）", ""])
        if with_sources and notes:
            lines.extend(["## 出处", ""])
            for stem, page_notes in notes.items():
                lines.append(f"- {stem}：" + "；".join(f"〔{n}〕{s}" for n, s in sorted(page_notes.items(), key=lambda x: int(x[0]))))
            lines.append("")
        return "\n".join(lines).strip() + "\n", facts

    # A budget only takes away what loses no fact: long tables fold sooner, uncurated leads drop.
    steps = [(TABLE_ROWS, True)] + ([(BUDGET_ROWS, True), (BUDGET_ROWS, False)] if budget else [])
    applied = []
    body, facts = "", []
    for rows, with_hits in steps:
        body, facts = render(rows, with_hits)
        if not budget or len(body) <= budget:
            break
        applied.append(rows)
    limit = budget or WARN_CHARS
    head = ["# 写作资料包", "",
            "> 客户专属事实可以代表公司写，其中无 ⚠ 且不与口径避坑冲突的明确型号、规格、材质、标准和场景贴合大纲时应优先具体写入正文；"
            "行业公共知识只作背景和论据，不能写成客户的能力、承诺或认证；"
            "口径避坑里的说法不要写成确定的事实。资料包里没有的公司事实不要编。",
            f"> 每条事实前带 `[{id_prefix}1]`、`[{id_prefix}2]` 编号；写完后在 `引用表.md` 标明每句引用的编号，由 `check_article.py --citations` 核对并生成 `引用追溯.md`。"]
    if budget and applied:
        head.append(f"> 为了控制在 {budget} 字以内：超过 {BUDGET_ROWS} 行的表已折叠（附补读命令）"
                    + ("，原文里搜到的线索已去掉" if len(applied) > 1 else "") + "。客户事实、外部知识和口径避坑一条没删。")
    if len(body) > limit:
        sizes = "、".join(f"{p.stem} {sum(len(s.text) for s in chosen if s.page is p)}" for p in used_pages)
        head.append(f"> ⚠ 资料包约 {len(body)} 字，超过 {limit} 字（各页约：{sizes}）。内容照常全给；需要时少挑几个小板块。")
    return {"text": "\n".join(head) + "\n\n" + body, "sections": [f"{s.page.stem}#{s.name}" for s in chosen],
            "gaps": gap_rows, "problems": problems, "characters": len(body), "facts": facts}


def split_list(values: list[str]) -> list[str]:
    return [part.strip() for value in values for part in re.split(r"[;；\n]", value) if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Take writing material from a published knowledge base (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("catalog", help="Print the writing catalog")
    one.add_argument("--vault", required=True)
    one.add_argument("--write", action="store_true", help="Also refresh 01_工作台/写作目录.md")
    two = sub.add_parser("pack", help="Print the material pack for the picked sections or an outline task file")
    two.add_argument("--vault", help="Knowledge base folder (optional when --task gives vault or lives inside the vault)")
    two.add_argument("--task", help="Outline task file (大纲.md): reads 资料对应 and 缺口, groups by outline, writes 资料包.md + 资料包.json alongside it")
    two.add_argument("--sections", action="append", default=[], help='"页名#小板块;页名#小板块 > 子节"; repeat or separate with ;')
    two.add_argument("--gaps", action="append", default=[], help="Outline sections the catalog has nothing for; ; separated")
    two.add_argument("--query", help="Free keywords, when no section was picked")
    two.add_argument("--with-sources", action="store_true", help="Keep the 〔n〕 marks and list where each came from")
    two.add_argument("--full-tables", action="store_true", help="Do not fold tables longer than 25 rows")
    two.add_argument("--no-brief", action="store_true")
    two.add_argument("--id-prefix", default="K", help="Prefix for fact IDs (default K; use R for 补读 packs)")
    two.add_argument("--budget", type=int, help="Aim for this many characters: fold tables over 10 rows, then drop "
                                                "uncurated source hits; never drop a fact")
    two.add_argument("--json", action="store_true")
    two.add_argument("--output", help="Write the pack to this file instead of printing it")
    args = parser.parse_args()
    if args.command == "catalog":
        vault = Path(args.vault).resolve()
        text = catalog(vault)
        if args.write:
            write_catalog(vault)
        print(text)
        return 0
    task_path = Path(args.task).resolve() if args.task else None
    task = parse_task(task_path) if task_path else None
    if task:
        status = task["fields"].get("outline_status", "").strip()
        if status and status not in CONFIRMED_OUTLINE:
            raise SystemExit(f"大纲尚未确认（outline_status={status}），请先确认大纲并标为“已确认”再取资料。")
    vault = resolve_vault(args.vault, task_path, task["fields"] if task else {})
    gaps = [*(task["gaps"] if task else []), *split_list(args.gaps)]
    result = pack(vault, split_list(args.sections), gaps, args.query, args.with_sources, args.full_tables,
                  brief=not args.no_brief, budget=args.budget,
                  task_groups=task["mapping"] if task else None, id_prefix=args.id_prefix)
    output_path = Path(args.output).resolve() if args.output else (task_path.parent / "资料包.md" if task_path else None)
    meta_path = None
    if output_path:
        write_text(output_path, result["text"])
        meta_path = output_path.with_suffix(".json")
        write_json(meta_path, {
            "vault": str(vault),
            "task": str(task_path) if task_path else "",
            "task_key": task["fields"].get("task_key", "") if task else "",
            "id_prefix": args.id_prefix,
            "sections": result["sections"],
            "facts": result["facts"],
        })
    if task and task_path:
        task_key = task["fields"].get("task_key") or task_path.parent.name
        update_article_ledger(vault, task_key, task["fields"].get("title_or_topic", ""),
                              task["fields"].get("writing_skill", ""), "写作中", task_path=task_path)
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k not in {"text", "facts"} or not output_path}, ensure_ascii=False, indent=2))
    elif not output_path:
        print(result["text"])
    else:
        print(json.dumps({"output": str(output_path), "facts_json": str(meta_path), "facts": len(result["facts"]),
                          "characters": result["characters"], "problems": result["problems"],
                          "gaps": [{k: v for k, v in g.items() if k != "source_hits"} for g in result["gaps"]]},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
