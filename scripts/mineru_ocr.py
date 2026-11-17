#!/usr/bin/env python3
"""Pick the files that need MinerU, ask for approval, run it, and store page-level text.

Uploading sends client files to a cloud service, so nothing is sent until the project's
kb-config.json records who approved it. Results are cached by file hash: a rerun never
uploads or pays twice.
"""

from __future__ import annotations

import argparse
import html
from datetime import date
from html.parser import HTMLParser
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from extract_package_text import markdown_table
from kb_common import BUILD_DIRECTORY, PAGED_FILES, load_config, load_json, load_jsonl, normalize_space, read_by, remaining_ocr_pages, run_lock, write_json, write_text
from mineru_client import MinerUError, client_from_env, extract, failure_report, probe
from samples import (METHODS, PAGED_KINDS, TYPES, choose_samples, doc_type, load_decisions, load_gate, method_and_source,
                     unreadable, unreadable_method)


MINERU_FILES = {".pdf", ".pptx", ".ppt", ".docx", ".doc"}

OCR_DIRECTORY = "mineru-units"
CACHE_DIRECTORY = "mineru-cache"


def candidates(build: Path, package_paths: dict[str, Path], samples_only: bool = False, missing: bool = False) -> list[dict[str, Any]]:
    """Files MinerU would read: whole kinds a person chose “MinerU 整份 + AI 核对” for after seeing their
    sample, pages with no text layer, and files nothing could be read from. With samples_only, the
    sample of each kind still waiting for a decision, so MinerU's result can be seen before deciding.
    With missing, also every PDF/PPT in the knowledge base MinerU has not read yet."""
    done = read_by(build) if missing else {}
    routes = load_json(build / "routes.json")
    docs = {d["item_id"]: d for d in load_jsonl(build / "docs.jsonl")} if (build / "docs.jsonl").is_file() else {}
    decisions = load_decisions(build)
    gate = load_gate(build).get("kinds", {})
    trial = {info["sample"] for kind, info in gate.items() if kind in PAGED_KINDS and not info.get("confirmed")}
    if samples_only and not trial:
        # No gate (review off or all confirmed): the fullest file of each paged kind stands in.
        item_of = {d["doc_id"]: d["item_id"] for d in docs.values()}
        trial = {item_of[d] for kind, d in choose_samples(list(docs.values()), {}, build).items() if kind in PAGED_KINDS}
    chosen = []
    for item_id, route in routes.items():
        name = route.get("display_name") or ""
        if Path(name).suffix.casefold() not in MINERU_FILES or item_id not in package_paths:
            continue
        reasons = []
        kind = doc_type(docs[item_id]) if item_id in docs else ""
        if samples_only:
            if item_id in trial and route["route"] == "kb":
                reasons.append("样例试读：看 MinerU 的效果，再定这类文件怎么处理")
        else:
            method, source = method_and_source(kind, decisions, item_id)
            if route["route"] == "kb" and method == "mineru":
                reasons.append(f"这份文件按“{METHODS['mineru']}”处理" if source == "文件"
                               else f"这类（{TYPES.get(kind, ('', ''))[0]}）按“{METHODS['mineru']}”处理")
            # Files read by the model (no upload) or by Reducto keep their no-text pages away from MinerU.
            left = remaining_ocr_pages(build, item_id, route.get("ocr_pages") or []) if route["route"] == "kb" and method in {"text", "mineru"} else []
            if left:
                reasons.append(f"{len(left)} 页没有文字层或文字很少，还没人读过")
            if unreadable(route):
                held_method, held_from = unreadable_method(decisions, item_id)
                if held_method in {"mineru", "text"}:
                    reasons.append("文件读不出文字" + ("（这份文件指定 MinerU）" if held_from == "文件" else ""))
            if (missing and not reasons and route["route"] == "kb" and Path(name).suffix.casefold() in PAGED_FILES
                    and "mineru" not in done.get(item_id, set())):
                reasons.append("已入库，还没走过 MinerU（整批补跑）")
        if reasons:
            path = package_paths[item_id]
            chosen.append({"item_id": item_id, "display_name": name, "path": str(path),
                           "size_mb": round(path.stat().st_size / 1024 / 1024, 1) if path.is_file() else None,
                           "reasons": reasons, "source_hash": route.get("source_hash", "")})
    return sorted(chosen, key=lambda entry: entry["display_name"])


def approved(config: dict[str, Any]) -> tuple[bool, str]:
    record = config.get("mineru") or {}
    if record.get("approved") is not True:
        return False, "kb-config.json 里没有 MinerU 外发批准"
    for field in ("approved_by", "approved_at"):
        if not record.get(field):
            return False, f"MinerU 批准记录缺少 {field}"
    return True, ""


