#!/usr/bin/env python3
"""Stage-by-stage build navigator: fast Stage-1 auto-routing + D-class pre-triage + 5-stage disk checkpoints.

Designed so a newcomer never has to remember CLI flags or manually inspect every sample/D-class file,
and an AI agent never runs a multi-hour single conversation that loses context:

  Stage 1 (plan / apply-plan): Inspect manifest + quick PDF/PPT layout scan (no extraction/curation).
      Auto-routes regular Word-like PDFs -> MinerU (free) and PPT/catalog/design-heavy PDFs -> Reducto.
      Surfaces all undecided D-class files and unhandled/special files (archives, drawings, media) upfront.
      One confirmation writes sample-decisions.json + d_keep/d_drop/ask_client + MinerU/Reducto approval.
  Stage 2 (extract):  Batch MinerU/Reducto OCR + pipeline through source layer & page-check todo.
  Stage 3 (page_check): Complete all page-check items in one stage, flushing collect every 20-30 pages
      so an interrupted run resumes from the exact remaining pages with zero repeated work.
  Stage 4 (governance): Agent auto-digests 90% of CUS/MAT/ANM and generates BOTH internal-decision HTML
      and customer-confirmation HTML together.
  Stage 5 (curate_publish): Write curation pages & brief card (flushing per topic), independent readthrough,
      publish to vault, and report final unhandled-files inventory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from extract_blocks import (AUDIO_FILES, DESIGN_FILES, PICTURE_FILES, VIDEO_FILES,
                            configured_route, format_pairs, is_catalog)
from kb_common import (BUILD_DIRECTORY, MANIFEST_NAME, TRIAGE_NAME, file_extension,
                       load_config, load_json, load_jsonl, long_path, run_lock,
                       unapproved_d, write_json, write_text)
from mineru_client import probe as probe_mineru
from reducto_client import probe as probe_reducto
from samples import (CONFIRMED, DECISIONS, FILE_PREFIX, METHODS, PAGED_KINDS,
                     kind_of, load_decisions, sync_reducto_primary)


ARCHIVES = {".zip", ".rar", ".7z", ".tar", ".gz"}
PLAN_JSON = "stage1-plan.json"
PLAN_MD = "stage1-plan.md"
STAGE4_MARKER = "stage4-done.json"
# Layout thresholds for quick PyMuPDF inspection (no full text extraction).
LANDSCAPE_RATIO = 1.12
LARGE_IMAGE_SHARE = 0.04
DENSE_DRAWINGS = 80
SHORT_TEXT_CHARS = 250


def count_pptx_slides(path: Path) -> int | None:
    try:
        with zipfile.ZipFile(long_path(path)) as zf:
            slides = [n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
            return len(slides) if slides else None
    except (OSError, zipfile.BadZipFile):
        return None


def inspect_pdf_layout(path: Path) -> dict[str, Any]:
    """Quickly inspect PDF page count and visual/design complexity on up to 6 sampled pages."""
    try:
        import pymupdf as fitz  # PyMuPDF
    except ImportError:
        try:
            import fitz
        except ImportError:
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(long_path(path)))
                return {"pages": len(reader.pages), "landscape_ratio": 0.0, "visual_ratio": 0.0, "inspected": False}
            except Exception:
                return {"pages": None, "landscape_ratio": 0.0, "visual_ratio": 0.0, "inspected": False}

    try:
        doc = fitz.open(str(long_path(path)))
    except Exception as exc:
        return {"pages": None, "landscape_ratio": 0.0, "visual_ratio": 0.0, "inspected": False, "error": str(exc)}

    try:
        total = len(doc)
        if total == 0:
            return {"pages": 0, "landscape_ratio": 0.0, "visual_ratio": 0.0, "inspected": True}
        sample_count = min(total, 6)
        indices = sorted({round(i * (total - 1) / max(sample_count - 1, 1)) for i in range(sample_count)})
        landscape = 0
        visual = 0
        for idx in indices:
            page = doc[idx]
            rect = page.rect
            area = (rect.width * rect.height) or 1.0
            if rect.width > rect.height * LANDSCAPE_RATIO:
                landscape += 1
            text_len = len((page.get_text("text") or "").strip())
            large_imgs = sum(
                1 for img in page.get_image_info()
                if (img["bbox"][2] - img["bbox"][0]) * (img["bbox"][3] - img["bbox"][1]) > area * LARGE_IMAGE_SHARE
            )
            drawings = len(page.get_drawings())
            # Design-heavy page: multiple large images, dense vector design elements, or mixed image + short caption.
            # A portrait full-page scan (1 image, <20 chars) or plain sparse text page stays with MinerU.
            if large_imgs >= 2 or drawings >= DENSE_DRAWINGS or (large_imgs >= 1 and 20 <= text_len < SHORT_TEXT_CHARS):
                visual += 1
        return {
            "pages": total,
            "landscape_ratio": round(landscape / len(indices), 2),
            "visual_ratio": round(visual / len(indices), 2),
            "inspected": True,
        }
    finally:
        doc.close()


def recommend_method(path: Path | None, suffix: str, catalog: bool) -> dict[str, Any]:
    """Recommend mineru vs reducto vs local based on file type and quick layout features."""
    if suffix in {".pptx", ".ppt"}:
        pages = count_pptx_slides(path) if (path and path.is_file() and suffix == ".pptx") else None
        return {"method": "reducto", "pages": pages, "reason": "PPT 排版与图文自由度高，适合 Reducto"}
    if suffix == ".pdf":
        layout = inspect_pdf_layout(path) if (path and path.is_file()) else {"pages": None, "landscape_ratio": 0.0, "visual_ratio": 0.0, "inspected": False}
        if layout.get("error"):
            return {"method": "mineru", "pages": None, "reason": f"PDF 预检异常（{layout['error']}），默认先列 MinerU", "error": layout["error"]}
        if catalog:
            return {"method": "reducto", "pages": layout["pages"], "reason": "产品画册/宣传册 PDF，图文排版复杂，适合 Reducto", "layout": layout}
        if layout["landscape_ratio"] >= 0.4 or layout["visual_ratio"] >= 0.45:
            return {"method": "reducto", "pages": layout["pages"],
                    "reason": f"设计/图文页占比高（横版 {int(layout['landscape_ratio']*100)}%、图文/低字 {int(layout['visual_ratio']*100)}%），适合 Reducto",
                    "layout": layout}
        return {"method": "mineru", "pages": layout["pages"],
                "reason": "版式较规整（类 Word 转 PDF / 规格书），适合 MinerU 免费额度",
                "layout": layout}
    if suffix in PICTURE_FILES:
        return {"method": "ai", "pages": 1, "reason": "单张图片默认 AI 看图读取（不上传云端）"}
    return {"method": "local", "pages": 0, "reason": "本地直接读取（不上传云端）"}


def load_triage_map(packages: list[Path]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for pkg in packages:
        path = pkg / TRIAGE_NAME
        if path.is_file():
            for item in load_json(path).get("items", []):
                found[item["item_id"]] = item
    return found


def build_stage1_plan(packages: list[Path], out: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Scan manifests + quick PDF/PPT layout to produce Stage 1 routing, D-class list, and unhandled inventory."""
    all_assets: dict[str, tuple[Path, dict[str, Any]]] = {}
    raw_assets: dict[str, dict[str, Any]] = {}
    for pkg in packages:
        manifest = load_json(pkg / MANIFEST_NAME)
        for asset in manifest.get("assets", []):
            all_assets[asset["item_id"]] = (pkg, asset)
            raw_assets[asset["item_id"]] = asset

    twins = format_pairs(raw_assets)
    triage_map = load_triage_map(packages)
    mineru_state = probe_mineru()
    reducto_state = probe_reducto()

    routed_files: list[dict[str, Any]] = []
    d_undecided: list[dict[str, Any]] = []
    unhandled: dict[str, list[dict[str, Any]]] = {
        "errors": [],
        "archives": [],
        "d_undecided": [],
        "images_to_read": [],
        "twins_skipped": [],
        "media_kept": [],
        "dropped": [],
    }

    for item_id, (pkg, asset) in sorted(all_assets.items(), key=lambda kv: (kv[1][1].get("path") or kv[0]).casefold()):
        rel = asset.get("path") or asset.get("source_relpath") or asset.get("display_name") or ""
        name = Path(rel).name or asset.get("display_name") or item_id
        suffix = file_extension(name)
        file_path = pkg / rel if rel else None
        route = configured_route(asset, config)
        triage = triage_map.get(item_id, {})
        screening_reason = "；".join(t for t in (triage.get("summary"), triage.get("required_follow_up")) if t)

        decision = asset.get("decision") or route.get("decision") or ""
        if suffix in ARCHIVES:
            entry = {"item_id": item_id, "name": name, "folder": Path(rel).parent.as_posix(),
                     "decision": decision, "reason": screening_reason or "压缩包未自动展开"}
            unhandled["archives"].append(entry)
            if decision == "D" and route.get("reason") == "unresolved_item":
                d_undecided.append({**entry, "kind": "archive", "pages": 0, "recommended_method": "drop",
                                    "hint": "压缩包：建议先确认是否需要解压补筛，否则丢弃 (drop)"})
            continue

        if item_id in twins:
            unhandled["twins_skipped"].append({"item_id": item_id, "name": name, "reason": "已有同名 Word/PPT 原稿，自动跳过 PDF 副本"})
            continue

        if decision == "D" and route.get("reason") == "unresolved_item":
            catalog = is_catalog(asset, route.get("tier", "T2"), config.get("doc_titles", {}))
            rec = recommend_method(file_path, suffix, catalog) if suffix in {".pdf", ".pptx", ".ppt"} else {"method": "local", "pages": 0, "reason": ""}
            if name.startswith("~$") or name == ".DS_Store":
                hint = "系统/Office 临时文件：建议丢弃 (drop)"
            elif suffix in PICTURE_FILES | DESIGN_FILES | VIDEO_FILES | AUDIO_FILES:
                hint = "图片/设计稿/音视频：如属公司资料建议捞回进素材库 (keep)"
            else:
                hint = "文档：AI 需快速初判是否捞回入库 (keep)、丢弃 (drop) 或问客户 (ask_client)"
            item = {
                "item_id": item_id, "name": name, "folder": Path(rel).parent.as_posix(),
                "suffix": suffix or "无扩展名", "pages": rec.get("pages"),
                "recommended_method": rec["method"], "method_reason": rec.get("reason", ""),
                "screening": screening_reason, "hint": hint,
            }
            d_undecided.append(item)
            unhandled["d_undecided"].append(item)
            continue

        if route["route"] == "kb":
            if file_path and not file_path.is_file():
                unhandled["errors"].append({"item_id": item_id, "name": name, "reason": f"初筛包中找不到文件：{rel}"})
                continue
            catalog = is_catalog(asset, route.get("tier", "T2"), config.get("doc_titles", {}))
            kind = kind_of(name, "产品画册" if catalog else "", route.get("tier", ""))
            rec = recommend_method(file_path, suffix, catalog)
            if rec.get("error"):
                unhandled["errors"].append({"item_id": item_id, "name": name, "reason": rec["error"]})
            routed_files.append({
                "item_id": item_id, "name": name, "folder": Path(rel).parent.as_posix(),
                "kind": kind, "suffix": suffix, "method": rec["method"],
                "pages": rec.get("pages"), "reason": rec["reason"],
            })
        elif route.get("reason") == "image_needs_review":
            unhandled["images_to_read"].append({"item_id": item_id, "name": name, "folder": Path(rel).parent.as_posix(),
                                                "reason": "单张图片待识别（默认 AI 看图，不上传云端）"})
        elif route["route"] in {"media", "asset"} or suffix in DESIGN_FILES | VIDEO_FILES | AUDIO_FILES | PICTURE_FILES:
            unhandled["media_kept"].append({"item_id": item_id, "name": name, "folder": Path(rel).parent.as_posix(),
                                            "reason": route.get("reason") or "素材/音视频/设计稿按原件保留"})
        else:
            unhandled["dropped"].append({"item_id": item_id, "name": name, "decision": route.get("decision", ""),
                                         "reason": route.get("reason") or screening_reason or "初筛未入库"})

    mineru_files = [f for f in routed_files if f["method"] == "mineru"]
    reducto_files = [f for f in routed_files if f["method"] == "reducto"]
    local_files = [f for f in routed_files if f["method"] in {"local", "ai", "text"}]

    plan_data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "packages": [str(p) for p in packages],
        "project_basics": {
            "project_id": config.get("project_id", ""),
            "company": config.get("company", ""),
            "websites": config.get("websites", []),
            "owner": config.get("owner", ""),
        },
        "credentials": {
            "mineru_ok": bool(mineru_state.get("ok")),
            "mineru_message": mineru_state.get("expiry", {}).get("message") if mineru_state.get("ok") else mineru_state.get("message", ""),
            "reducto_ok": bool(reducto_state.get("ok")),
            "reducto_message": reducto_state.get("message", ""),
        },
        "summary": {
            "routed_total": len(routed_files),
            "mineru_files": len(mineru_files),
            "mineru_pages": sum(f["pages"] or 0 for f in mineru_files),
            "mineru_unknown_pages": sum(1 for f in mineru_files if f["pages"] is None),
            "reducto_files": len(reducto_files),
            "reducto_pages": sum(f["pages"] or 0 for f in reducto_files),
            "reducto_unknown_pages": sum(1 for f in reducto_files if f["pages"] is None),
            "local_files": len(local_files),
            "d_undecided": len(d_undecided),
            "archives": len(unhandled["archives"]),
            "errors": len(unhandled["errors"]),
        },
        "routed_files": routed_files,
        "d_undecided": d_undecided,
        "unhandled": unhandled,
    }

    build = out / BUILD_DIRECTORY
    build.mkdir(parents=True, exist_ok=True)
    write_json(build / PLAN_JSON, plan_data)
    write_text(build / PLAN_MD, render_stage1_plan_md(plan_data))
    return plan_data


