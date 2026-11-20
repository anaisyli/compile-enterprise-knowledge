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
from pathlib import Path
from typing import Any

from kb_common import parse_frontmatter, write_text
from kb_index import aliases_of, search, tokens
from sections import keyword_pattern


CUSTOMER = Path("03_正式知识/10_客户知识")
EXTERNAL = Path("03_正式知识/20_外部公共知识")
CATALOG = Path("01_工作台/写作目录.md")
STATE = Path("05_数据与审核/70_构建状态")
RULES = Path(__file__).resolve().parents[1] / "assets" / "research-rules.json"
BRIEF_STEM = "公司-速查卡"
CHECK_HEADING = "需核实"
SKIP = {"来源", "另见"}
TABLE_ROWS = 25
WARN_CHARS = 15000
FALLBACK_HITS = 3
MARKER = "<!-- compile-enterprise-knowledge -->"


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


def slim(text: str, with_sources: bool, full_tables: bool, where: str, notes: dict[str, str]) -> str:
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
        out = fold_tables(out, where)
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


def fold_tables(text: str, where: str) -> str:
    def html(match: re.Match[str]) -> str:
        rows = re.findall(r"(?is)<tr\b.*?</tr>", match.group(0))
        if len(rows) - 1 <= TABLE_ROWS:
            return match.group(0)
        head = [re.sub(r"(?s)<[^>]+>", "", c).strip() for c in re.findall(r"(?is)<t[hd]\b[^>]*>(.*?)</t[hd]>", rows[0])]
        return f"（表格共 {len(rows) - 1} 行；列：{' / '.join(h for h in head if h)[:200]}；完整表见：{where}）"

    text = re.sub(r"(?is)<table\b.*?</table>", html, text)
    out, run = [], []
    for line in [*text.splitlines(), ""]:
        if line.lstrip().startswith("|"):
            run.append(line)
            continue
        if len(run) - 2 > TABLE_ROWS:
            out.extend([run[0], run[1], f"（表格共 {len(run) - 2} 行，这里只列表头；完整表见：{where}）"])
        else:
            out.extend(run)
        run = []
        out.append(line)
    return "\n".join(out[:-1])


def catalog(vault: Path) -> str:
    pages = load_pages(vault)
    lines = ["# 写作目录", "", MARKER,
             "> 用法：写文章前先看这一页，挑出要用的“页名#小板块”（或“页名#小板块 > 子节”），再运行",
             "> `kb_lookup.py pack --vault <知识库> --sections \"页名#小板块;…\" --gaps \"知识库里没有的大纲节;…\"`。",
             "> 客户知识可以代表公司写；外部公共知识只作背景和论据。括号里是大约字数。", ""]
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