class _HtmlTableParser(HTMLParser):
    """Parse an HTML table into a 2D grid, expanding colspan and rowspan into every covered cell."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.grid: dict[tuple[int, int], str] = {}
        self.row = -1
        self.col = 0
        self.max_col = 0
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._colspan = 1
        self._rowspan = 1

    def _span(self, value: str | None) -> int:
        return max(1, int(value)) if value and value.strip().isdigit() else 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        if name == "tr":
            self.row += 1
            self.col = 0
        elif name in {"td", "th"} and self.row >= 0:
            while (self.row, self.col) in self.grid:
                self.col += 1
            attr_map = {k.casefold(): v for k, v in attrs}
            self._colspan = self._span(attr_map.get("colspan"))
            self._rowspan = self._span(attr_map.get("rowspan"))
            self._cell_parts = []
            self._in_cell = True
        elif name == "br" and self._in_cell:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in {"td", "th"} and self._in_cell:
            text = " ".join("".join(self._cell_parts).split())
            for dr in range(self._rowspan):
                for dc in range(self._colspan):
                    self.grid[(self.row + dr, self.col + dc)] = text
            self.col += self._colspan
            self.max_col = max(self.max_col, self.col)
            self._in_cell = False


def html_table(body: str) -> str:
    """MinerU/Reducto return tables as HTML; expand spans into every covered cell so columns align."""
    parser = _HtmlTableParser()
    parser.feed(body)
    parser.close()
    if parser.row < 0 or parser.max_col == 0:
        return re.sub(r"<[^>]+>", " ", body)
    max_row = max(r for r, _ in parser.grid)
    rows = []
    for r in range(max_row + 1):
        row = [parser.grid.get((r, c), "") for c in range(parser.max_col)]
        if any(cell.strip() for cell in row):
            rows.append(row)
    if not rows:
        return re.sub(r"<[^>]+>", " ", body)
    return markdown_table(rows)


def separate_markdown_tables(text: str) -> str:
    """Ensure Markdown tables have blank lines before and after so Obsidian renders them as tables."""
    out: list[str] = []
    in_table = False
    for line in text.splitlines():
        is_row = line.lstrip().startswith("|") and "|" in line.lstrip()[1:]
        if is_row and not in_table and out and out[-1].strip():
            out.append("")
        elif not is_row and in_table and line.strip() and out and out[-1].strip():
            out.append("")
        out.append(line)
        in_table = is_row
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def page_units(result: Path) -> list[dict[str, str]]:
    """Turn MinerU's content list into one unit per page; fall back to the whole Markdown."""
    lists = sorted(result.rglob("*content_list*.json"))
    if lists:
        pages: dict[int, list[str]] = {}
        for entry in load_json(lists[0]):
            text = entry.get("text") or ""
            is_table = entry.get("type") == "table"
            if is_table:
                text = html_table(entry.get("table_body") or "")
            elif entry.get("type") == "image":
                # MinerU's current schema puts style labels in image_footnote.
                # Keep the older img_* aliases too; captions are not the only text.
                parts = []
                for field in ("image_caption", "img_caption", "image_footnote", "img_footnote"):
                    value = entry.get(field) or []
                    parts.extend([value] if isinstance(value, str) else value)
                text = "\n".join(dict.fromkeys(parts))
            text = "\n".join(normalize_space(line) for line in text.splitlines() if line.strip())
            if text:
                bucket = pages.setdefault(int(entry.get("page_idx", 0)) + 1, [])
                if is_table or (bucket and bucket[-1].lstrip().startswith("|")):
                    bucket.append(f"\n{text}\n" if is_table else f"\n{text}")
                else:
                    bucket.append(text)
        # Keep a separate locator for provenance; the source-page builder chooses the reading.
        return [{"locator": f"page {page} ocr",
                 "text": separate_markdown_tables("\n".join(lines).strip())}
                for page, lines in sorted(pages.items())]
    markdowns = sorted(result.rglob("*.md"), key=lambda path: path.stat().st_size, reverse=True)
    if markdowns:
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdowns[0].read_text(encoding="utf-8", errors="replace"))
        text = re.sub(r"(?is)<table\b[^>]*>.*?</table>", lambda m: f"\n\n{html_table(m.group(0))}\n\n", text)
        return [{"locator": "mineru ocr", "text": separate_markdown_tables(text)}]
    return []


