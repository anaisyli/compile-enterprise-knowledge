#!/usr/bin/env python3
"""Acceptance check, written apart from the pipeline so it can disagree with it.

Rereads the extracted source text and the finished files. A run counts only when
this reports "verified": true.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from source_fidelity import reviewed_units
from sheet_layout import excel_date

from guard_blocks import EMAIL, FURNITURE, HARD_PATTERNS, LABELLED_PHONE, MOBILE_CN, ROW_PATTERNS, compile_terms
from kb_common import (
    unapproved_d,
    BUILD_DIRECTORY,
    CLEAN_DIRECTORY,
    load_config,
    load_json,
    load_jsonl,
    load_units,
    normalize_space,
    parse_frontmatter,
    sha256_file,
    write_json,
    write_text,
)


UNREAD_LIMIT = 0.3
REQUIRED_FIELDS = ("doc_id", "tier", "visibility", "module", "source_item_id")
# Words every clean page may use: labels the pipeline itself writes.
BOILERPLATE = re.compile(r"^(?:>|\s*$|---|[a-z_]+: )")


# Hex image names can look like phone numbers.
IMAGE_LINK = re.compile(r"!\[\[img-[^\]]+\]\]|`?img-[0-9a-f]+\.[a-z]+`?")


SHEET_LOCATOR = re.compile(r"^sheet .+ \(([^)]+)\)$")
SHEET_CELL = re.compile(r"(?:^|;\s*)[A-Z]{1,3}\d+=")
TABLE_RULE = re.compile(r"^:?-{3,}:?$")


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|") if not TABLE_RULE.match(cell.strip())]


def squeeze(text: str) -> str:
    # A protected dimension asterisk is identical source data after Markdown renders.
    return re.sub(r"\s+", "", text.replace(" ", "").replace(r"\*", "*"))


def ocr_format_key(text: str) -> str:
    """Ignore OCR list markup only, never substantive words or style-code hyphens."""
    text = unicodedata.normalize("NFKC", text)
    return squeeze(re.sub(r"(?:^|\s)(?:\\?[-*•·])\s+", " ", text)).casefold()


def ocr_segments(text: str) -> list[str]:
    """Split an OCR line at visual bullet markers before source comparison.

    MinerU sometimes joins several bullet lines around a page heading.  The
    words are still present in the OCR unit, but the joined line is not a
    contiguous substring of that unit.  Comparing the visual segments keeps
    the fidelity check strict while allowing this harmless line-wrap change.
    """
    text = re.sub(r"(?<!^)(?=[©◎☐⚠️])", "\n", text)
    text = re.sub(r"(?<!^)(?=\\-\s*)", "\n", text)
    return [part.strip() for part in text.splitlines() if part.strip()]


def scan_text(
    text: str, terms: re.Pattern[str] | None, own_domains: set[str], own_contacts: list[str], keep_texts: set[str],
    product_rows: bool = True,
) -> list[str]:
    """product_rows: table lines are product-table rows, held to the stricter row rules. A table
    inside a brochure or a Word file is ordinary text laid out in cells."""
    hits = []
    for paragraph in text.split("\n"):
        if squeeze(paragraph) in keep_texts:
            continue
        for name, pattern in HARD_PATTERNS.items():
            if pattern.search(paragraph):
                hits.append(name)
        if paragraph.startswith("|") and product_rows:
            hits.extend(name for name, pattern in ROW_PATTERNS.items() if pattern.search(paragraph))
        for match in EMAIL.finditer(paragraph):
            if match.group(1).casefold() not in own_domains:
                hits.append("email")
        remainder = paragraph
        for contact in own_contacts:
            remainder = remainder.replace(contact, " ")
        if MOBILE_CN.search(remainder) or LABELLED_PHONE.search(remainder):
            hits.append("phone")
        if terms and terms.search(paragraph):
            hits.append("denied_term")
    return hits


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a built knowledge base.")
    parser.add_argument("--package", action="append", required=True, help="Preparation package; repeat")
    parser.add_argument("--text-dir", action="append", required=True, help="Extracted text units; repeat")
    parser.add_argument("--ocr-text-dir", help="OCR units; defaults to 04_知识库构建/mineru-units when present")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--sample", type=int, default=30, help="Blocks to list for a human or agent spot check")
    parser.add_argument("--seed", type=int, default=20261102)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run = Path(args.out).resolve()
    build = run / BUILD_DIRECTORY
    clean = run / CLEAN_DIRECTORY
    config = load_config(Path(args.config) if args.config else None)
    packages = [Path(raw).resolve() for raw in args.package]
    assets = [asset for package in packages for asset in load_json(package / "审计记录/package-manifest.json")["assets"]]
    routes = load_json(build / "routes.json")
    docs = {doc["doc_id"]: doc for doc in load_jsonl(build / "docs.jsonl")}
    blocks = load_jsonl(build / "blocks.jsonl")
    published: dict[str, str] = load_json(build / "published.json")
    terms = load_json(build / "guard-terms.json")
    text_dirs = [Path(raw).resolve() for raw in args.text_dir]
    ocr_dir = Path(args.ocr_text_dir).resolve() if args.ocr_text_dir else build / "mineru-units"
    if ocr_dir.is_dir():
        text_dirs.append(ocr_dir)
    website = build / "website"
    pages = {page["page_id"]: page for page in load_json(website / "pages.json")} if (website / "pages.json").is_file() else {}
    own_domains = {domain.casefold() for domain in config["own_domains"]}
    errors: list[str] = []

    # Every item in the package has a recorded fate.
    asset_ids = {asset["item_id"] for asset in assets}
    for item_id in asset_ids - set(routes):
        errors.append(f"no route for {item_id}")
    hashes = {asset["item_id"]: asset["source_hash"] for asset in assets}
    hashes.update({page_id: page["html_sha256"] for page_id, page in pages.items()})

    # Nothing outside the allow path was published.
    for doc_id, relative in published.items():
        doc = docs[doc_id]
        if doc["item_id"] != "knowledge-points" and routes.get(doc["item_id"], {}).get("route") != "kb":
            errors.append(f"published without a kb route: {relative}")
        path = clean / relative
        if not path.is_file():
            errors.append(f"missing file: {relative}")
            continue
        fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        for field in REQUIRED_FIELDS:
            if not fields.get(field):
                errors.append(f"{relative}: missing {field}")
        if doc["source_sha256"] and fields.get("source_sha256") != hashes.get(doc["item_id"]):
            errors.append(f"{relative}: source hash differs from the package")

    # A run that read almost nothing must not look successful.
    unread = [r for r in routes.values() if r.get("route") == "hold" and r.get("reason") == "no_extractable_content"]
    readable = [r for r in routes.values() if r.get("route") == "kb"] + unread
    if readable and len(unread) / len(readable) > UNREAD_LIMIT and (len(unread) >= 3 or len(unread) * 2 > len(readable)):
        errors.append(f"{len(unread)} / {len(readable)} 份该入库的资料读不到文字：检查 PDF 读取依赖（pypdf）、LibreOffice，或提取目录是否给错")

    # Every kept block is found word for word in its source.
    units_by_item: dict[str, str] = {}
    points = {
        record["knowledge_point"]
        for package in packages
        for record in load_jsonl(package / "C-知识点/knowledge-points.jsonl")
    }
    kept = [block for block in blocks if block["status"] in {"kept", "flagged", "pending_client"} and block["doc_id"] in published]
    for block in kept:
        doc = docs[block["doc_id"]]
        if block["kind"] == "point":
            if block["text"] not in points:
                errors.append(f"point not in package: {block['block_id']}")
            continue
        item_id = doc["item_id"]
        if item_id not in units_by_item:
            dirs = [website / "units"] if doc.get("origin") == "website" else text_dirs
            units = reviewed_units(build, item_id, doc['source_sha256'], load_units(dirs, item_id))
            units_by_item[item_id] = squeeze(" ".join(u["text"] for u in units))
        source = units_by_item[item_id]
        # Non-product workbook sheets are rendered as Markdown tables.  The pipes,
        # separator row, and <br> tags are layout introduced by the builder; compare
        # each cell's text to the source export instead of treating the whole row as
        # a literal source line.
        if block["kind"] == "text" and block["locator"].startswith("sheet ") and block["text"].lstrip().startswith("|"):
            for line in block["text"].splitlines():
                if not line.strip() or TABLE_RULE.fullmatch(line.strip().strip("| ")):
                    continue
                for piece in table_cells(line):
                    piece = re.sub(r"<br\s*/?>", "", piece)
                    if squeeze(piece) and squeeze(piece) not in source:
                        errors.append(f"text not in source: {doc['title']} {block['locator']}")
                        break
            continue
        # ``above``/``below`` are layout parts generated from spreadsheet cell
        # exports.  Their text is rearranged (and the empty-column note is
        # builder metadata), so it cannot be matched as one literal source
        # line.  Cell-level preservation is checked in the source-line pass.
        if block["kind"] == "text" and block["locator"].startswith("sheet ") and \
                (block["locator"].endswith(" above") or block["locator"].endswith(" below")):
            continue
        values = block["cells"].values() if block["kind"] == "row" else block["text"].splitlines()
        for value in values:
            # A Markdown table is checked cell by cell; its bars and rules are layout, not text.
            pieces = table_cells(value) if str(value).lstrip().startswith("|") else str(value).split(" / ")
            if block["locator"].endswith(" ocr") and not str(value).lstrip().startswith("|"):
                pieces = ocr_segments(str(value))
            for piece in pieces:
                if block["locator"].endswith(" ocr"):
                    piece = re.sub(r"^[©◎☐⚠️]\s*", "", piece)
                if squeeze(piece) and squeeze(piece) not in source:
                    errors.append(f"text not in source: {doc['title']} {block['locator']}")
                    break

    # Nothing vanishes silently: each source line is kept, held, or a recognised footer.
    for doc in docs.values():
        if doc["tier"] not in ("T1", "T2"):
            continue
        # A line break kept inside a table cell is written <br>; it is layout, not text.
        accounted = squeeze("".join(b["text"] for b in blocks if b["doc_id"] == doc["doc_id"]).replace("<br>", ""))
        accounted_ocr = ocr_format_key("\n".join(b["text"] for b in blocks if b["doc_id"] == doc["doc_id"]))
        dirs = [website / "units"] if doc.get("origin") == "website" else text_dirs
        units = reviewed_units(build, doc['item_id'], doc['source_sha256'], load_units(dirs, doc['item_id']))
        for unit in units:
            sheet = SHEET_LOCATOR.match(unit["locator"])
            if sheet and sheet.group(1) != "visible":
                continue  # Hidden sheets never enter.
            for line in unit["text"].splitlines():
                if sheet:
                    # A sheet row "A7=x; B7=y" is published as table cells: each value must be there.
                    # Phone/contact rows are deliberately removed by guard_blocks.py.
                    # They therefore cannot be expected to appear in the cleaned source.
                    if LABELLED_PHONE.search(line):
                        continue
                    # An Excel date number (46148) is published as the date it stands for (2026-05-05).
                    missing = [v for v in SHEET_CELL.split(line) if v.strip() and squeeze(v) not in accounted
                               and not (excel_date(v) and squeeze(excel_date(v)) in accounted)]
                    if missing:
                        errors.append(f"source line lost: {doc['title']} {unit['locator']}")
                        break
                    continue
                text = normalize_space(line)
                if unit["locator"].endswith(" ocr") and ocr_format_key(text) in accounted_ocr:
                    continue  # The same words are present; only OCR list formatting differs.
                if text and squeeze(text) not in accounted and not FURNITURE.search(text) and doc.get("origin") != "website":
                    errors.append(f"source line lost: {doc['title']} {unit['locator']}")
                    break

    # The finished files carry nothing the guard forbids, in any published page.
    decisions_path = build / "review-decisions.json"
    decisions = load_json(decisions_path) if decisions_path.is_file() else {}
    keep_ids = set(decisions.get("keep_blocks", []))
    keep_texts = {squeeze(line) for b in blocks if b["block_id"] in keep_ids for line in b["text"].split("\n")}
    pattern = compile_terms(terms["hard"] + terms["soft"])
    knowledge = build / "vault" / "03_正式知识"
    # Every page goes through the check, the 建站资料 view too: it lists file and folder names,
    # and site_view.py hides the ones that carry a customer, a price or a quote term.
    pages = list(clean.rglob("*.md")) + list(knowledge.rglob("*.md"))
    for path in pages:
        raw = path.read_text(encoding="utf-8")
        product_rows = parse_frontmatter(raw).get("tier") == "T3"
        body = raw.split("\n---\n", 1)[-1]
        body = "\n".join(line for line in body.splitlines() if not BOILERPLATE.match(line))
        body = IMAGE_LINK.sub(" ", body)
        for hit in sorted(set(scan_text(body, pattern, own_domains, config["own_contacts"], keep_texts, product_rows))):
            errors.append(f"{path.name}: forbidden content ({hit})")

    # File names travel with the content, so they are checked too.
    if pattern:
        for path in pages:
            if pattern.search(path.stem):
                errors.append(f"forbidden term in file name: {path.name}")
        # Picture names are made from the words beside them, so they pass the same test.
        named = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
        for entry in named.values():
            if pattern.search(entry["file"]) or pattern.search(entry.get("alt", "")):
                errors.append(f"forbidden term in picture name: {entry['file']}")

    # Risky passages are published on purpose, so each one must carry its mark.
    for block in kept:
        if block.get("flags"):
            page = clean / published[block["doc_id"]]
            if "⚠" not in page.read_text(encoding="utf-8"):
                errors.append(f"标了风险的内容没有 ⚠：{docs[block['doc_id']]['title']} {block['locator']}")
    curation = build / "curation-report.json"
    for slug, result in (load_json(curation).items() if curation.is_file() else []):
        if not result["ok"]:
            errors.append(f"整理页没通过核对，未发布：{result['title']}（{'；'.join(result['errors'][:2])}）")

    # A knowledge layer left half written must not look finished.
    warnings: list[str] = []
    d_path = build / "review-decisions.json"
    # Counted as pages, like docs.jsonl: one file kept in three folders is one document to approve.
    waiting_d = {routes[i].get("source_hash") or i for i in unapproved_d(routes, load_json(d_path) if d_path.is_file() else {})}
    if waiting_d:
        warnings.append(f"{len(waiting_d)} 份保留的 D 类还没批准，现在是“待审”：看过原文没问题，把资料编号写进 review-decisions.json 的 approve_docs")
    undecided_d = {r.get("source_hash") or i for i, r in routes.items() if r.get("decision") == "D" and r.get("reason") == "unresolved_item"}
    if undecided_d:
        warnings.append(f"{len(undecided_d)} 份 D 类还没决定要不要用：看过后写进 kb-config.json 的 d_keep / d_drop，"
                        "要问客户的写进 ask_client")
    topics_path = build / "curation" / "topics.json"
    topics = load_json(topics_path) if topics_path.is_file() else []
    topics = [t for t in topics if t.get("blocks")]  # A topic with no source text has nothing to write.
    unwritten = [t for t in topics if not (build / "curation" / t["slug"] / "page.md").is_file() and not t.get("skipped")]
    if unwritten and len(unwritten) < len(topics):
        names = "、".join(f"{t['topic']}（{t['slug']}）" for t in unwritten)
        errors.append(f"整理页没写完：缺 {names}。写好 page.md 后重跑；确实不写的，在 kb-config.json 的 skip_topics 里写明理由")
    elif unwritten:
        warnings.append(f"还没有整理页：{len(unwritten)} 个主题已准备好，等 AI 写 page.md（04_知识库构建/curation）")
    usage_path = build / "curation-used.json"
    if usage_path.is_file() and load_json(usage_path):
        cited = {d for page in load_json(usage_path).values() for d in page["sources"]}
        statuses = load_json(build / "statuses.json")
        idle = [docs[d]["title"] for d in published if statuses.get(d) == "有效" and docs[d]["tier"] in ("T1", "T2") and d not in cited]
        if idle:
            warnings.append(f"{len(idle)} 份有效资料没有被任何整理页引用：{'、'.join(idle[:8])}{' 等' if len(idle) > 8 else ''}")

    # Pages picked for a look beside their text: started and left half done is not finished.
    plan_path = build / "page-check" / "plan.json"
    check_plan = load_json(plan_path) if plan_path.is_file() else {}
    checked = load_json(build / "page-check" / "results.json") if (build / "page-check" / "results.json").is_file() else {}
    waiting = check_plan.get("to_check", 0)
    skip = config.get("page_check", {}).get("skip") if isinstance(config.get("page_check"), dict) else None
    if check_plan.get("ok") is False:
        warnings.append(f"没做看图核对：{check_plan.get('reason')}")
    elif waiting and not skip:
        if checked:
            errors.append(f"看图核对没做完：还有 {waiting} 页（04_知识库构建/page-check/todo.jsonl）。看完运行 page_check.py collect 再重跑；"
                          "确实不做，在 kb-config.json 的 page_check 里写 {\"skip\": \"理由\"}")
        else:
            warnings.append(f"还没做看图核对：{waiting} 页等 AI 对照原图看（04_知识库构建/page-check/README.md）")

    # A deck without slide pictures still has all its text; say why the pictures are missing.
    status_path = build / "package-text" / "extract-status.json"
    extracted = load_json(status_path).get("items", {}) if status_path.is_file() else {}
    no_slides = Counter(item["page_images_skipped"] for item in extracted.values() if item.get("page_images_skipped"))
    if no_slides:
        warnings.append("PPT 没有幻灯片截图：" + "，".join(f"{reason} {count} 份" for reason, count in no_slides.most_common())
                        + "（文字已全部读取；装好 LibreOffice、PyMuPDF 后重跑即可补上）")

    # Kinds of material nobody has looked at an example of yet.
    waiting_path = build / "samples-waiting.json"
    unconfirmed = load_json(waiting_path) if waiting_path.is_file() else []
    if unconfirmed:
        warnings.append(f"{len(unconfirmed)} 类资料还没确认样例（{'、'.join(unconfirmed[:6])}）：看《处理计划与样例》，确认后再统一处理同类")

    # A website that was asked for but not read leaves a hole nobody would notice.
    if config.get("websites") and not any(d.get("origin") == "website" for d in docs.values()):
        fetch = build / "website" / "fetch-report.json"
        failed = load_json(fetch).get("failed", []) if fetch.is_file() else []
        reason = ("还没运行 fetch_website.py" if not fetch.is_file()
                  else "；".join(f"{f['url']}：{f['error']}" for f in failed[:3]) if failed
                  else "读到的页面都没有可用正文，可能要用浏览器读取")
        errors.append(f"kb-config.json 登记了官网，但知识库里没有官网页面（{reason}）。"
                      "重新读取，或用浏览器保存后 --browser-capture 导入；确定不要官网就从 websites 里删掉")

    rng = random.Random(args.seed)
    sample = rng.sample(kept, min(args.sample, len(kept)))
    lines = ["# 抽样核对", "", "把每一条和原件对照，看提取有没有错、有没有不该进库的内容。", ""]
    for block in sample:
        doc = docs[block["doc_id"]]
        lines.append(f"- [ ] {doc['title']}｜{doc['package_path']}｜{block['locator']}")
        lines.append(f"  - {normalize_space(block['text'])[:300]}")
    write_text(build / "抽样核对.md", "\n".join(lines) + "\n")

    withdrawn = sum(1 for path in clean.rglob("*.md") if parse_frontmatter(path.read_text(encoding="utf-8")).get("status") == "已失效")
    report: dict[str, Any] = {
        "verified": not errors,
        "withdrawn": withdrawn,
        "documents": len(published),
        "blocks_kept": len(kept),
        "error_count": len(errors),
        "errors": errors[:200],
        "warnings": warnings,
        "sample_file": str(build / "抽样核对.md"),
    }
    write_json(build / "校验结果.json", report)
    print(json.dumps({key: report[key] for key in ("verified", "documents", "blocks_kept", "error_count")}, ensure_ascii=False))
    for error in errors[:15]:
        print(error)
    for warning in warnings:
        print("提醒：" + warning)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
