#!/usr/bin/env python3
"""Refresh generated navigation and material records without extraction or publication."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from governance import CLOSED_STAGES, SOLVED_STAGE, LEDGERS, merge_ledger, parse_ledger, render_ledger
from kb_common import BUILD_DIRECTORY, CLEAN_DIRECTORY, load_json, load_jsonl, write_json, write_text
from pictures import INDEX, LIST_CSV, PUBLIC, CONFIRMED_MEDIA, retained_media, retained_section

REGISTER = "02_源资料/源资料与可检索性台账.md"
PLAN = "02_源资料/处理计划与样例.md"
SOURCE = "02_源资料/10_净化原文"
RECEIPT = "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
ARCHIVE = "05_数据与审核/60_版本归档/10_正式知识历史"
# Written into the vault by kb_status after each publish, never part of a build.
STATUS_PAGES = ("01_工作台/00_知识库总览.md", "05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md")


def obsidian_prefix(project: Path) -> str:
    """Obsidian resolves full wiki paths from its vault, which can contain many projects."""
    project = project.resolve()
    for root in (project, *project.parents):
        if (root / ".obsidian").is_dir():
            relative = project.relative_to(root).as_posix()
            return "" if relative == "." else relative + "/"
    return ""


class Links:
    def __init__(self, files: dict[str, Path], prefix: str = ""):
        self.files = files
        self.prefix = prefix
        self.names = defaultdict(set)
        self.headings = {}
        for key in files:
            names = {key, key.removesuffix(".md"), prefix + key, prefix + key.removesuffix(".md")}
            if "60_版本归档" not in Path(key).parts and "70_构建状态" not in Path(key).parts:
                names.update({Path(key).name, Path(key).stem})
            for name in names:
                self.names[name.casefold()].add(key)

    def rewrite(self, text: str) -> str:
        def line_rewrite(line: str) -> str:
            def replace(match):
                raw = match[1]
                target, separator, alias = raw.partition("|")
                target = target.strip().rstrip("\\").rstrip()
                dest, fragment, heading = target.partition("#")
                choices = self.names.get(dest.casefold(), set())
                if len(choices) != 1:
                    return match[0]
                key = next(iter(choices))
                label = alias.strip() if separator else Path(dest).name
                if fragment and not heading.startswith("^"):
                    if key not in self.headings:
                        path = self.files[key]
                        self.headings[key] = set(re.findall(r"^#{1,6}\s+(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)) if path.is_file() and path.suffix == ".md" else set()
                    if heading not in self.headings[key]:
                        identity = re.match(r"^([A-Z]-\d{3}-(?:CUS|MAT|ANM)-\d{3})", heading)
                        heading_key = identity[1] if identity else heading
                        expanded = [h for h in self.headings[key] if h.startswith(heading_key + "｜")]
                        if len(expanded) == 1:
                            heading = expanded[0]
                        else:
                            fragment = heading = ""
                            label = (alias if separator else f"{Path(dest).name} · {target.partition('#')[2]}")
                            if "该页未生成段落" not in label:
                                label += "（该页未生成段落，打开资料）"
                destination = self.prefix + key.removesuffix(".md") + (f"#{heading}" if fragment else "")
                delim = "\\|" if line.lstrip().startswith("|") else "|"
                return f"[[{destination}{delim}{label}]]"
            return re.sub(r"\[\[([^\]]+)\]\]", replace, line)
        return "\n".join(line_rewrite(line) for line in text.splitlines()) + "\n"


def build_files(run: Path) -> dict[str, Path]:
    build = run / BUILD_DIRECTORY
    files = {p.relative_to(build / "vault").as_posix(): p for p in (build / "vault").rglob("*") if p.is_file()}
    files.update({f"{SOURCE}/{p.relative_to(run / CLEAN_DIRECTORY).as_posix()}": p
                  for p in (run / CLEAN_DIRECTORY).rglob("*.md")})
    for image_id, entry in load_json(build / "pictures.json").items():
        files[f"03_正式知识/90_素材/{entry['file']}"] = build / "assets" / image_id
    # kb_status writes these into the vault after each publish; known up front, links to them
    # resolve the same on the first build as on every later one.
    for key in STATUS_PAGES:
        files.setdefault(key, build / "vault" / key)
    return files


def normalize_build(run: Path, project: Path | None = None) -> None:
    files = build_files(run)
    links = Links(files, obsidian_prefix(project) if project else "")
    for key, path in files.items():
        if path.suffix == ".md" and path.is_file():
            write_text(path, links.rewrite(path.read_text(encoding="utf-8")))


def material_texts(build: Path, index: str, table: str) -> tuple[str, str]:
    from guard_blocks import compile_terms
    terms = load_json(build / "guard-terms.json") if (build / "guard-terms.json").is_file() else {}
    forbidden = compile_terms(terms.get("hard", []) + terms.get("soft", []))
    entries = load_json(build / "pictures.json")
    records = retained_media(build, entries, forbidden)
    head = index.partition("## 留在初筛包里的素材")[0].rstrip()
    index = head + "\n\n" + "\n".join(retained_section(records, lambda s: str(s).replace("|", "\\|"))).rstrip() + "\n"
    reader = csv.DictReader(io.StringIO(table))
    rows = [r for r in reader if r["编号"] in entries]
    for r in records:
        rows.append(dict(zip(reader.fieldnames, [r["name"], "", PUBLIC if r["status"] == CONFIRMED_MEDIA else r["status"], "", "原文件名",
                    "初筛包原文件（没有复制进知识库）", "", r["entry"]["item_id"], r["kind"], r["size"], r["folder"], r["status"], r["note"]])))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return index, output.getvalue()


def deduplicate_term_record(records: list[dict], build: Path) -> list[dict]:
    """Recount an older risk table from cached blocks using the queue's case-insensitive key."""
    from governance import link, short
    records = json.loads(json.dumps(records))
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = load_json(build / "published.json")
    terms = {}
    for block in load_jsonl(build / "blocks.jsonl"):
        doc = docs.get(block["doc_id"])
        if not doc or block["doc_id"] not in published or doc["tier"] not in {"T1", "T2"} or block["status"] not in {"kept", "flagged"}:
            continue
        for flag in block.get("flags", []):
            if flag["type"] == "unverified_name":
                for term in flag["terms"]:
                    entry = terms.setdefault(term.casefold(), {"label": term, "sources": set()})
                    entry["sources"].add(link(doc))
    for record in records:
        if "个没核实过的词" not in record["row"][0]:
            continue
        record["row"][0] = f"{len(terms)} 个没核实过的词出现在已生效资料里"
        record["row"][1] = f"{len(terms)} 个词"
        first = next((i for i, line in enumerate(record["details"]) if line.startswith("| 词 |")), len(record["details"]))
        record["details"] = record["details"][:first] + ["- 统计按大小写不敏感去重，同一个词只算一次。", "", "| 词 | 出现在 |", "| --- | --- |"]
        for term, entry in sorted(terms.items(), key=lambda kv: (-len(kv[1]["sources"]), kv[0])):
            sources = sorted(entry["sources"])
            record["details"].append(f"| {short(entry['label'], 30)} | {'、'.join(sources[:3])}{' 等' if len(sources) > 3 else ''} |")
    return records


