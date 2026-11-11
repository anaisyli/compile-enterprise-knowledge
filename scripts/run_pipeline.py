#!/usr/bin/env python3
"""Run extract, guard, render, and verify in order. Safe to rerun after any change.

MinerU and the website reader run separately, because one uploads and the other goes
online; their results sit in the build folder and are picked up here automatically.
"""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from kb_common import load_json, write_json
from kb_state import STATE, restore


HERE = Path(__file__).resolve().parent
SCREENING_WORKBOOK = "客户确认表.xlsx"


def step(script: str, *arguments: str) -> int:
    print(f"== {script}", flush=True)
    return subprocess.run([sys.executable, str(HERE / script), *arguments]).returncode


def repeat(flag: str, values: list[str]) -> list[str]:
    return [item for value in values for item in (flag, value)]


def report_kept(kept: list[str], limit: int = 5) -> None:
    """Which copies were not replaced. Thirty MinerU files saying the same thing hide the one that matters."""
    kept = sorted(kept, key=lambda p: ("kb-config" not in p and "review-decisions" not in p, p))
    for path in kept[:limit]:
        print(f"   两边都有，按较新的那份保留或逐项合并：{path}")
    if len(kept) > limit:
        print(f"   …另有 {len(kept) - limit} 个文件同样处理（多是 MinerU 识别稿）")


def merge_vault_config(config_path: Path, vault: Path) -> None:
    """Settings that only the vault's copy has (a product list written after publishing, say) go into
    the --config file before anything runs, so every step and the next publish see one config.

    Where both copies have a setting, --config wins: it is the file the person is editing now. To
    remove a setting, give it an empty value; a deleted one comes back from the vault."""
    vault_config = vault / STATE / "kb-config.json"
    if not vault_config.is_file() or not config_path.is_file():
        return
    mine, kept = load_json(config_path), load_json(vault_config)
    if not isinstance(mine, dict) or not isinstance(kept, dict):
        return
    added = sorted(k for k in kept if k not in mine)
    differ = sorted(k for k in kept if k in mine and kept[k] != mine[k])
    if differ:
        print(f"   运行目录的配置和知识库里的不一样，用运行目录的：{'、'.join(differ[:8])}{' 等' if len(differ) > 8 else ''}")
    if not added:
        return
    backup = config_path.with_name(f"{config_path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak.json")
    shutil.copy2(config_path, backup)
    write_json(config_path, {**mine, **{k: kept[k] for k in added}})
    print(f"   知识库的配置里有、运行目录的配置里没有，已补进 {config_path.name}：{'、'.join(added)}（原文件备份为 {backup.name}）")


LOCK_NAME = ".run_pipeline.lock"


def take_lock(run: Path) -> Path | None:
    """One build at a time per run folder: two would overwrite each other's files halfway."""
    run.mkdir(parents=True, exist_ok=True)
    lock = run / LOCK_NAME
    if lock.is_file():
        try:
            pid = int(lock.read_text(encoding="utf-8").split()[0])
        except (ValueError, IndexError, OSError):
            pid = 0
        if pid and pid != os.getpid() and alive(pid):
            print(f"这个运行目录已经有一次建库在跑（进程 {pid}）。等它跑完再来；确定它已经停了，删掉 {lock} 再重跑。")
            return None
    lock.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    return lock


def alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
    parser.add_argument("--images", choices=["none", "xlsx", "all"], default="xlsx")
    parser.add_argument("--max-image-mb", default="5")
    parser.add_argument("--tiers", default="T1,T2", help="Tiers to publish; the rest stay in 待审内容")
    parser.add_argument("--skip-t3-gate", action="store_true", help="Tests only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if take_lock(Path(args.out).resolve()) is None:
        return 3
    if args.vault:
        # Only the package and the vault are kept long term; the run folder is rebuilt from them.
        restored = restore(Path(args.out).resolve(), Path(args.vault).resolve())
        print(f"== 从知识库恢复构建状态：{restored['restored']} 个文件", flush=True)
        report_kept(restored["kept_newer_in_run"])
        build = Path(args.out).resolve() / "04_知识库构建"
        if not args.config and (build / "kb-config.json").is_file():
            args.config = str(build / "kb-config.json")
        elif args.config:
            merge_vault_config(Path(args.config).resolve(), Path(args.vault).resolve())
        if not args.package and (build / "run-info.json").is_file():
            args.package = load_json(build / "run-info.json")["packages"]
            missing = [p for p in args.package if not Path(p).is_dir()]
            if missing:
                # The vault came from another computer: the package is somewhere else here.
                print(f"上次用的初筛包不在这台电脑的原位置：{'；'.join(missing)}。用 --package 指定它现在的位置重建；"
                      "只想让知识库里“打开原件”的链接能点开，运行 kb_relink.py --vault <知识库> --package <初筛包>。")
                return 2
    if not args.package:
        print("要指定 --package，或者用 --vault 从知识库读取上次用的初筛包。")
        return 2
    for package in args.package:
        if not (Path(package) / "审计记录" / "package-manifest.json").is_file():
            print(f"{package} 不是初筛包。没筛选过的资料先用 prepare-seo-knowledge-materials 生成初筛包，再来建库。")
            return 2
    for package in args.package:
        workbook = Path(package) / SCREENING_WORKBOOK
        # The client may have answered screening's questions already; the build should not ask again.
        if workbook.is_file() and args.config:
            if step("apply_answers.py", "--out", args.out, "--config", args.config, "--answers", str(workbook)):
                print(f"   读不了初筛包的客户确认表，跳过：{workbook}")
    # Before anything is read: one sample of each kind of file nobody has confirmed yet.
    gate = ["--out", args.out, *repeat("--package", args.package), *(["--config", args.config] if args.config else [])]
    if step("sample_gate.py", *gate):
        return 1
    if not args.text_dir:
        # Colleagues hand over the package, not the screening work folder, so read it again.
        code = step("extract_package_text.py", *repeat("--package", args.package), "--out", args.out)
        if code:
            return code
        text_dir = Path(args.out).resolve() / "04_知识库构建" / "package-text"
        args.text_dir = [str(text_dir)]
        args.extract_status = args.extract_status or [str(text_dir / "extract-status.json")]
    common = ["--out", args.out, *(["--config", args.config] if args.config else [])]
    ocr = ["--ocr-text-dir", args.ocr_text_dir] if args.ocr_text_dir else []
    sources = [*repeat("--package", args.package), *repeat("--text-dir", args.text_dir)]
    extract = [*sources, *repeat("--extract-status", args.extract_status), *ocr, *common,
               "--images", args.images, "--max-image-mb", args.max_image_mb]
    render = [*common, "--tiers", args.tiers, *(["--skip-t3-gate"] if args.skip_t3_gate else [])]
    stages = [
        ("extract_blocks.py", extract),
        # Pick the pages to look at beside their text; the model's answers come in on the next run.
        ("page_check.py", ["plan", *sources, *common]),
        ("guard_blocks.py", [*repeat("--text-dir", args.text_dir), *common]),
        ("render_kb.py", render),
    ]
    config = load_json(Path(args.config)) if args.config else {}
    if config.get("build_stage") != "sources_only":
        stages.extend([("curate.py", ["prepare", *common]), ("curate.py", ["build", *common])])
    stages.append(("verify_kb.py", [*sources, *ocr, *common]))
    for script, arguments in stages:
        code = step(script, *arguments)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
