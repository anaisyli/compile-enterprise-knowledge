#!/usr/bin/env python3
"""Run extract, guard, render, and verify in order. Safe to rerun after any change.

MinerU and the website reader run separately, because one uploads and the other goes
online; their results sit in the build folder and are picked up here automatically.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from kb_common import load_json
from kb_state import restore


HERE = Path(__file__).resolve().parent


def step(script: str, *arguments: str) -> int:
    print(f"== {script}", flush=True)
    return subprocess.run([sys.executable, str(HERE / script), *arguments]).returncode


def repeat(flag: str, values: list[str]) -> list[str]:
    return [item for value in values for item in (flag, value)]


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
    if args.vault:
        # Only the package and the vault are kept long term; the run folder is rebuilt from them.
        restored = restore(Path(args.out).resolve(), Path(args.vault).resolve())
        print(f"== 从知识库恢复构建状态：{restored['restored']} 个文件", flush=True)
        for path in restored["kept_newer_in_run"]:
            print(f"   运行目录里的版本较新，保留不覆盖：{path}")
        build = Path(args.out).resolve() / "04_知识库构建"
        if not args.config and (build / "kb-config.json").is_file():
            args.config = str(build / "kb-config.json")
        if not args.package and (build / "run-info.json").is_file():
            args.package = load_json(build / "run-info.json")["packages"]
    if not args.package:
        print("要指定 --package，或者用 --vault 从知识库读取上次用的初筛包。")
        return 2
    for package in args.package:
        if not (Path(package) / "审计记录" / "package-manifest.json").is_file():
            print(f"{package} 不是初筛包。没筛选过的资料先用 prepare-seo-knowledge-materials 生成初筛包，再来建库。")
            return 2
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
    for script, arguments in (
        ("extract_blocks.py", extract),
        ("guard_blocks.py", [*repeat("--text-dir", args.text_dir), *common]),
        ("render_kb.py", render),
        ("curate.py", ["prepare", *common]),
        ("curate.py", ["build", *common]),
        ("verify_kb.py", [*sources, *ocr, *common]),
    ):
        code = step(script, *arguments)
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