def write_plan(build: Path, chosen: list[dict[str, Any]], state: dict[str, Any]) -> None:
    write_json(build / "mineru-plan.json", chosen)
    total = sum(entry["size_mb"] or 0 for entry in chosen)
    lines = ["# MinerU 解析计划", "",
             f"共 {len(chosen)} 个文件，约 {total:.0f} MB。这些文件会上传到 MinerU 云端解析。", "",
             "| 文件 | 大小 MB | 原因 |", "| --- | --- | --- |"]
    lines.extend(f"| {e['display_name']} | {e['size_mb']} | {'；'.join(e['reasons'])} |" for e in chosen)
    lines.extend(["", f"Token 状态：{state['message']}；{state['expiry']['message']}。", "",
                  "同意上传后，在 kb-config.json 里加：",
                  '`"mineru": {"approved": true, "approved_by": "姓名", "approved_at": "YYYY-MM-DD", "scope": "本计划全部文件"}`',
                  "", "已有 `approved_item_ids` 时，只上传其中列出的文件；要扩大范围，把 `mineru-plan.json` 里的 item_id 加进去，并改写 scope。"])
    write_text(build / "mineru-plan.md", "\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan, approve, and run MinerU extraction for a build.")
    parser.add_argument("command", choices=["plan", "run"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--package", action="append", required=True, help="Preparation package; repeat for several")
    parser.add_argument("--config", help="kb-config.json")
    parser.add_argument("--only", action="append", default=[], help="item_id to run; default every planned file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--samples", action="store_true", help="Only the samples of kinds still waiting for a decision: try MinerU before choosing")
    group.add_argument("--missing", action="store_true", help="Also every PDF/PPT in the knowledge base MinerU has not read yet")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not run_lock(Path(args.out).resolve(), f"mineru_ocr.py {args.command}"):
        return 3
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    config = load_config(Path(args.config) if args.config else None)
    package_paths = {}
    for package in args.package:
        root = Path(package).resolve()
        for asset in load_json(root / "审计记录/package-manifest.json")["assets"]:
            if asset.get("path"):
                package_paths[asset["item_id"]] = root / asset["path"]
    routes = load_json(build / "routes.json")
    chosen = candidates(build, package_paths, args.samples, args.missing)
    if args.only:
        unknown = set(args.only) - {entry["item_id"] for entry in chosen}
        if unknown:
            print(json.dumps({"ok": False, "reason": "--only 包含不在候选清单中的编号", "item_ids": sorted(unknown)}, ensure_ascii=False))
            return 2
        chosen = [entry for entry in chosen if entry["item_id"] in args.only]
    state = probe()
    write_plan(build, chosen, state)
    if args.command == "plan":
        print(json.dumps({"planned_files": len(chosen), "plan": str(build / "mineru-plan.md"), "token": state["message"]}, ensure_ascii=False))
        return 0

    ok, reason = approved(config)
    if not ok:
        print(json.dumps({"ok": False, "reason": reason, "plan": str(build / "mineru-plan.md")}, ensure_ascii=False))
        return 2
    allowed_ids = (config.get("mineru") or {}).get("approved_item_ids")
    if allowed_ids is not None and any(entry["item_id"] not in allowed_ids for entry in chosen):
        print(json.dumps({"ok": False, "reason": "本次所选文件超出 MinerU 批准范围，请用 --only 限定"}, ensure_ascii=False))
        return 2
    units_dir, cache = build / OCR_DIRECTORY, build / CACHE_DIRECTORY
    units_dir.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"done": [], "cached": [], "failed": []}
    try:
        api = client_from_env()
    except MinerUError as exc:
        print(json.dumps(failure_report(exc), ensure_ascii=False, indent=2))
        return 2
    for entry in chosen:
        if args.only and entry["item_id"] not in args.only:
            continue
        source = Path(entry["path"])
        key = (routes[entry["item_id"]].get("source_hash") or entry["item_id"])[:32]
        cached = cache / f"{key}.json"
        if cached.is_file():
            units = load_json(cached)
            report["cached"].append(entry["display_name"])
        else:
            try:
                with tempfile.TemporaryDirectory(prefix="mineru-") as temp:
                    extract(api, source, Path(temp), {"model": "vlm", "ocr": True, "table": True, "language": "ch"})
                    units = page_units(Path(temp))
            except MinerUError as exc:
                failure = failure_report(exc)
                report["failed"].append({"file": entry["display_name"], **failure})
                if failure["failure_type"] in {"credential-missing-or-expired", "credential-invalid", "quota-or-rate-limit"}:
                    break  # The next file would fail the same way.
                continue
            write_json(cached, units)
            report["done"].append(entry["display_name"])
        # Pages the model read and MinerU did not stay; MinerU's reading replaces the rest.
        target = units_dir / f"{entry['item_id']}.json"
        previous = load_json(target).get("units", []) if target.is_file() else []
        read = {unit["locator"] for unit in units}
        units = units + [unit for unit in previous if unit.get("engine") == "ai" and unit["locator"] not in read]
        # The hash and date travel with the text, so a copy kept in the vault is a usable cache.
        write_json(target, {"item_id": entry["item_id"], "source": "mineru",
                            "source_hash": routes[entry["item_id"]].get("source_hash", ""),
                            "extracted_at": date.today().isoformat(), "units": units})
    write_json(build / "mineru-report.json", report)
    print(json.dumps({key: len(value) for key, value in report.items()}, ensure_ascii=False))
    for failure in report["failed"][:3]:
        print(f"{failure['file']}: {failure['failure_label']} -> {failure['next_action']}")
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