def render_stage1_plan_md(plan: dict[str, Any]) -> str:
    s = plan["summary"]
    cred = plan["credentials"]
    lines = [
        "# 阶段 1：建库预检、智能分流与未处理文件清点",
        "",
        f"生成时间：{plan['generated_at']}（仅预检与算页数，尚未提取正文和整理）。",
        "",
        "## 一、云端引擎分流汇总",
        "",
        f"- **MinerU（免费额度，规整 PDF）**：{s['mineru_files']} 份，共 **{s['mineru_pages']}** 页"
        + (f"（另有 {s['mineru_unknown_pages']} 份页数待定）" if s["mineru_unknown_pages"] else "")
        + f"；本机 Token：{cred['mineru_message']}",
        f"- **Reducto（按页计费，PPT / 画册 / 复杂设计排版）**：{s['reducto_files']} 份，共 **{s['reducto_pages']}** 页"
        + (f"（另有 {s['reducto_unknown_pages']} 份页数待定）" if s["reducto_unknown_pages"] else "")
        + f"；本机 Key：{cred['reducto_message']}",
        f"- **本地直读（Word / Excel / TXT / 单图）**：{s['local_files']} 份（不上传云端）",
    ]
    if s["reducto_files"] and not cred["reducto_ok"]:
        lines.append("- ⚠ **提示**：有文件建议使用 Reducto，但本机未配置 Reducto Key；可先双击 `scripts/setup_reducto.cmd` 配置，或在确认时加 `--fallback-mineru` 全部降级为 MinerU。")
    if plan["routed_files"]:
        lines.extend(["", "| 文件 | 推荐方式 | 页数 | 分流依据 |", "| --- | --- | ---: | --- |"])
        for f in plan["routed_files"]:
            if f["method"] == "local":
                continue
            pg = str(f["pages"]) if f["pages"] is not None else "待定"
            lines.append(f"| {f['name']} | {METHODS.get(f['method'], f['method'])} | {pg} | {f['reason']} |")

    lines.extend(["", "## 二、D 类待初判文件（建议在阶段 1 顺手捞回可用资料）", ""])
    if plan["d_undecided"]:
        lines.append(f"共 **{len(plan['d_undecided'])}** 份未决定的 D 类文件。AI 应先快速初判（`keep` 捞回并计入本轮提取 / `drop` 丢弃 / `ask_client` 进第 4 阶段客户网页），再随分流表一起给用户确认：")
        lines.extend(["", "| item_id | 目录 | 文件名 | 页数 | 若捞回推荐引擎 | 初筛说明 / 提示 |",
                      "| --- | --- | --- | ---: | --- | --- |"])
        for d in plan["d_undecided"]:
            pg = str(d["pages"]) if d.get("pages") is not None else "-"
            note = d.get("screening") or d.get("reason") or d.get("hint", "")
            lines.append(f"| `{d['item_id']}` | {d['folder']} | {d['name']} | {pg} | {d['recommended_method']} | {note} |")
    else:
        lines.append("- 无未决定的 D 类文件。")

    u = plan["unhandled"]
    lines.extend(["", "## 三、未处理 / 特殊文件清点", ""])
    if u["errors"]:
        lines.append(f"- 🔴 **异常/缺失（{len(u['errors'])} 份）**：" + "、".join(f"{e['name']}（{e['reason']}）" for e in u["errors"][:10]))
    if u["archives"]:
        lines.append(f"- 🟡 **压缩包（{len(u['archives'])} 个，未自动展开）**：" + "、".join(a["name"] for a in u["archives"][:10]) + " — 请确认是否需要先解压补筛")
    if u["images_to_read"]:
        lines.append(f"- 🟡 **待识别单张图片（{len(u['images_to_read'])} 张）**：默认由 AI 看图读取")
    if u["media_kept"]:
        lines.append(f"- ⚪ **音视频/设计稿/素材（{len(u['media_kept'])} 份）**：按规则保留原件，不入正文")
    if u["twins_skipped"]:
        lines.append(f"- ⚪ **同名 PDF 副本（{len(u['twins_skipped'])} 份）**：已有 Word/PPT 原稿，自动跳过")
    if u["dropped"]:
        lines.append(f"- ⚪ **初筛已排除（{len(u['dropped'])} 份）**：按初筛决定不入库")
    if not any(u.values()):
        lines.append("- 无未处理或特殊文件。")
    lines.append("")
    return "\n".join(lines)


