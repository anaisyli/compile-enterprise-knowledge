#!/usr/bin/env python3
"""Decide, before anything is read, which files this build reads: one sample of each kind of file
that is not confirmed yet, and every file of the kinds that are. See samples.py.

Writes 04_知识库构建/sample-gate.json; extract_package_text, extract_blocks, ai_read and
mineru_ocr follow it. With "sample_review": false in the config there is no gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from extract_blocks import configured_route, package_title
from kb_common import BUILD_DIRECTORY, MANIFEST_NAME, load_config, load_json
from samples import TYPES, gate, kind_of, load_decisions, save_gate


def entries_of(packages: list[Path], config: dict) -> list[dict]:
    """Every file the build would read, with its kind: documents to extract and pictures to read."""
    found = []
    for package in packages:
        for asset in load_json(package / MANIFEST_NAME)["assets"]:
            route = configured_route(asset, config)
            if not (route["route"] == "kb" or route.get("reason") == "image_needs_review"):
                continue
            _, folder = package_title(asset, route.get("tier", "T2"), config.get("doc_titles", {}))
            name = asset.get("display_name") or asset.get("path") or ""
            found.append({"item_id": asset["item_id"], "kind": kind_of(name, folder), "bytes": int(asset.get("byte_size") or 0),
                          "folder": str(Path(asset.get("path") or "").parent.as_posix())})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Choose the sample files of this build.")
    parser.add_argument("--package", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    config = load_config(Path(args.config) if args.config else None)
    config.setdefault("client_answers", {})
    build = Path(args.out).resolve() / BUILD_DIRECTORY
    build.mkdir(parents=True, exist_ok=True)
    result = gate(entries_of([Path(p).resolve() for p in args.package], config), config, load_decisions(build))
    save_gate(build, result)
    waiting = {TYPES[k][0]: v["waiting"] for k, v in result.get("kinds", {}).items() if v["waiting"]}
    print(json.dumps({"sample_review": bool(result), "read_now": len(result.get("allowed", [])),
                      "waiting_for_confirmation": waiting}, ensure_ascii=False))
    if waiting:
        print("这次每类只提取一份样例。把《处理计划与样例》和样例页给用户看，确认后再重跑，其余文件才会提取。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