def pack(vault: Path, specs: list[str], gaps: list[str], query: str | None, with_sources: bool, full_tables: bool,
         brief: bool = True) -> dict[str, Any]:
    pages = load_pages(vault)
    rules = load_rules(vault)
    chosen: list[Section] = []
    problems: list[str] = []
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
    for gap in gaps:
        wanted = set(tokens(gap).split())
        external = sorted((s for p in pages if p.layer == "外部公共知识" for s in p.sections if s.h2 != CHECK_HEADING and s.text),
                          key=lambda s: score(s, wanted), reverse=True)
        reuse = [s for s in external[:2] if score(s, wanted) >= 0.35]
        chosen.extend(s for s in reuse if s not in chosen)
        kind = classify(gap, rules)
        hits = []
        if not reuse and (vault / "02_源资料/knowledge-index.sqlite").is_file():
            # Material waiting for the client's answer is not used before the answer comes.
            hits = [h for h in search(vault, gap, FALLBACK_HITS * 3, None, None, ["原文"])
                    if h["status"] not in {"待客户确认", "已失效"}][:FALLBACK_HITS]
        gap_rows.append({"gap": gap, "kind": kind, "reused": [f"{s.page.stem}#{s.name}" for s in reuse], "source_hits": hits})
    used_pages = list(dict.fromkeys(s.page for s in chosen))
    notes: dict[str, dict[str, str]] = {}
    lines = []
    brief_page = next((p for p in pages if p.stem == BRIEF_STEM), None)
    if brief and brief_page:
        page_notes: dict[str, str] = {}
        text = "\n\n".join(f"### {s.name}\n\n{slim(s.text, with_sources, full_tables, brief_page.stem, page_notes)}"
                           for s in brief_page.sections if s.h2 != CHECK_HEADING)
        lines.extend(["## 速查卡", "", text, ""])
        if page_notes:
            notes[brief_page.stem] = page_notes

    def zone(title: str, layer: str) -> None:
        mine = [s for s in chosen if s.page.layer == layer and s.page is not brief_page]
        if not mine:
            return
        lines.extend([f"## {title}", ""])
        for page in dict.fromkeys(s.page for s in mine):
            meta = ""
            if layer == "外部公共知识":
                meta = f"（{page.fields.get('status', '待抽检')}；复查日期 {page.fields.get('review_due', '未写')}）"
            page_notes = notes.setdefault(page.stem, {})
            for section in [s for s in mine if s.page is page]:
                where = f"{page.stem}#{section.h2}"
                lines.extend([f"### {page.stem} · {section.name}{meta}", "", slim(section.text, with_sources, full_tables, where, page_notes), ""])
            if not page_notes:
                notes.pop(page.stem, None)

    zone("一、客户专属事实（可以代表公司写）", "客户知识")
    zone("二、行业公共知识（只作背景和论据，不能写成客户的能力、承诺或认证）", "外部公共知识")
    checks = []
    for page in [*([brief_page] if brief and brief_page else []), *used_pages]:
        for section in page.sections:
            if section.h2 == CHECK_HEADING and section.text:
                body = slim(section.text, False, True, page.stem, {})
                checks.extend(f"- 〔{page.stem}〕{line.lstrip('- ').strip()}" for line in body.splitlines() if line.strip())
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
            hits = [(row["gap"], h) for row in gap_rows for h in row["source_hits"]]
            if hits:
                lines.extend(["### 原文里搜到的（还没整理进知识页，用前核对，客户专属内容不要据此写成承诺）", ""])
                for gap, hit in hits:
                    text = slim(hit["text"], False, False, Path(hit["path"]).stem, {})[:600]
                    pending = "（待审资料，还没人看过）" if hit["status"] == "待审" else ""
                    lines.extend([f"- 〔{gap}〕{Path(hit['path']).stem}{pending} · {hit['section']}：{text}", ""])
    if with_sources and notes:
        lines.extend(["## 出处", ""])
        for stem, page_notes in notes.items():
            lines.append(f"- {stem}：" + "；".join(f"〔{n}〕{s}" for n, s in sorted(page_notes.items(), key=lambda x: int(x[0]))))
        lines.append("")
    body = "\n".join(lines).strip() + "\n"
    head = ["# 写作资料包", "",
            "> 客户专属事实可以代表公司写；行业公共知识只作背景和论据，不能写成客户的能力、承诺或认证；"
            "口径避坑里的说法不要写成确定的事实。资料包里没有的公司事实不要编。"]
    if len(body) > WARN_CHARS:
        sizes = "、".join(f"{p.stem} {sum(len(s.text) for s in chosen if s.page is p)}" for p in used_pages)
        head.append(f"> ⚠ 资料包约 {len(body)} 字，超过 {WARN_CHARS} 字（各页约：{sizes}）。内容照常全给；需要时少挑几个小板块。")
    return {"text": "\n".join(head) + "\n\n" + body, "sections": [f"{s.page.stem}#{s.name}" for s in chosen],
            "gaps": gap_rows, "problems": problems, "characters": len(body)}


def split_list(values: list[str]) -> list[str]:
    return [part.strip() for value in values for part in re.split(r"[;；\n]", value) if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Take writing material from a published knowledge base (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("catalog", help="Print the writing catalog")
    one.add_argument("--vault", required=True)
    one.add_argument("--write", action="store_true", help="Also refresh 01_工作台/写作目录.md")
    two = sub.add_parser("pack", help="Print the material pack for the picked sections")
    two.add_argument("--vault", required=True)
    two.add_argument("--sections", action="append", default=[], help='"页名#小板块;页名#小板块 > 子节"; repeat or separate with ;')
    two.add_argument("--gaps", action="append", default=[], help="Outline sections the catalog has nothing for; ; separated")
    two.add_argument("--query", help="Free keywords, when no section was picked")
    two.add_argument("--with-sources", action="store_true", help="Keep the 〔n〕 marks and list where each came from")
    two.add_argument("--full-tables", action="store_true", help="Do not fold tables longer than 25 rows")
    two.add_argument("--no-brief", action="store_true")
    two.add_argument("--json", action="store_true")
    two.add_argument("--output", help="Write the pack to this file instead of printing it")
    args = parser.parse_args()
    vault = Path(args.vault).resolve()
    if args.command == "catalog":
        text = catalog(vault)
        if args.write:
            write_catalog(vault)
        print(text)
        return 0
    result = pack(vault, split_list(args.sections), split_list(args.gaps), args.query, args.with_sources, args.full_tables,
                  brief=not args.no_brief)
    if args.output:
        write_text(Path(args.output), result["text"])
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "text" or not args.output}, ensure_ascii=False, indent=2))
    elif not args.output:
        print(result["text"])
    else:
        print(json.dumps({"output": args.output, "characters": result["characters"], "problems": result["problems"],
                          "gaps": [{k: v for k, v in g.items() if k != "source_hits"} for g in result["gaps"]]},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