def refresh(run: Path, vault: Path, apply: bool = False, recover: bool = False, all_links: bool = False) -> dict:
    from kb_status import OVERVIEW, COVERAGE, gather, overview, coverage
    from publish_vault import normalized_hash
    build = run / BUILD_DIRECTORY
    receipt = load_json(vault / RECEIPT)
    keys = [REGISTER, PLAN, INDEX, LIST_CSV, *(r[0] for r in LEDGERS.values())]
    for i, key in enumerate(keys):
        side = vault / Path(key).with_name(Path(key).name.replace("01_", "02_知识库构建_", 1))
        if key in {r[0] for r in LEDGERS.values()} and side.is_file():
            keys[i] = side.relative_to(vault).as_posix()
    texts = {key: (vault / key).read_text(encoding="utf-8-sig") for key in keys if (vault / key).is_file()}
    if all_links:
        for key in receipt["files"]:
            if key.endswith(".md") and (vault / key).is_file():
                texts.setdefault(key, (vault / key).read_text(encoding="utf-8-sig"))
    # Rebuild only machine parts of ledgers; merge preserves human stages, owners and notes.
    machine = load_json(build / "governance.json")
    config = load_json(build / "kb-config.json")
    for kind, (key, title, purpose) in LEDGERS.items():
        ledger_key = next((k for k in texts if Path(k).parent == Path(key).parent), None)
        if ledger_key:
            details = {match[1]: match[2].split("#### 处理记录", 1)[0].strip().splitlines()
                       for match in re.finditer(r"^###\s+([A-Z]-\d{3}-[A-Z]{3}-\d{3})｜[^\n]*\n([\s\S]*?)(?=^### |\Z)", texts[ledger_key], re.M)}
            records = [{**r, "details": details.get(r["id"], [])} for r in machine.get(kind, [])]
            # Older builds counted spelling/case variants as different risk terms.
            if kind == "ANM":
                records = deduplicate_term_record(records, build)
            current = render_ledger(kind, title, purpose, config.get("project_id", "P-000"),
                                    datetime.now().strftime("%Y-%m-%d"), records)
            texts[ledger_key] = merge_ledger(texts[ledger_key], current)
    recovered = {}
    if recover:
        for kind, (key, _, _) in LEDGERS.items():
            if key not in texts or parse_ledger(texts[key])[1]:
                continue
            machine = build / "vault" / key
            if not machine.is_file() or parse_ledger(machine.read_text(encoding="utf-8"))[1]:
                continue
            for old in sorted((vault / ARCHIVE).glob(f"*/{key}"), reverse=True):
                old_text = old.read_text(encoding="utf-8")
                if parse_ledger(old_text)[1]:
                    texts[key] = merge_ledger(old_text, texts[key])
                    history = old.relative_to(vault).as_posix().removesuffix(".md")
                    texts[key] = texts[key].replace("## 当前事项", "已不再登记的旧事项保留为已解决历史，不代表需要重新询问客户。"
                        f"原始详情见 [[{history}|确认前的台账]]。\n\n## 当前事项", 1)
                    recovered[kind] = len(parse_ledger(texts[key])[1])
                    break
    texts[INDEX], texts[LIST_CSV] = material_texts(build, texts[INDEX], texts[LIST_CSV])
    state = gather(run, vault)
    for kind, (key, _, _) in LEDGERS.items():
        text = texts.get(key) or next((t for k, t in texts.items() if Path(k).parent == Path(key).parent), "")
        header, rows, _ = parse_ledger(text)
        stage = header.index("当前阶段") if "当前阶段" in header else None
        closed = sum(stage is not None and r[stage] in CLOSED_STAGES - {SOLVED_STAGE} for r in rows.values())
        solved = sum(stage is not None and r[stage] == SOLVED_STAGE for r in rows.values())
        state["ledger_counts"][kind] = {"pending": len(rows) - closed - solved, "resolved": closed, "system_solved": solved}
    texts[OVERVIEW.as_posix()] = "\n".join(overview(state)) + "\n"
    texts[COVERAGE.as_posix()] = "\n".join(coverage(state, run)) + "\n"
    files = {p.relative_to(vault).as_posix(): p for p in vault.rglob("*") if p.is_file()}
    prefix = obsidian_prefix(vault)
    links = Links(files, prefix)
    links.headings.update({k: set(re.findall(r"^#{1,6}\s+(.+?)\s*$", t, re.M)) for k, t in texts.items() if k.endswith(".md")})
    texts = {k: links.rewrite(t) if k.endswith(".md") else t for k, t in texts.items()}
    changed = [k for k, text in texts.items() if text.replace("\r\n", "\n") != (vault / k).read_text(encoding="utf-8-sig")]
    # Preflight all pages before writing any page. Ledgers and sample plans allow human edits.
    for key in changed:
        if key in receipt["files"] and "30_异常与待决定/" not in key and key != PLAN:
            if normalized_hash(vault / key) != receipt["files"][key]:
                raise SystemExit(f"文件有人修改，停止维护，不覆盖：{key}")
        elif key not in receipt["files"] and "<!-- compile-enterprise-knowledge -->" not in (vault / key).read_text(encoding="utf-8"):
            raise SystemExit(f"无法确认是生成页，不覆盖：{key}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-navigation"
    result = {"applied": apply, "changed": changed, "recovered_resolved_history": recovered,
              "obsidian_project_prefix": prefix,
              "ledger_counts": state["ledger_counts"], "pictures": state["picture_count"], "retained": state["retained_count"]}
    if apply:
        for key in changed:
            backup = vault / ARCHIVE / stamp / key
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault / key, backup)
            encoding = "utf-8-sig" if key.endswith(".csv") else "utf-8"
            (vault / key).write_text(texts[key], encoding=encoding, newline="")
        for key, text in texts.items():
            if key in STATUS_PAGES:
                continue  # Vault pages only: a build must stay what a rebuild would make.
            destination = run / CLEAN_DIRECTORY / Path(key).relative_to(SOURCE) if key.startswith(SOURCE + "/") else build / "vault" / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8-sig" if key.endswith(".csv") else "utf-8", newline="")
            receipt["files"][key] = normalized_hash(vault / key)
        write_text(build / "总览.md", texts[OVERVIEW.as_posix()])
        receipt["navigation_refreshed_at"] = stamp
        write_json(vault / RECEIPT, receipt)
        if changed:
            write_text(vault / ARCHIVE / stamp / "00_版本说明.md", "# 导航与素材记录维护\n\n"
                       "只维护生成页及台账历史，未提取、未发布。以下文件的旧版已备份：\n\n"
                       + "\n".join(f"- {key}" for key in changed) + "\n")
        write_json(build / "navigation-refresh.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--apply", action="store_true", help="Write after a read-only preview")
    parser.add_argument("--recover-ledger-history", action="store_true", help="Recover vanished rows as resolved history from the latest archive")
    parser.add_argument("--all-links", action="store_true", help="Repair links in every receipt-managed Markdown page")
    args = parser.parse_args()
    print(json.dumps(refresh(Path(args.out), Path(args.vault), args.apply, args.recover_ledger_history, args.all_links), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
