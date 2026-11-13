#!/usr/bin/env python3
"""Send only the hard pages to Reducto, keep its reading apart, and compare it with MinerU.

Every page of a PDF or PPT has a grade (page-triage.json, written by page_check.py plan): A text,
B pictures with text, C no text layer, D two readings disagree. Files a person chose to read
“Reducto 难页 + AI 抽查” (for their kind, or for the file itself) send their B and C pages here.
Reducto's text is a candidate: published with ⚠, and where MinerU read the same page, MinerU's text
is used and Reducto's is only compared against it. Pages where Reducto disagrees with MinerU or the
text layer on codes or numbers become grade D and go to the AI page check.

Nothing is uploaded until kb-config.json records who approved it. Pages already read for the same
file hash are never sent again.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from ai_read import package_files, pdf_of
from kb_common import BUILD_DIRECTORY, load_config, load_json, load_jsonl, long_path, normalize_space, write_json, write_text
from mineru_ocr import html_table
from reducto_client import KEY_PAGE, MODEL, PRICING_PAGE, ReductoError, client_from_env, failure_report, parse_pages, probe
from samples import GRADES, METHODS, PAGED_KINDS, PICTURES, choose_samples, held_image, load_gate, load_triage, unreadable


FOLDER = "reducto-units"
PAGED = {".pdf", ".pptx", ".ppt"}
SLIDES = {".pptx", ".ppt"}
CHECK_SHARE = 0.3
STOP_FAILURES = {"credential-missing", "credential-invalid", "quota-or-rate-limit"}
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-/]*[A-Za-z0-9]|\d")
UNIT_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)[A-Z%°]{1,4}")


def key_terms(text: str) -> set[str]:
    """Codes (letters with digits) and numbers of two or more digits: what a misreading changes."""
    text = unicodedata.normalize("NFKC", re.sub(r"<[^>]+>", " ", text or ""))
    found = set()
    for raw in TOKEN.findall(text):
        term = raw.upper().strip(".-/").replace("-", "")
        # "120MM" and "120 mm" are the same value; only the number is compared.
        term = match.group(1) if (match := UNIT_SUFFIX.fullmatch(term)) else term
        digits = sum(c.isdigit() for c in term)
        if digits and any(c.isalpha() for c in term) and len(term) >= 3:
            found.add(term)
        elif digits >= 2 and not any(c.isalpha() for c in term):
            found.add(term)
    return found


def record_of(build: Path, item_id: str) -> dict[str, Any]:
    path = build / FOLDER / f"{item_id}.json"
    return load_json(path) if path.is_file() else {}


def page_of(locator: str) -> int | None:
    match = re.fullmatch(r"(?:page|slide) (\d+) ocr", locator)
    return int(match.group(1)) if match else None


def mineru_pages(build: Path, item_id: str) -> dict[int, str]:
    path = build / "mineru-units" / f"{item_id}.json"
    units = load_json(path).get("units", []) if path.is_file() else []
    return {number: unit["text"] for unit in units
            if unit.get("engine") != "ai" and unit.get("text", "").strip() and (number := page_of(unit["locator"])) is not None}


def compare(build: Path, item_id: str) -> dict[int, dict[str, Any]]:
    """Per page Reducto read: what it says against MinerU, and codes it put only in figure descriptions."""
    record = record_of(build, item_id)
    if not record:
        return {}
    mineru = mineru_pages(build, item_id)
    figures = record.get("figures", {})
    result: dict[int, dict[str, Any]] = {}
    for unit in record.get("units", []):
        number = page_of(unit["locator"])
        if number is None:
            continue
        text_terms = key_terms(unit.get("text", ""))
        figure_terms = key_terms("\n".join(figures.get(str(number), [])))
        entry: dict[str, Any] = {"text": unit.get("text", ""), "terms": sorted(text_terms), "figure_terms": sorted(figure_terms),
                                 "figure_only": sorted(figure_terms - text_terms), "mineru": number in mineru}
        if number in mineru:
            theirs = key_terms(mineru[number])
            # A code MinerU found that Reducto only mentions in a figure description is not a disagreement.
            entry["only_mineru"] = sorted(theirs - text_terms - figure_terms)
            entry["only_reducto"] = sorted(text_terms - theirs)
            entry["figure_only"] = sorted(set(entry["figure_only"]) - theirs)
        result[number] = entry
    return result


DISAGREE = ("MinerU 与 Reducto 不一致", "文字层有")


def check_reasons(entry: dict[str, Any], native: str) -> list[str]:
    """Why a page Reducto read still needs an AI look at the picture. Reasons starting with DISAGREE make it grade D."""
    reasons = []
    if entry.get("only_mineru") or entry.get("only_reducto"):
        diff = [f"MinerU 有 {'、'.join(entry['only_mineru'][:4])}" if entry.get("only_mineru") else "",
                f"Reducto 有 {'、'.join(entry['only_reducto'][:4])}" if entry.get("only_reducto") else ""]
        reasons.append("MinerU 与 Reducto 不一致（" + "；".join(d for d in diff if d) + "）")
    elif not entry.get("mineru"):
        # Without MinerU the text layer is the other reading: a code or 3+ digit number it has and Reducto lost.
        missed = sorted(t for t in key_terms(native) - set(entry.get("terms", [])) - set(entry.get("figure_terms", []))
                        if not t.isdigit() or len(t) >= 3)
        if missed:
            reasons.append(f"文字层有 {'、'.join(missed[:4])}，Reducto 没读出")
    figure_only = sorted(set(entry.get("figure_only", [])) - key_terms(native))
    if figure_only:
        reasons.append(f"Reducto 只在图片说明里提到 {'、'.join(figure_only[:4])}，正文没有")
    return reasons


def disagrees(reasons: list[str]) -> bool:
    return any(r.startswith(DISAGREE) for r in reasons)


def candidates(build: Path, files: dict[str, dict[str, Any]], samples_only: bool = False) -> list[dict[str, Any]]:
    """Per file read “Reducto 难页 + AI 抽查”, its grade B and C pages (page-triage.json) nobody read yet.

    With samples_only, the same pages of each paged kind's sample still waiting for a decision, so
    Reducto's result can be seen before choosing. Pages MinerU read, or Reducto read for the same
    file hash, are never sent."""
    routes = load_json(build / "routes.json")
    triage = load_triage(build).get("files", {})
    gate_path = build / "sample-gate.json"
    allowed = set(load_json(gate_path)["allowed"]) if gate_path.is_file() else None
    trial = {info["sample"] for kind, info in load_gate(build).get("kinds", {}).items() if kind in PAGED_KINDS and not info.get("confirmed")}
    if samples_only and not trial and (build / "docs.jsonl").is_file():
        docs = load_jsonl(build / "docs.jsonl")
        item_of = {d["doc_id"]: d["item_id"] for d in docs}
        trial = {item_of[d] for kind, d in choose_samples(docs, {}, build).items() if kind in PAGED_KINDS}
    chosen = []
    for item_id, route in sorted(routes.items()):
        asset = files.get(item_id)
        info = triage.get(item_id)
        suffix = asset["file"].suffix.casefold() if asset else ""
        image = held_image(route) and suffix in PICTURES
        if (route.get("route") != "kb" and not unreadable(route) and not image) or not asset or not info or (suffix not in PAGED and not image):
            continue
        if allowed is not None and item_id not in allowed:
            continue
        if samples_only:
            if item_id not in trial:
                continue
            why = "样例试读：看 Reducto 的效果，再定这类文件怎么处理"
        elif info.get("method") != "reducto":
            continue
        elif unreadable(route):
            why = f"整份读不出内容，这份文件指定“{METHODS['reducto']}”"
        elif image:
            why = f"单张图片，这张指定“{METHODS['reducto']}”"
        else:
            why = f"按“{METHODS['reducto']}”处理（{'这份文件指定' if info.get('method_from') == '文件' else '这类文件选的'}）"
        record = record_of(build, item_id)
        done = set(record.get("pages_read", [])) if record.get("source_hash") == asset.get("source_hash") else set()
        done |= set(mineru_pages(build, item_id))
        pages = {int(number): [why, f"{page['grade']} 级{GRADES[page['grade']][0]}", *page.get("reasons", [])]
                 for number, page in info.get("pages", {}).items() if page["grade"] in {"B", "C"} and int(number) not in done}
        if pages:
            chosen.append({"item_id": item_id, "display_name": route.get("display_name") or asset["file"].name,
                           "path": str(asset["file"]), "source_hash": asset.get("source_hash", ""),
                           "unit": "slide" if suffix in SLIDES else "page", "image": image,
                           "pages": sorted(pages), "reasons": {str(k): v for k, v in sorted(pages.items())}})
    return chosen


def approved(config: dict[str, Any]) -> tuple[bool, str]:
    record = config.get("reducto") or {}
    if record.get("approved") is not True:
        return False, "kb-config.json 里没有 Reducto 外发批准"
    for field in ("approved_by", "approved_at"):
        if not record.get(field):
            return False, f"Reducto 批准记录缺少 {field}"
    return True, ""


def write_plan(build: Path, chosen: list[dict[str, Any]], state: dict[str, Any]) -> None:
    write_json(build / "reducto-plan.json", chosen)
    total = sum(len(entry["pages"]) for entry in chosen)
    lines = ["# Reducto 难页解析计划", "",
             f"共 {len(chosen)} 个文件、{total} 页。只上传这些页所在的文件，并只解析列出的页；按页计费。", "",
             "- 选哪些页：处理方式是“Reducto 难页 + AI 抽查”的文件（按类型选，或单个文件指定），里面 B 级（图文页）和 C 级（难页）"
             "还没人读过的页；MinerU 读过的页不再送。单张图片原样上传，算 1 页。",
             f"- 模型：{MODEL}。价格和套餐看 {PRICING_PAGE} ，余额在 {KEY_PAGE} 查看（新账号送 15,000 credits）。",
             "- Reducto 的结果是候选：入库标 ⚠；同一页有 MinerU 的以 MinerU 为准，两者不一致的页交给 AI 看图核对。",
             "- 免费和标准套餐不能主动删除上传的文件，由 Reducto 按其保留期清除；要求零保留需 Growth / Enterprise 套餐。", "",
             "| 文件 | 页数 | 页码 | 原因 |", "| --- | --- | --- | --- |"]
    for entry in chosen:
        why = sorted({reason for reasons in entry["reasons"].values() for reason in reasons})
        lines.append(f"| {entry['display_name']} | {len(entry['pages'])} | {page_list(entry['pages'])} | {'；'.join(why)} |")
    lines.extend(["", f"API Key 状态：{state['message']}。", "", "同意上传后，在 kb-config.json 里加：",
                  '`"reducto": {"approved": true, "approved_by": "姓名", "approved_at": "YYYY-MM-DD", "scope": "本计划全部文件"}`',
                  "", "可选：`approved_item_ids` 只允许列出的文件；`max_pages` 限制每次最多解析的页数。"])
    write_text(build / "reducto-plan.md", "\n".join(lines) + "\n")


def page_list(pages: list[int]) -> str:
    parts, start = [], None
    for index, number in enumerate(pages):
        start = number if start is None else start
        if index + 1 == len(pages) or pages[index + 1] != number + 1:
            parts.append(str(start) if start == number else f"{start}-{number}")
            start = None
    return ", ".join(parts)


def one_page(pages: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """A picture is page 1, whatever page numbers Reducto gave its blocks."""
    merged: dict[str, Any] = {"text": [], "figures": [], "low_confidence": 0}
    for _, read in sorted(pages.items()):
        merged["text"].extend(read.get("text", []))
        merged["figures"].extend(read.get("figures", []))
        merged["low_confidence"] += read.get("low_confidence", 0)
    return {1: merged}


def page_text(entry: dict[str, Any]) -> str:
    lines = []
    for content in entry.get("text", []):
        content = html_table(content) if re.search(r"(?i)<t[rdh][\s>]", content) else content
        lines.extend(normalize_space(line) for line in content.splitlines() if line.strip())
    return "\n".join(line for line in lines if line)


def store(build: Path, entry: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    record = record_of(build, entry["item_id"])
    if record.get("source_hash") != entry["source_hash"]:
        record = {}  # The file changed: what was read before belongs to another version.
    units = {u["locator"]: u for u in record.get("units", [])}
    figures = dict(record.get("figures", {}))
    for number in entry["pages"]:
        read = result["pages"].get(number, {})
        locator = f"{entry['unit']} {number} ocr"
        text = page_text(read)
        if read.get("figures"):
            # The model's picture descriptions mix what is printed with what it thinks it sees:
            # kept for comparison and for the page check, never published as the page's words.
            figures[str(number)] = read["figures"]
        if text:
            units[locator] = {"locator": locator, "text": text, "engine": "reducto"}
        elif not read.get("figures"):
            units[locator] = {"locator": locator, "text": "", "empty": True, "engine": "reducto"}
    record = {"item_id": entry["item_id"], "source": "reducto", "model": MODEL, "source_hash": entry["source_hash"],
              "extracted_at": date.today().isoformat(),
              "pages_read": sorted(set(record.get("pages_read", [])) | set(entry["pages"])),
              "job_ids": record.get("job_ids", []) + [result["job_id"]],
              "units": sorted(units.values(), key=lambda u: int(u["locator"].split()[1])), "figures": figures}
    write_json(build / FOLDER / f"{entry['item_id']}.json", record)
    return record


def write_diff(build: Path, names: dict[str, str]) -> dict[str, int]:
    counts = {"pages": 0, "with_mineru": 0, "disagree": 0, "figure_only": 0}
    lines = ["# Reducto 与 MinerU 对照", "",
             "只比较款号、型号和两位以上的数字。不一致的页会进入看图核对（page_check.py plan）。", "",
             "| 文件 | 页 | 有 MinerU | 只有 MinerU 有 | 只有 Reducto 有 | 只在图片说明里 |", "| --- | --- | --- | --- | --- | --- |"]
    folder = build / FOLDER
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        item_id = path.stem
        for number, entry in sorted(compare(build, item_id).items()):
            counts["pages"] += 1
            counts["with_mineru"] += entry["mineru"]
            counts["disagree"] += bool(entry.get("only_mineru") or entry.get("only_reducto"))
            counts["figure_only"] += bool(entry["figure_only"])
            if entry.get("only_mineru") or entry.get("only_reducto") or entry["figure_only"]:
                lines.append(f"| {names.get(item_id, item_id)} | {number} | {'是' if entry['mineru'] else '否'} | "
                             f"{'、'.join(entry.get('only_mineru', [])[:8])} | {'、'.join(entry.get('only_reducto', [])[:8])} | "
                             f"{'、'.join(entry['figure_only'][:8])} |")
    lines.extend(["", f"共 {counts['pages']} 页；{counts['with_mineru']} 页两边都读过，{counts['disagree']} 页不一致；"
                      f"{counts['figure_only']} 页有只在图片说明里出现的款号或数字。"])
    write_text(build / "reducto-diff.md", "\n".join(lines) + "\n")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan, approve, and run Reducto on the hard pages of a build.")
    parser.add_argument("command", choices=["plan", "run", "diff"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--package", action="append", default=[], help="Preparation package; repeat for several")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--only", action="append", default=[], help="item_id to run; default every planned file")
    parser.add_argument("--samples", action="store_true", help="Only the samples of kinds still waiting for a decision")
    args = parser.parse_args()
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    routes = load_json(build / "routes.json")
    names = {item_id: route.get("display_name") or item_id for item_id, route in routes.items()}
    if args.command == "diff":
        print(json_text(write_diff(build, names)))
        return 0
    if not args.package:
        parser.error("plan / run 要指定 --package")
    config = load_config(Path(args.config) if args.config else None)
    files = package_files(args.package)
    if not (build / "page-triage.json").is_file():
        print(json_text({"ok": False, "reason": "还没有页面分级（page-triage.json），先跑一次流水线"}))
        return 2
    chosen = candidates(build, files, args.samples)
    if args.only:
        unknown = set(args.only) - {entry["item_id"] for entry in chosen}
        if unknown:
            print(json_text({"ok": False, "reason": "--only 包含不在候选清单中的编号", "item_ids": sorted(unknown)}))
            return 2
        chosen = [entry for entry in chosen if entry["item_id"] in args.only]
    state = probe()
    write_plan(build, chosen, state)
    if args.command == "plan":
        print(json_text({"planned_files": len(chosen), "planned_pages": sum(len(e["pages"]) for e in chosen),
                         "plan": str(build / "reducto-plan.md"), "key": state["message"]}))
        return 0

    ok, reason = approved(config)
    if not ok:
        print(json_text({"ok": False, "reason": reason, "plan": str(build / "reducto-plan.md")}))
        return 2
    rules = config.get("reducto") or {}
    allowed_ids = rules.get("approved_item_ids")
    if allowed_ids is not None and any(entry["item_id"] not in allowed_ids for entry in chosen):
        print(json_text({"ok": False, "reason": "本次所选文件超出 Reducto 批准范围，请用 --only 限定"}))
        return 2
    budget = rules.get("max_pages")
    try:
        api = client_from_env()
    except ReductoError as exc:
        print(json_text(failure_report(exc)))
        return 2
    report: dict[str, Any] = {"done": [], "failed": [], "skipped": [], "pages": 0}
    for entry in chosen:
        if budget is not None and report["pages"] + len(entry["pages"]) > int(budget):
            report["skipped"].append(f"{entry['display_name']}：超过 max_pages={budget}，下次再跑")
            continue
        if entry.get("image"):
            source: Path | None = Path(entry["path"])  # A picture is sent as it is: it has one page and no page range.
        else:
            source = pdf_of(Path(entry["path"]), build, entry["source_hash"][:16])
        if source is None:
            report["skipped"].append(f"{entry['display_name']}：没装 LibreOffice，幻灯片转不成 PDF（页码要和流水线一致）")
            continue
        try:
            result = parse_pages(api, Path(long_path(source)), None if entry.get("image") else entry["pages"])
        except ReductoError as exc:
            failure = failure_report(exc)
            report["failed"].append({"file": entry["display_name"], **failure})
            if failure["failure_type"] in STOP_FAILURES:
                break  # The next file would fail the same way.
            continue
        if entry.get("image"):
            result["pages"] = one_page(result["pages"])
        store(build, entry, result)
        report["pages"] += len(entry["pages"])
        report["done"].append({"file": entry["display_name"], "pages": len(entry["pages"]), "job_id": result["job_id"],
                               "upload_cleanup": result["upload_cleanup"]})
    report["comparison"] = write_diff(build, names)
    write_json(build / "reducto-report.json", report)
    print(json_text({"done": len(report["done"]), "pages": report["pages"], "failed": len(report["failed"]),
                     "skipped": len(report["skipped"]), **report["comparison"], "diff": str(build / "reducto-diff.md")}))
    for failure in report["failed"][:3]:
        print(f"{failure['file']}: {failure['failure_label']} -> {failure['next_action']}")
    return 0 if not report["failed"] else 1


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())
