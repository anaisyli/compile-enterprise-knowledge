#!/usr/bin/env python3
"""Run extract, guard, render, and verify in order. Safe to rerun after any change.

MinerU and the website reader run separately, because one uploads and the other goes
online; their results sit in the build folder and are picked up here automatically.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from build_progress import (PIPELINE_CHECKPOINT, VERIFY_REPORT, batch_id, curation_inputs,
                            extracted_inputs, file_digest, output_digests, package_digests,
                            package_input_digests, routing_inputs)
from kb_common import (BUILD_DIRECTORY, LOCK_HOLDER, SITE_VIEW_ROOT, load_config, load_json, run_lock, sha256_file,
                       site_view_unset, write_json)
from kb_state import STATE, config_base, json_object, record_adopted, restore


HERE = Path(__file__).resolve().parent
SCREENING_WORKBOOK = "客户确认表.xlsx"


def step(script: str, *arguments: str) -> int:
    print(f"== {script}", flush=True)
    # The steps take the same run lock; this pid in the environment lets them pass as ours.
    env = {**os.environ, LOCK_HOLDER: str(os.getpid())}
    return subprocess.run([sys.executable, str(HERE / script), *arguments], env=env).returncode


def repeat(flag: str, values: list[str]) -> list[str]:
    return [item for value in values for item in (flag, value)]


def report_kept(kept: list[str], limit: int = 5) -> None:
    """Which copies were not replaced. Thirty MinerU files saying the same thing hide the one that matters."""
    kept = sorted(kept, key=lambda p: ("kb-config" not in p and "review-decisions" not in p, p))
    for path in kept[:limit]:
        print(f"   两边都有，按较新的那份保留或逐项合并：{path}")
    if len(kept) > limit:
        print(f"   …另有 {len(kept) - limit} 个文件同样处理（多是 MinerU 识别稿）")


def merge_vault_config(config_path: Path, vault: Path, base: dict | None = None,
                       record: Path | None = None) -> None:
    """Settings that only the vault's copy has (a product list written after publishing, say) go into
    the --config file before anything runs, so every step and the next publish see one config.

    Where both copies have a setting, --config wins: it is the file the person is editing now.
    A setting only the vault has is added unless it still equals the agreed value in the
    vault-base record (`base`, or the record next to --config for two-argument callers): then
    the run deleted it on purpose and it is not added back. Added settings are noted in the
    record (`record`) so deleting them later sticks. With no record — a deleted or different
    run folder — every vault-only setting is added, so to remove one then, write an empty value."""
    vault_config = vault / STATE / "kb-config.json"
    if not vault_config.is_file() or not config_path.is_file():
        return
    mine, kept = load_json(config_path), load_json(vault_config)
    if not isinstance(mine, dict) or not isinstance(kept, dict):
        return
    if base is None and record is None:
        candidate = config_base(config_path.parent)
        if candidate.is_file():
            record = candidate
    if base is None and record is not None:
        base = json_object(record)
    added, deleted = [], []
    for key in sorted(k for k in kept if k not in mine):
        if base is not None and key in base and base[key] == kept[key]:
            deleted.append(key)
        else:
            added.append(key)
    differ = sorted(k for k in kept if k in mine and kept[k] != mine[k])
    if differ:
        print(f"   运行目录的配置和知识库里的不一样，用运行目录的：{'、'.join(differ[:8])}{' 等' if len(differ) > 8 else ''}")
    if deleted:
        print(f"   运行目录的配置删掉了：{'、'.join(deleted)}（不从知识库补回，下次发布时知识库那份也删掉）")
    if not added:
        return
    backup = config_path.with_name(f"{config_path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak.json")
    shutil.copy2(config_path, backup)
    merged_config = {**mine, **{k: kept[k] for k in added}}
    write_json(config_path, merged_config)
    print(f"   知识库的配置里有、运行目录的配置里没有，已补进 {config_path.name}：{'、'.join(added)}（原文件备份为 {backup.name}）")
    if record is not None:
        record_adopted(record, mine, kept, merged_config)


def sync_config(out: Path, vault: Path, config_path: Path | None = None) -> dict:
    """Restore the run folder's state from the vault, then merge vault-only settings into --config.

    The base record is read before restore notes the vault values the run adopts, so
    merge_vault_config still sees them as vault-side additions, not run-side deletions."""
    build = out / BUILD_DIRECTORY
    base_before = json_object(config_base(build))
    restored = restore(out, vault)
    if config_path is not None:
        merge_vault_config(config_path, vault, base_before, config_base(build))
    return restored


def keep_site_view_open(config_path: Path, vault: Path) -> None:
    """A project already using 80_建站资料 keeps it: the view is off by default only for projects
    that never had one. Written into --config once, so later runs and the vault agree."""
    if not config_path.is_file() or not (vault / SITE_VIEW_ROOT).is_dir():
        return
    config = load_json(config_path)
    if not isinstance(config, dict) or not site_view_unset(config):
        return
    config["site_view"] = {**(config.get("site_view") or {}), "enabled": True}
    write_json(config_path, config)
    print(f"   知识库里已有 80_建站资料，配置里没写 site_view：保持打开，已在 {config_path.name} 写入 "
          '"site_view": {"enabled": true}；不要了就改成 false，下次发布时旧页面移进归档')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a knowledge base from one or more preparation packages.")
    parser.add_argument("--package", action="append", default=[], help="Verified preparation package; repeat. With --vault, defaults to the packages last used")
    parser.add_argument("--text-dir", action="append", default=[],
                        help="Optional: text already extracted by prepare-seo-knowledge-materials. Without it, text is read from the packages")
    parser.add_argument("--extract-status", action="append", default=[], help="extract-status.json; repeat")
    parser.add_argument("--ocr-text-dir", help="OCR units; defaults to 04_知识库构建/mineru-units")
    parser.add_argument("--out", required=True, help="Run directory outside the packages and the vault")
    parser.add_argument("--config", help="kb-config.json; with --vault, defaults to the copy kept in the vault")
    parser.add_argument("--vault", help="Published project: restore config, decisions, pages, MinerU text and the website snapshot first")
    parser.add_argument("--from", dest="start_from", choices=["extract", "guard", "curate"], default="extract",
                        help="Incremental start stage: extract (default, reuses frozen extractions), guard (after CUS/D-review/terms), or curate (formal knowledge pages only)")
    parser.add_argument("--re-extract", action="append", default=[],
                        help="Item IDs to force re-extract even when frozen cached extractions exist; repeat")
    parser.add_argument("--images", choices=["none", "xlsx", "all"], default=None,
                        help="Image extraction mode (with --vault, defaults to the last published setting; otherwise xlsx)")
    parser.add_argument("--max-image-mb", default=None)
    parser.add_argument("--tiers", default=None, help="Tiers to publish; with --vault defaults to last published setting, otherwise T1,T2")
    parser.add_argument("--skip-t3-gate", action="store_true", help="Tests only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not run_lock(Path(args.out).resolve(), "run_pipeline.py"):
        return 3
    out = Path(args.out).resolve()
    build = out / BUILD_DIRECTORY
    checkpoint_path = build / PIPELINE_CHECKPOINT
    previous = load_json(checkpoint_path) if checkpoint_path.is_file() else {}
    checkpoint_path.unlink(missing_ok=True)
    if args.vault:
        # Only the package and the vault are kept long term; the run folder is rebuilt from them.
        vault = Path(args.vault).resolve()
        published_checkpoint = vault / STATE / PIPELINE_CHECKPOINT
        if (not previous or "build_id" not in previous) and published_checkpoint.is_file():
            previous = load_json(published_checkpoint)
        restored = sync_config(out, vault, Path(args.config).resolve() if args.config else None)
        # Stages and notes people wrote in the published ledgers become decisions before anything is rebuilt.
        from governance_state import sync as sync_decisions
        sync_decisions(build, vault)
        print(f"== 从知识库恢复构建状态：{restored['restored']} 个文件", flush=True)
        report_kept(restored["kept_newer_in_run"])
        for line in restored["removed"]:
            print(f"   {line}")
        if not args.config and (build / "kb-config.json").is_file():
            args.config = str(build / "kb-config.json")
        if args.config:
            keep_site_view_open(Path(args.config).resolve(), vault)
        if (build / "run-info.json").is_file():
            info = load_json(build / "run-info.json")
            if not args.package and info.get("packages"):
                args.package = info["packages"]
                missing = [p for p in args.package if not Path(p).is_dir()]
                if missing:
                    # The vault came from another computer: the package is somewhere else here.
                    print(f"上次用的初筛包不在这台电脑的原位置：{'；'.join(missing)}。用 --package 指定它现在的位置重建；"
                          "只想让知识库里“打开原件”的链接能点开，运行 kb_relink.py --vault <知识库> --package <初筛包>。")
                    return 2
            if args.images is None and info.get("images"):
                args.images = info["images"]
            if args.max_image_mb is None and info.get("max_image_mb"):
                args.max_image_mb = str(info["max_image_mb"])
            if args.tiers is None and info.get("tiers"):
                args.tiers = str(info["tiers"])
    if not args.config:
        # Reuse the run directory's saved config when resuming without --vault.
        # Otherwise later stages would silently build with defaults while the
        # publish step could still pick up the saved config.
        previous_config = Path(previous["config_path"]) if previous.get("config_path") else None
        candidates = [path for path in (out / "kb-config.json", build / "kb-config.json") if path.is_file()]
        if previous_config and previous_config.is_file():
            candidates.append(previous_config)
        saved_config = max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
        if saved_config is not None:
            args.config = str(saved_config)
    args.images = args.images or "xlsx"
    args.max_image_mb = args.max_image_mb or "5"
    args.tiers = args.tiers or "T1,T2"
    if not args.package:
        print("要指定 --package，或者用 --vault 从知识库读取上次用的初筛包。")
        return 2
    for package in args.package:
        if not (Path(package) / "审计记录" / "package-manifest.json").is_file():
            print(f"{package} 不是初筛包。没筛选过的资料先用 prepare-seo-knowledge-materials 生成初筛包，再来建库。")
            return 2
    (build / VERIFY_REPORT).unlink(missing_ok=True)
    for package in args.package:
        workbook = Path(package) / SCREENING_WORKBOOK
        # The client may have answered screening's questions already; the build should not ask again.
        if workbook.is_file() and args.config:
            if step("apply_answers.py", "--out", args.out, "--config", args.config, "--answers", str(workbook)):
                print(f"   读不了初筛包的客户确认表，跳过：{workbook}")
    # sync_config and apply_answers may legitimately rewrite the config; from here on a change
    # means someone edited it by hand mid-build, and the result would mix two configs.
    config_path = Path(args.config).resolve() if args.config else None
    config_hash = sha256_file(config_path) if config_path and config_path.is_file() else None
    config = load_config(config_path if config_hash else None)
    packages = package_digests([Path(path) for path in args.package])
    package_inputs = package_input_digests([Path(path) for path in args.package])
    mineru_dir = Path(args.ocr_text_dir).resolve() if args.ocr_text_dir else None
    source_inputs = extracted_inputs(build, mineru_dir)
    route_inputs = routing_inputs(build, config)
    if args.start_from != "extract" and previous and (
        previous.get("packages") != packages
        or previous.get("package_inputs") != package_inputs
        or previous.get("routing") != route_inputs
        or previous.get("source_inputs") != source_inputs
    ):
        changed = []
        if previous.get("packages") != packages:
            changed.append("初筛包清单")
        if previous.get("package_inputs") != package_inputs:
            changed.append("初筛包文件")
        if previous.get("routing") != route_inputs:
            changed.append("资料去向/表格设置")
        if previous.get("source_inputs") != source_inputs:
            changed.append("OCR/看图核对")
        print(f"{'、'.join(changed)}已变，改跑完整流水线并复用已有提取稿。")
        args.start_from = "extract"

    def config_changed(script: str) -> bool:
        if config_path is None:
            return False
        now = sha256_file(config_path) if config_path.is_file() else None
        if now == config_hash:
            return False
        print(f"kb-config.json 在建库过程中被改过（{script} 之后发现）：这次结果混用了改动前后的配置，已停下。改完后重跑一次。")
        return True

    text_dir = build / "package-text"
    if args.start_from == "extract":
        # Before anything is read: one sample of each kind of file nobody has confirmed yet.
        gate = ["--out", args.out, *repeat("--package", args.package), *(["--config", args.config] if args.config else [])]
        code = step("sample_gate.py", *gate)
        if config_changed("sample_gate.py"):
            return 5
        if code:
            return 1
        source_inputs = extracted_inputs(build, mineru_dir)
        route_inputs = routing_inputs(build, config)
        if not args.text_dir:
            # Colleagues hand over the package, not the screening work folder; unchanged items reuse frozen cache.
            re_ext = repeat("--re-extract", args.re_extract)
            code = step("extract_package_text.py", *repeat("--package", args.package), "--out", args.out, *re_ext)
            if config_changed("extract_package_text.py"):
                return 5
            if code:
                return code
            args.text_dir = [str(text_dir)]
            args.extract_status = args.extract_status or [str(text_dir / "extract-status.json")]
    else:
        required = ["docs.jsonl", "blocks-raw.jsonl"] if args.start_from == "guard" else ["docs.jsonl", "blocks.jsonl", "statuses.json"]
        missing_cache = [name for name in required if not (build / name).is_file()]
        if missing_cache:
            print(f"--from {args.start_from} 需要已冻结的来源层数据，但缺少：{'、'.join(missing_cache)}。请先去掉 --from 完整跑一次。")
            return 2
        if not args.text_dir and text_dir.is_dir():
            args.text_dir = [str(text_dir)]
            args.extract_status = args.extract_status or ([str(text_dir / "extract-status.json")] if (text_dir / "extract-status.json").is_file() else [])

    common = ["--out", args.out, *(["--config", args.config] if args.config else [])]
    ocr = ["--ocr-text-dir", args.ocr_text_dir] if args.ocr_text_dir else []
    sources = [*repeat("--package", args.package), *repeat("--text-dir", args.text_dir)]
    extract = [*sources, *repeat("--extract-status", args.extract_status), *ocr, *common,
               "--images", args.images, "--max-image-mb", args.max_image_mb]
    render = [*common, "--tiers", args.tiers, *(["--skip-t3-gate"] if args.skip_t3_gate else [])]
    stages: list[tuple[str, list[str]]] = []
    if args.start_from == "extract":
        stages.extend([
            ("extract_blocks.py", extract),
            # Pick the pages to look at beside their text; the model's answers come in on the next run.
            ("page_check.py", ["plan", *sources, *common]),
        ])
    if args.start_from in {"extract", "guard"}:
        stages.extend([
            ("guard_blocks.py", [*repeat("--text-dir", args.text_dir), *common]),
            ("render_kb.py", render),
        ])
    elif not (build / "vault" / "02_源资料").is_dir():
        # Fresh run folder with --from curate: render the frozen source layer once so verify_kb / publish_vault have it.
        stages.append(("render_kb.py", render))
    if config.get("build_stage") != "sources_only":
        stages.extend([("curate.py", ["prepare", *common]), ("curate.py", ["build", *common])])
    stages.append(("verify_kb.py", [*sources, *ocr, *common]))
    checkpoint = {
        "batch_id": batch_id(config),
        "build_stage": config.get("build_stage", "full"),
        "packages": packages,
        "package_inputs": package_inputs,
        "routing": route_inputs,
        "source_inputs": source_inputs,
        "config": config_hash,
        "config_path": str(config_path) if config_hash else None,
        "mineru_dir": str(mineru_dir) if mineru_dir else None,
        "normalization_vault": str(Path(args.vault).resolve()) if args.vault else None,
    }
    verification_snapshots: dict[str, object] | None = None
    for script, arguments in stages:
        if script == "verify_kb.py" and args.vault:
            from refresh_navigation import normalize_build
            normalize_build(out, Path(args.vault).resolve())
        if script == "verify_kb.py":
            if package_input_digests([Path(path) for path in args.package]) != checkpoint["package_inputs"]:
                print("初筛包输入在构建期间发生变化；这次结果作废，请重跑 run_pipeline.py。")
                return 5
            current_config = load_config(config_path if config_path and config_path.is_file() else None)
            checkpoint["batch_id"] = batch_id(current_config)
            checkpoint["routing"] = routing_inputs(build, current_config)
            checkpoint["source_inputs"] = extracted_inputs(build, mineru_dir)
            checkpoint["curation"] = curation_inputs(build)
            checkpoint["validated_files"] = output_digests(out, checkpoint["build_stage"])
            verification_snapshots = {
                "routing": checkpoint["routing"],
                "source_inputs": checkpoint["source_inputs"],
                "package_inputs": checkpoint["package_inputs"],
                "curation": checkpoint["curation"],
                "validated_files": checkpoint["validated_files"],
            }
            write_json(checkpoint_path, checkpoint)
        code = step(script, *arguments)
        if config_changed(script):
            return 5
        if code:
            return code
        if script == "verify_kb.py" and checkpoint["source_inputs"] != extracted_inputs(build, mineru_dir):
            print("OCR 或看图核对文件在校验期间发生变化；这次校验结果作废，请重跑 run_pipeline.py。")
            return 5
        if script == "verify_kb.py" and verification_snapshots is not None:
            final_config = load_config(config_path if config_path and config_path.is_file() else None)
            current = {
                "routing": routing_inputs(build, final_config),
                "source_inputs": extracted_inputs(build, mineru_dir),
                "package_inputs": package_input_digests([Path(path) for path in args.package]),
                "curation": curation_inputs(build),
                "validated_files": output_digests(out, checkpoint["build_stage"]),
            }
            labels = {"routing": "资料分流", "source_inputs": "OCR/看图核对", "package_inputs": "初筛包输入",
                      "curation": "整理页原稿", "validated_files": "发布文件"}
            changed = [labels[key] for key, value in verification_snapshots.items() if current[key] != value]
            changed += ["初筛包清单"] if package_digests([Path(path) for path in args.package]) != checkpoint["packages"] else []
            changed += ["授权批次"] if batch_id(final_config) != checkpoint["batch_id"] else []
            if changed:
                print(f"校验期间输入或发布文件发生变化（{'、'.join(changed)}）；这次校验结果作废，请重跑 run_pipeline.py。")
                return 5
        if script == "page_check.py":
            # A mostly scanned package may fail verification until OCR fills its source layer.
            checkpoint["source_inputs"] = extracted_inputs(build, mineru_dir)
            write_json(checkpoint_path, checkpoint)
    if (build / "run-info.json").is_file():
        info = load_json(build / "run-info.json")
        info.update({"images": args.images, "max_image_mb": args.max_image_mb, "tiers": args.tiers})
        write_json(build / "run-info.json", info)
    if config_hash and config_path != build / "kb-config.json":
        build.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path, build / "kb-config.json")
    build_config_hash = file_digest(build / "kb-config.json")
    if build_config_hash != config_hash:
        print("运行目录保存的 kb-config.json 与本次校验使用的配置不一致；这次结果作废，请检查配置后重跑。")
        return 5
    checkpoint["effective_config"] = build_config_hash
    write_json(checkpoint_path, {
        **checkpoint,
        "build_id": uuid4().hex,
        "curation": curation_inputs(build),
        "report": file_digest(build / VERIFY_REPORT),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
