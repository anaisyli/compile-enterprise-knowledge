#!/usr/bin/env python3
"""Render 02_源资料/处理计划与样例.md and the optional platform comparison pages.

Extracted from render_kb.py so sample-plan and platform-comparison reporting live apart from
source-page and ledger rendering.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from kb_common import load_json, load_jsonl, path_uri, remaining_ocr_pages, safe_name, write_json, write_text
from samples import (
    FILE_METHODS_HEADING,
    GRADES,
    HELD_KINDS,
    IMAGE_KIND,
    METHOD_HELP,
    METHODS,
    PAGED_KINDS,
    PLAN_PAGE,
    STRUCTURE_PREFIX,
    TYPES,
    UNREADABLE_DEFAULT,
    UNREADABLE_KIND,
    choose_samples,
    doc_type,
    is_confirmed,
    load_decisions,
    load_gate,
    load_triage,
    method_of,
    status_of,
)
from source_fidelity import literal_dimensions


NAME_MAP_PAGE = "审计记录/知识库文件对照.md"
MARKER_PLAN = "<!-- compile-enterprise-knowledge plan -->"
DECISION_LABELS = {"A": "A 原文保留", "B": "B 待清理", "C": "C 提取知识点", "D": "D 待定", "E": "E 排除", "web": "官网页面"}


def escape_cell(text: str) -> str:
    return literal_dimensions(str(text).replace("|", "\\|").replace("\n", " "))


def squeeze_key(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def check_group(result: dict[str, Any]) -> str:
    """The heading a page-check result belongs under, the same one extract_blocks gives the page."""
    unit, number = result["unit"], result["page"]
    return {"slide": f"第 {number} 张幻灯片", "image": f"图片 {number}"}.get(unit, f"第 {number} 页")


def write_platform_comparison(run: Path, build: Path, docs: dict[str, dict[str, Any]], config: dict[str, Any],
                              originals: dict[str, str]) -> None:
    """Write platform-only page transcripts for explicitly selected comparison files.

    They never mix the native PDF text layer into either platform's output.
    """
    scope = config.get("pdf_comparison") or {}
    if not scope:
        return
    root = run / "平台提取对比"
    lines = ["# PDF 平台提取对比", "", "产品 PDF 按文件分组，各平台 5 份；培训 PDF 全部使用 MinerU。", "",
             "以下为平台逐页返回的提取初稿，未混入 PDF 原生文字层，供与原件对照。看图核对结果及补充文字见净化原文；本目录保留平台初稿，不作为已核实知识使用。",
             "原稿保留原件内容；正式净化原文另按知识库规则清理。不同文件分组用于观察效果，不代表同一文件的准确率对照。", "",
             "| 文件 | 用途 | 提取平台 | 原件页数 | 返回正文页数 | 平台提取稿 |", "| --- | --- | --- | --- | --- | --- |"]
    records = []
    by_item = {d["item_id"]: d for d in docs.values()}
    groups = [("MinerU", "mineru-units", scope.get("mineru_product_ids", []), "产品"),
              ("Reducto", "reducto-units", scope.get("reducto_product_ids", []), "产品"),
              ("MinerU", "mineru-units", scope.get("mineru_training_ids", []), "培训")]
    for platform, folder, item_ids, purpose in groups:
        for item_id in item_ids:
            doc = by_item.get(item_id)
            if not doc:
                continue
            path = build / folder / f"{item_id}.json"
            record = load_json(path) if path.is_file() else {}
            valid = record.get("source_hash") == doc.get("source_sha256") and record.get("source") == platform.lower()
            units = [u for u in record.get("units", []) if u.get("engine") != "ai"] if valid else []
            pages = {int(m.group(1)): u["text"] for u in units
                     if (m := re.fullmatch(r"page (\d+) ocr", u["locator"]))}
            relative = f"{platform}/{safe_name(doc['title'])}.md"
            body = [f"# {doc['title']}", "", f"> 提取平台：**{platform}**", f"> 用途：{purpose} PDF 提取效果对比",
                    f"> 资料编号：`{item_id}`", f"> 原件 SHA-256：`{doc['source_sha256']}`",
                    "> 状态：平台返回初稿；核对补充和原件疑点见对应净化原文，本稿保留平台原样结果，不混入原生文字层。"]
            if originals.get(doc["doc_id"]):
                body.append(f"> [打开原件]({originals[doc['doc_id']]})")
            for number in range(1, int(doc.get("pages") or max(pages, default=0)) + 1):
                body.extend(["", f"## 第 {number} 页 · {platform}", "", pages.get(number) or "> 平台未返回本页正文，需对照原件。", ""])
            for unit in units:
                if not re.fullmatch(r"page \d+ ocr", unit["locator"]):
                    body.extend(["", f"## {unit['locator']} · {platform}", "", unit["text"], ""])
            write_text(root / relative, "\n".join(body) + "\n")
            lines.append(f"| {doc['title']} | {purpose} | {platform} | {doc.get('pages') or ''} | {sum(bool(t.strip()) for t in pages.values())} | [{platform} 提取稿]({relative}) |")
            records.append({"item_id": item_id, "platform": platform, "purpose": purpose, "source_hash": doc["source_sha256"],
                            "expected_pages": doc.get("pages"), "text_pages": sorted(n for n, t in pages.items() if t.strip()),
                            "record_matches_source": valid, "file": relative})
    write_text(root / "00_对比目录.md", "\n".join(lines) + "\n")
    write_json(root / "comparison-manifest.json", records)


def sample_findings(build: Path, sample: dict[str, Any], done: dict[str, Any], status_info: dict[str, Any]) -> str:
    """What the sample showed about its kind, in numbers, and the way of reading it that suggests."""
    item = sample["item_id"]
    pages = int(sample.get("pages") or 0)
    unread = remaining_ocr_pages(build, item, status_info.get(item, {}).get("ocr_pages") or [])
    looked = [r for r in done.values() if r["item_id"] == item]
    added = sum(1 for r in looked if r.get("added"))
    wrong = sum(1 for r in looked if r.get("text_errors"))
    unclear = sum(1 for r in looked if r.get("unclear"))
    parts = [f"共 {pages} 页" if pages else ""]
    graded = load_triage(build).get("files", {}).get(item)
    if graded:
        parts.append("分级 " + "、".join(f"{g} {n}" for g, n in graded["counts"].items() if n))
    if unread:
        parts.append(f"{len(unread)} 页没有文字层")
    if looked:
        parts.append(f"对照原图看了 {len(looked)} 页：{added} 页图里有字文字层没有，{wrong} 页文字层有错" + (f"，{unclear} 页看不出款号对应" if unclear else ""))
    else:
        parts.append("还没对照原图看（先做看图核对，才看得出文字层够不够）")
    read_path = build / "mineru-units" / f"{item}.json"
    reviewed_path = build / 'reviewed-pages.json'
    reviewed = load_json(reviewed_path).get(item, []) if reviewed_path.is_file() else []
    if (build / 'mineru-drafts' / item / 'provenance.json').is_file() and reviewed:
        parts.append(f"本样例已用 MinerU 初稿 + Agent 对照原页复核，共 {len(reviewed)} 页；原始初稿及核对依据已保存")
    if read_path.is_file() and load_json(read_path).get("source") == "mineru":
        native: dict[str, str] = defaultdict(str)
        record = load_json(read_path).get("units", [])
        text_path = build / "package-text" / f"{item}.json"
        for unit in load_json(text_path).get("units", []) if text_path.is_file() else []:
            native[re.sub(r" (?:col|table) \d+$", "", unit["locator"])] += squeeze_key(unit["text"])
        fuller = sum(1 for u in record if u["locator"].endswith(" ocr") and u.get("engine") != "ai"
                     and any(squeeze_key(line) and squeeze_key(line) not in native[u["locator"][:-4]] for line in u["text"].splitlines()))
        parts.append(f"MinerU 试读：{fuller} 页读出了文字层没有的内容")
    reducto_path = build / "reducto-units" / f"{item}.json"
    reducto = load_json(reducto_path) if reducto_path.is_file() else {}
    if reducto.get("pages_read"):
        conflicts = graded["counts"]["D"] if graded else 0
        parts.append(f"Reducto 试读：读了 {len(reducto['pages_read'])} 页" + (f"，{conflicts} 页和其他来源对不上（D 级）" if conflicts else ""))
    tried_mineru = read_path.is_file() and load_json(read_path).get("source") == "mineru"
    tried_reducto = bool(reducto.get("pages_read"))
    if tried_mineru and tried_reducto:
        advice = (f"建议：对照两边的净化原文选一个：{METHODS['mineru']}（免费额度），或 {METHODS['reducto']}"
                  "（按页计费，复杂表格、扫描件通常更好）")
    elif tried_mineru:
        advice = (f"建议：{METHODS['mineru']}；MinerU 读得不好（表格乱、漏字多）再试读 Reducto"
                  "（reducto_ocr.py plan --samples）")
    elif tried_reducto:
        advice = f"建议：{METHODS['reducto']}；想省钱可以再试读 MinerU（mineru_ocr.py plan --samples）对比"
    else:
        advice = ("建议：先试读 MinerU（mineru_ocr.py plan --samples），读得不好再试读 Reducto；"
                  "不想上传就选 AI 看图复核")
    return "；".join(p for p in parts if p) + (f"。{advice}" if advice else "")


def write_plan_page(build: Path, routes: dict[str, Any], docs: dict[str, dict[str, Any]], published: dict[str, str],
                    statuses: dict[str, str], originals: dict[str, str], config: dict[str, Any], status_info: dict[str, Any],
                    reason_label_fn: Callable[[str], str] | None = None) -> list[str]:
    """02_源资料/处理计划与样例: every kind of material, what is done to it, one example, and whether
    a person has confirmed the example. Returns the kinds still waiting for confirmation."""
    label_reason = reason_label_fn or (lambda r: r)
    entered = [docs[d] for d in published]
    decisions = load_decisions(build)
    samples = choose_samples(entered, config, build)
    gate = load_gate(build).get("kinds", {})
    check_dir = build / "page-check"
    todo = load_jsonl(check_dir / "todo.jsonl")
    done = load_json(check_dir / "results.json") if (check_dir / "results.json").is_file() else {}
    deferred = (load_json(check_dir / "plan.json") if (check_dir / "plan.json").is_file() else {}).get("deferred", {})
    by_item = {d["item_id"]: d for d in entered}
    checks: Counter = Counter()
    for entry in list(todo) + list(done.values()):
        if entry["item_id"] in by_item:
            checks[doc_type(by_item[entry["item_id"]])] += 1
    maps = sorted({path_uri(Path(r["package"]) / NAME_MAP_PAGE) for r in routes.values() if r.get("package")})
    lines = ["# 处理计划与样例", "", MARKER_PLAN,
             "每类文件先只提取一份样例给人看，确认后再批量提取同类。还没确认的类，这次只提取了样例那一份，其余的在“要做的事”里写着“确认后提取”。", "",
             "- 怎么确认：在下表“确认状态”一列写“已确认”，或者写“要改：……”说明怎么改；也可以直接告诉 AI。下次建库会读回来，不会被覆盖。",
             "- 怎么选处理方式（PDF、PPT）：先看“样例情况”，再在“处理方式”一列写下面几种之一，和确认一起生效：",
             *[f"  - **{name}**：{METHOD_HELP[key]}" for key, name in METHODS.items()],
             "  - 想先看 MinerU 或 Reducto 读样例的效果再选，跟 AI 说“样例试读 MinerU”或“样例试读 Reducto”（要批准上传，只传样例）。",
             "  - 个别文件要和同类不一样，在最下面“按文件指定处理方式”里改那一行的“处理方式”，或者告诉 AI。",
             "- 页面分级（PDF、PPT 每页一级，处理方式决定每级怎么处理）：",
             *[f"  - **{grade} {label}**：{rule}" for grade, (label, rule) in GRADES.items()],
             "- 原文件：点“打开原件”直接打开初筛包里的原文件。原名对照：" + "、".join(f"[打开对照表]({uri})" for uri in maps), ""]
    lines.extend(["## 按资料类型", "", "| 类型 | 份数 | 在初筛包哪里 | 怎么提取 | 样例 | 样例情况 | 要做的事 | 处理方式 | 确认状态 |",
                  "| --- | ---: | --- | --- | --- | --- | --- | --- | --- |"])
    waiting: list[str] = []
    for kind, (label, how) in TYPES.items():
        members = [d for d in entered if doc_type(d) == kind]
        info = gate.get(kind, {})
        if not members and not info:
            continue
        folders = Counter(str(Path(d.get("package_path") or "").parent.as_posix()) if not str(d.get("package_path", "")).startswith("http")
                          else "官网" for d in members)
        if info.get("folders"):
            folders = Counter(info["folders"])
        where = "、".join(f"{f}（{n} 份）" for f, n in folders.most_common(3)) + (" 等" if len(folders) > 3 else "")
        todo_text = []
        if info.get("waiting"):
            todo_text.append(f"确认后提取其余 {info['waiting']} 份")
        roles = Counter("报价单" if d.get("kind") == "quotation" else "产品表" if d.get("tier") == "T3" else
                        "参考整理稿（不当公司事实）" if d.get("role") == "reference" else "" for d in members)
        found_roles = "；".join(f"其中 {n} 份是{r}" for r, n in roles.items() if r)
        if checks[kind] or deferred.get(kind):
            todo_text.append(f"对照原图核对 {checks[kind]} 页" + (f"，确认后再核对其余 {deferred[kind]} 页" if deferred.get(kind) else ""))
        ocr = sum(len(remaining_ocr_pages(build, d["item_id"], status_info.get(d["item_id"], {}).get("ocr_pages") or [])) for d in members)
        if ocr:
            todo_text.append(f"{ocr} 页图片要 MinerU 或 AI 读图")
        sample = docs.get(samples.get(kind, ""))
        sample_text = ""
        if sample:
            stem = Path(sample["file_name"]).stem
            sample_text = f"[[{stem}]]" + (f" · [打开原件]({originals[sample['doc_id']]})" if sample["doc_id"] in originals else "")
        status = status_of(kind, decisions)
        if not is_confirmed(kind, decisions, config):
            waiting.append(label)
        count = info.get("total", len(members))
        how_text = how + (f"。{found_roles}" if found_roles else "")
        seen = sample_findings(build, sample, done, status_info) if sample and kind in PAGED_KINDS else "—"
        chosen = (decisions.get(kind) or {}).get("method")
        method = (METHODS[method_of(kind, decisions)] + ("" if chosen else "（默认）")) if kind in PAGED_KINDS else "—"
        lines.append(f"| {label} | {count} | {escape_cell(where)} | {how_text} | {sample_text} | {escape_cell(seen)} "
                     f"| {'；'.join(todo_text) or '—'} | {method} | {status} |")
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in done.values():
        if result.get("structure") and result["item_id"] in by_item:
            found[result["structure"]].append(result)
    if found:
        lines.extend(["", "## 特殊结构", "", "看图核对时认出的表格、流程图、时间轴、地图、图表。每种先看样例，确认整理方式后再统一处理。", "",
                      "| 结构 | 出现几处 | 出现在 | 样例 | 确认状态 |", "| --- | ---: | --- | --- | --- |"])
        for name, results in sorted(found.items()):
            links = [f"[[{Path(by_item[r['item_id']]['file_name']).stem}#{check_group(r)}|{by_item[r['item_id']]['title']} · {check_group(r)}]]"
                     for r in results]
            key = f"{STRUCTURE_PREFIX}{name}"
            if not is_confirmed(key, decisions, config):
                waiting.append(f"结构：{name}")
            lines.append(f"| {name} | {len(results)} | {'、'.join(links[:3])}{' 等' if len(links) > 3 else ''} | {links[0]} | {status_of(key, decisions)} |")
    held = Counter((r.get("decision") or "", label_reason(r["reason"])) for r in routes.values()
                   if r["route"] in {"hold", "skip"} and r["reason"] != "awaiting_sample")
    lines.extend(["", "## 不处理的资料", "", "| 初筛类别 | 原因 | 份数 |", "| --- | --- | ---: |"])
    lines.extend(f"| {DECISION_LABELS.get(code, code)} | {reason} | {count} |" for (code, reason), count in sorted(held.items()))
    graded = {i: f for i, f in load_triage(build).get("files", {}).items() if i in by_item or f.get("kind") in HELD_KINDS}
    if graded:
        lines.extend(["", FILE_METHODS_HEADING, "",
                      "已提取的 PDF、PPT 每份一行，按 C、B 级页多少排。要和同类不一样，就把“处理方式”改成上面几种之一；"
                      "写着“（随类型）”的跟着上表这一类走。下次建库读回来。", "",
                      f"类型是“{UNREADABLE_KIND}”的是整份没有文字层的文件（扫描件、全是图的 PPT），每页都是 C 级；"
                      f"没指定时用 {METHODS[UNREADABLE_DEFAULT]}（免费额度），要用 Reducto 就把这一行改成“{METHODS['reducto']}”，按页计费。", "",
                      f"类型是“{IMAGE_KIND}”的是还没识别的单张图片，算 1 页 C 级；没指定时 AI 看图（不上传）。"
                      f"图上字多、表格多的可以改成“{METHODS['reducto']}”，每张按 1 页计费；MinerU 不接单张图片。", "",
                      "| 文件 | 编号 | 类型 | A 文字页 | B 图文页 | C 难页 | D 冲突页 | 处理方式 |",
                      "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |"])
        for item_id, info in sorted(graded.items(), key=lambda pair: (-pair[1]["counts"]["C"] - pair[1]["counts"]["B"], pair[1]["name"])):
            counts = info["counts"]
            follows = "（默认）" if info["kind"] in HELD_KINDS else "（随类型）"
            label = "AI 看图" if info["kind"] == IMAGE_KIND and info["method"] == "ai" else METHODS[info["method"]]
            method = label + ("" if info["method_from"] == "文件" else follows)
            name = Path(by_item[item_id]["file_name"]).stem if item_id in by_item else Path(info["name"]).stem
            kind = info["kind"] if info["kind"] in HELD_KINDS else TYPES.get(info["kind"], ("—",))[0]
            lines.append(f"| {escape_cell(name)} | {item_id} | {kind} "
                         f"| {counts['A']} | {counts['B']} | {counts['C']} | {counts['D']} | {method} |")
    write_text(build / "vault" / PLAN_PAGE, "\n".join(lines) + "\n")
    return waiting