def apply_stage1_plan(out: Path, config_path: Path, approved_by: str,
                      overrides: dict[str, str], d_decisions_path: Path | None,
                      fallback_mineru: bool, fast_mvp: bool = False) -> dict[str, Any]:
    """One-shot apply: D-class decisions + sample-decisions.json + MinerU/Reducto approvals.

    With fast_mvp=True (快车道极简版), heavy Reducto-routed catalogs/PPTs are temporarily deferred
    into config["deferred_items"] + excluded_items, and page_check is temporarily skipped so a usable
    MVP (website + Word/Excel + regular MinerU PDFs + brief card) can publish in ~15 minutes.
    Run `kb_next.py resume-full` later to bring back deferred files and run full page-check/governance.
    """
    build = out / BUILD_DIRECTORY
    plan_path = build / PLAN_JSON
    if not plan_path.is_file():
        raise SystemExit(f"找不到 {plan_path}，请先运行 kb_next.py plan")
    plan = load_json(plan_path)
    config = load_config(config_path) if config_path.is_file() else {}

    # 1. Apply D-class decisions if provided
    d_applied = {"keep": [], "drop": [], "ask_client": []}
    d_by_id = {d["item_id"]: d for d in plan.get("d_undecided", [])}
    if d_decisions_path and d_decisions_path.is_file():
        raw_text = d_decisions_path.read_text(encoding="utf-8").strip()
        records = json.loads(raw_text) if raw_text.startswith("[") else [json.loads(ln) for ln in raw_text.splitlines() if ln.strip()]
        d_keep = list(config.get("d_keep") or [])
        d_drop = list(config.get("d_drop") or [])
        ask_client = dict(config.get("ask_client") or {})
        for rec in records:
            iid = rec.get("item_id")
            dec = rec.get("decision")
            if not iid or dec not in {"keep", "drop", "ask_client"}:
                continue
            if dec == "keep":
                if iid not in d_keep:
                    d_keep.append(iid)
                d_applied["keep"].append(iid)
            elif dec == "drop":
                if iid not in d_drop:
                    d_drop.append(iid)
                d_applied["drop"].append(iid)
            elif dec == "ask_client":
                ask_client[iid] = rec.get("reason") or "阶段 1 初判需客户确认归属或使用范围"
                d_applied["ask_client"].append(iid)
        config["d_keep"] = d_keep
        config["d_drop"] = d_drop
        config["ask_client"] = ask_client

    # 2. Build sample-decisions.json (all kinds confirmed + per-file method)
    decisions = load_decisions(build)
    kinds_present: set[str] = set()
    used_methods: set[str] = set()
    item_kinds: dict[str, str] = {}
    deferred: dict[str, dict[str, Any]] = {}
    excluded = dict(config.get("excluded_items") or {})

    effective_files = list(plan.get("routed_files", []))
    for iid in d_applied["keep"]:
        d_item = d_by_id.get(iid)
        if d_item and d_item.get("recommended_method") in {"mineru", "reducto", "ai", "local"}:
            suffix = d_item.get("suffix", "")
            kind = kind_of(d_item["name"], "", "")
            effective_files.append({
                "item_id": iid, "name": d_item["name"], "kind": kind,
                "suffix": suffix, "method": d_item["recommended_method"],
                "pages": d_item.get("pages"),
            })

    for entry in effective_files:
        iid = entry["item_id"]
        kind = entry["kind"]
        method = overrides.get(iid, entry["method"])
        if fallback_mineru and method == "reducto":
            method = "mineru"
        if fast_mvp and method == "reducto":
            deferred[iid] = {"name": entry["name"], "kind": kind, "method": "reducto", "pages": entry.get("pages")}
            excluded[iid] = "快车道极简版暂缓，二期补全"
            continue
        kinds_present.add(kind)
        item_kinds[iid] = kind
        if kind in PAGED_KINDS and method in METHODS:
            decisions[f"{FILE_PREFIX}{iid}"] = {"method": method}
            used_methods.add(method)

    for kind in sorted(kinds_present):
        existing = dict(decisions.get(kind) or {})
        existing["status"] = CONFIRMED
        if kind in PAGED_KINDS and "method" not in existing:
            existing["method"] = "reducto" if (kind in {"catalog_pdf", "ppt"} and not fallback_mineru) else "mineru"
        decisions[kind] = existing

    write_json(build / DECISIONS, decisions)
    sync_reducto_primary(build, item_kinds, decisions)

    # 3. Record cloud OCR approvals and optional fast_mvp deferral in kb-config.json
    today = date.today().isoformat()
    if "mineru" in used_methods:
        mineru_cfg = dict(config.get("mineru") or {})
        mineru_cfg.update({"approved": True, "approved_by": approved_by, "approved_at": today, "scope": "阶段1分流计划确认"})
        config["mineru"] = mineru_cfg
    if "reducto" in used_methods:
        reducto_cfg = dict(config.get("reducto") or {})
        reducto_cfg.update({"approved": True, "approved_by": approved_by, "approved_at": today, "scope": "阶段1分流计划确认"})
        config["reducto"] = reducto_cfg

    if fast_mvp:
        config["excluded_items"] = excluded
        config["deferred_items"] = deferred
        config["page_check"] = {"skip": "快车道极简版暂跳过，二期补全"}
        config["build_stage"] = "full"
        write_json(build / STAGE4_MARKER, {"completed_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "fast_mvp": True})

    write_json(config_path, config)
    return {
        "applied": True,
        "fast_mvp": fast_mvp,
        "approved_by": approved_by,
        "confirmed_kinds": sorted(kinds_present),
        "cloud_approvals": sorted(m for m in ("mineru", "reducto") if m in used_methods),
        "deferred_count": len(deferred),
        "deferred_items": list(deferred),
        "d_applied": d_applied,
        "next_stage": "stage2_extract",
    }


def resume_full_from_fast(out: Path, config_path: Path, approved_by: str, fallback_mineru: bool = False) -> dict[str, Any]:
    """Upgrade a fast-MVP vault to full build: restore deferred catalogs/PPTs, re-enable page-check & Stage 4."""
    build = out / BUILD_DIRECTORY
    config = load_config(config_path) if config_path.is_file() else {}
    deferred: dict[str, dict[str, Any]] = dict(config.get("deferred_items") or {})
    excluded = dict(config.get("excluded_items") or {})
    decisions = load_decisions(build)
    item_kinds: dict[str, str] = {}
    used_methods: set[str] = set()

    for iid, info in deferred.items():
        excluded.pop(iid, None)
        kind = info.get("kind", "catalog_pdf")
        method = "mineru" if fallback_mineru else info.get("method", "reducto")
        decisions[f"{FILE_PREFIX}{iid}"] = {"method": method}
        existing = dict(decisions.get(kind) or {})
        existing["status"] = CONFIRMED
        existing.setdefault("method", method)
        decisions[kind] = existing
        item_kinds[iid] = kind
        used_methods.add(method)

    config["excluded_items"] = excluded
    config.pop("deferred_items", None)
    if (config.get("page_check") or {}).get("skip", "").startswith("快车道"):
        config.pop("page_check", None)

    today = date.today().isoformat()
    if "reducto" in used_methods:
        reducto_cfg = dict(config.get("reducto") or {})
        reducto_cfg.update({"approved": True, "approved_by": approved_by, "approved_at": today, "scope": "二期补全暂缓文件"})
        config["reducto"] = reducto_cfg
    if "mineru" in used_methods:
        mineru_cfg = dict(config.get("mineru") or {})
        mineru_cfg.update({"approved": True, "approved_by": approved_by, "approved_at": today, "scope": "二期补全暂缓文件"})
        config["mineru"] = mineru_cfg

    write_json(build / DECISIONS, decisions)
    sync_reducto_primary(build, item_kinds, decisions)
    write_json(config_path, config)
    marker = build / STAGE4_MARKER
    if marker.is_file() and load_json(marker).get("fast_mvp"):
        marker.unlink()

    return {
        "resumed_full": True,
        "restored_count": len(deferred),
        "restored_items": list(deferred),
        "cloud_approvals": sorted(used_methods),
        "next_stage": "stage2_extract",
    }


def detect_status(out: Path, packages: list[Path], config_path: Path | None, vault: Path | None) -> dict[str, Any]:
    """Inspect disk state only and report exact stage, progress counters, and next-chat prompt."""
    build = out / BUILD_DIRECTORY
    config = load_config(config_path) if (config_path and config_path.is_file()) else (
        load_config(build / "kb-config.json") if (build / "kb-config.json").is_file() else {}
    )
    decisions = load_decisions(build)
    plan_exists = (build / PLAN_JSON).is_file()
    confirmed_any = any(isinstance(v, dict) and v.get("status") == CONFIRMED for k, v in decisions.items() if not k.startswith(FILE_PREFIX))

    def next_prompt(stage_label: str) -> str:
        parts = [f"用 compile-enterprise-knowledge 继续推进这个项目（{stage_label}）："]
        for p in packages:
            parts.append(f"- 初筛包：{p}")
        parts.append(f"- 运行目录：{out}")
        if config_path:
            parts.append(f"- 配置文件：{config_path}")
        if vault:
            parts.append(f"- 知识库：{vault}")
        return "\n".join(parts)

    # Stage 1: Need plan or confirmation
    if not confirmed_any and config.get("sample_review", True):
        return {
            "stage": "stage1_plan",
            "stage_name": "阶段 1：预检分流与一次确认",
            "plan_generated": plan_exists,
            "instruction": (
                "先运行 kb_next.py plan（只算页数和自动分流，不提取）；如有未决定的 D 类文件，AI 快速初判建议捞回/丢弃/问客户；"
                "把分流表、总页数、D 类建议和未处理文件清点一次给用户确认，用户同意后运行 kb_next.py apply-plan。"
            ),
            "next_chat_prompt": next_prompt("阶段 2：批量提取"),
        }

    # Stage 2: Need extraction / OCR / pipeline
    docs_path = build / "docs.jsonl"
    routes_path = build / "routes.json"
    excluded = set(config.get("excluded_items") or {})
    mineru_missing = [
        k.removeprefix(FILE_PREFIX)
        for k, v in decisions.items()
        if k.startswith(FILE_PREFIX) and isinstance(v, dict) and v.get("method") == "mineru"
        and k.removeprefix(FILE_PREFIX) not in excluded
        and not (build / "mineru-units" / f"{k.removeprefix(FILE_PREFIX)}.json").is_file()
    ]
    reducto_missing = [
        k.removeprefix(FILE_PREFIX)
        for k, v in decisions.items()
        if k.startswith(FILE_PREFIX) and isinstance(v, dict) and v.get("method") == "reducto"
        and k.removeprefix(FILE_PREFIX) not in excluded
        and not (build / "reducto-units" / f"{k.removeprefix(FILE_PREFIX)}.json").is_file()
    ]
    if not docs_path.is_file() or not routes_path.is_file() or mineru_missing or reducto_missing:
        return {
            "stage": "stage2_extract",
            "stage_name": "阶段 2：批量云端提取与抽块",
            "ocr_pending": {"mineru_missing": mineru_missing, "reducto_missing": reducto_missing},
            "instruction": (
                "按已批准的分流方案运行 mineru_ocr.py run / reducto_ocr.py run（有对应未完成文件才跑），"
                "再运行 run_pipeline.py --images all 生成来源层与 page-check 待办。"
            ),
            "next_chat_prompt": next_prompt("阶段 3：看图核对"),
        }

    # Stage 3: Page-check progress (resumable!)
    todo_path = build / "page-check/todo.jsonl"
    results_path = build / "page-check/results.json"
    pc_skip = bool((config.get("page_check") or {}).get("skip"))
    if todo_path.is_file() and not pc_skip:
        todo_entries = load_jsonl(todo_path)
        todo_keys = {e["key"] for e in todo_entries if e.get("key")}
        results = load_json(results_path) if results_path.is_file() else {}
        done_count = len(todo_keys & set(results))
        left_count = len(todo_keys - set(results))
        if left_count > 0:
            return {
                "stage": "stage3_page_check",
                "stage_name": "阶段 3：看图核对（增量落盘，断点续跑）",
                "page_check": {"total": len(todo_keys), "done": done_count, "remaining": left_count},
                "instruction": (
                    f"当前看图核对进度：{done_count}/{len(todo_keys)} 页已完成，剩余 {left_count} 页。"
                    "请一口气核对完剩余页面（页多时分给子 Agent 并行），但每核对完 20-30 页必须立刻写 answer-*.jsonl 并运行 "
                    "page_check.py collect 落盘，防止中途中断重来！全部完成后重跑 run_pipeline.py，再停下让用户开新对话进入阶段 4。"
                ),
                "next_chat_prompt": next_prompt("阶段 3/4：续跑看图核对或进入待办消化"),
            }

    # Stage 4: Governance auto-digestion + generate BOTH internal & customer HTML webpages
    stage4_done = (build / STAGE4_MARKER).is_file()
    gov_summary = load_json(build / "governance-decisions-summary.json") if (build / "governance-decisions-summary.json").is_file() else {}
    routes = load_json(routes_path)
    rev_dec = load_json(build / "review-decisions.json") if (build / "review-decisions.json").is_file() else {}
    unapproved_d_count = len(unapproved_d(routes, rev_dec))
    if not stage4_done:
        return {
            "stage": "stage4_governance",
            "stage_name": "阶段 4：自动消化待办并生成内部/客户双网页",
            "governance": {
                "internal_open_evidence": gov_summary.get("internal_open_evidence", 0),
                "cus_draft": gov_summary.get("cus_draft", 0),
                "unapproved_d_docs": unapproved_d_count,
            },
            "instruction": (
                "按 SKILL.md 自动消化全部 CUS/MAT/ANM 与待审 D 类文档（run_pipeline.py --from guard 刷新台账），"
                "然后同时导出并生成两份离线 HTML 网页（有对应事项才生成）："
                "1) 内部待办决策网页（governance_state.py review --webpage-json → customer-confirmation-webpage --mode internal）；"
                "2) 客户确认网页（governance_state.py cus-export --webpage-json → customer-confirmation-webpage --mode customer）。"
                "完成后运行 kb_next.py mark-stage4 --out <运行目录>，停下把两份网页路径交给用户，提示开新对话进入阶段 5。"
            ),
            "next_chat_prompt": next_prompt("阶段 5：整理正式知识与发布"),
        }

    # Stage 5: Curation + verify + publish
    topics_path = build / "curation/topics.json"
    topics = load_json(topics_path) if topics_path.is_file() else []
    needed_topics = [t for t in topics if t.get("blocks") and not t.get("skipped")]
    written_slugs = {p.parent.name for p in (build / "curation").glob("*/page.md")} if (build / "curation").is_dir() else set()
    missing_topics = [t["slug"] for t in needed_topics if t["slug"] not in written_slugs]
    verify_ok = bool((load_json(build / "校验结果.json") if (build / "校验结果.json").is_file() else {}).get("verified"))
    receipt_ok = bool(vault and (vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json").is_file())
    build_full = config.get("build_stage", "full") == "full"

    if not build_full or not topics or missing_topics or not verify_ok or (vault and not receipt_ok):
        return {
            "stage": "stage5_curate_publish",
            "stage_name": "阶段 5：整理正式知识、独立通读与发布",
            "curation": {
                "build_stage": config.get("build_stage", "full"),
                "topics_total": len(needed_topics),
                "topics_written": len(needed_topics) - len(missing_topics),
                "topics_remaining": missing_topics,
                "verified": verify_ok,
                "published": receipt_ok,
            },
            "instruction": (
                "把 build_stage 设为 full，按主题逐个写完整理页并落盘（最后写速查卡 brief），跑 curate.py check 和 run_pipeline.py "
                "直到 verified: true；派独立子 Agent 抽样通读；运行 publish_vault.py 发布进知识库，并附上最终未处理/特殊文件清点。"
            ),
            "next_chat_prompt": next_prompt("阶段 5：续跑整理与发布"),
        }

    return {
        "stage": "done",
        "stage_name": "已完成并发布上线",
        "verified": True,
        "published": True,
        "instruction": "知识库已建好发布，可直接接入写作 Skill 写文章，或使用 kb_status.py --vault-root 做月度只读体检。",
        "next_chat_prompt": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="5-stage build navigator: fast Stage-1 routing, D-class rescue, and disk checkpoints.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Stage 1: quick PDF/PPT layout scan, page count, D-class list, unhandled inventory")
    p_plan.add_argument("--package", action="append", required=True, help="Preparation package; repeat for several")
    p_plan.add_argument("--out", required=True, help="Run directory")
    p_plan.add_argument("--config", help="kb-config.json")
    p_plan.add_argument("--json", action="store_true")

    p_apply = sub.add_parser("apply-plan", help="Stage 1 confirm: write D-class decisions, sample-decisions.json, and cloud OCR approvals")
    p_apply.add_argument("--out", required=True, help="Run directory")
    p_apply.add_argument("--config", required=True, help="kb-config.json")
    p_apply.add_argument("--approved-by", required=True, help="Person who approved the Stage 1 plan")
    p_apply.add_argument("--override", action="append", default=[], help="Override one file: <item_id>=<mineru|reducto|text|ai>")
    p_apply.add_argument("--d-decisions", help="Optional JSON/JSONL file with D-class keep/drop/ask_client decisions")
    p_apply.add_argument("--fallback-mineru", action="store_true", help="Downgrade all Reducto-recommended files to MinerU")
    p_apply.add_argument("--fast", action="store_true", help="Fast MVP mode: defer heavy Reducto catalogs/PPTs and skip page-check for quick first publish")

    p_resume = sub.add_parser("resume-full", help="Upgrade a --fast MVP build to full: restore deferred catalogs/PPTs and re-enable page-check")
    p_resume.add_argument("--out", required=True, help="Run directory")
    p_resume.add_argument("--config", required=True, help="kb-config.json")
    p_resume.add_argument("--approved-by", required=True, help="Person approving phase-2 full build")
    p_resume.add_argument("--fallback-mineru", action="store_true", help="Use MinerU instead of Reducto for restored files")

    p_status = sub.add_parser("status", help="Detect exact current stage and remaining progress from disk")
    p_status.add_argument("--out", required=True, help="Run directory")
    p_status.add_argument("--package", action="append", default=[])
    p_status.add_argument("--config")
    p_status.add_argument("--vault")
    p_status.add_argument("--json", action="store_true")

    p_m4 = sub.add_parser("mark-stage4", help="Mark Stage 4 (governance + dual webpages) complete")
    p_m4.add_argument("--out", required=True, help="Run directory")
    p_m4.add_argument("--internal-html", default="", help="Path to generated internal decision HTML (if any)")
    p_m4.add_argument("--customer-html", default="", help="Path to generated customer confirmation HTML (if any)")

    args = parser.parse_args()
    out = Path(args.out).resolve()
    if not run_lock(out, f"kb_next.py {args.command}"):
        return 3

    if args.command == "plan":
        packages = [Path(p).resolve() for p in args.package]
        config = load_config(Path(args.config).resolve()) if args.config else {}
        data = build_stage1_plan(packages, out, config)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(render_stage1_plan_md(data))
        return 0

    if args.command == "apply-plan":
        overrides: dict[str, str] = {}
        for item in args.override:
            if "=" not in item:
                raise SystemExit(f"--override 格式应为 item_id=method，收到：{item}")
            k, v = item.split("=", 1)
            overrides[k.strip()] = v.strip()
        d_path = Path(args.d_decisions).resolve() if args.d_decisions else None
        result = apply_stage1_plan(out, Path(args.config).resolve(), args.approved_by, overrides, d_path, args.fallback_mineru, fast_mvp=args.fast)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "resume-full":
        result = resume_full_from_fast(out, Path(args.config).resolve(), args.approved_by, args.fallback_mineru)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "mark-stage4":
        build = out / BUILD_DIRECTORY
        build.mkdir(parents=True, exist_ok=True)
        payload = {"completed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "internal_html": args.internal_html, "customer_html": args.customer_html}
        write_json(build / STAGE4_MARKER, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    packages = [Path(p).resolve() for p in args.package]
    cfg = Path(args.config).resolve() if args.config else None
    vault = Path(args.vault).resolve() if args.vault else None
    st = detect_status(out, packages, cfg, vault)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
