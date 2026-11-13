#!/usr/bin/env python3
"""Create a project vault with the same layout manage-article-knowledge uses.

Nothing is written until the five basics are shown and the user confirms, the same gate
manage-article-knowledge uses. Existing files are never overwritten, so a later install of
that skill can add its own files to the same project.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from kb_common import SKILL_VERSION, safe_name, write_text


DIRECTORIES = [
    "01_工作台",
    "02_源资料/50_外部调研证据",
    "03_正式知识/10_客户知识/10_公司概述",
    "03_正式知识/10_客户知识/20_产品介绍",
    "03_正式知识/10_客户知识/30_解决方案",
    "03_正式知识/10_客户知识/40_合作案例",
    "03_正式知识/10_客户知识/50_行业知识与洞察/10_企业提供",
    "03_正式知识/10_客户知识/60_FAQ",
    "03_正式知识/10_客户知识/70_其他",
    "03_正式知识/20_外部公共知识/50_行业知识与洞察",
    "03_正式知识/90_素材",
    "04_文章任务/10_进行中",
    "04_文章任务/20_等待终稿",
    "04_文章任务/30_等待Faithfulness",
    "04_文章任务/40_已完成",
    "04_文章任务/90_归档/10_文章历史版本",
    "04_文章任务/90_归档/20_已取消任务",
    "05_数据与审核/10_Faithfulness",
    "05_数据与审核/20_知识库覆盖与缺口",
    "05_数据与审核/30_异常与待决定/10_待客户补充",
    "05_数据与审核/30_异常与待决定/20_源资料处理",
    "05_数据与审核/30_异常与待决定/30_源文与事实异常",
    "05_数据与审核/30_异常与待决定/40_Skill运行反馈",
    "05_数据与审核/40_月度与交接",
    "05_数据与审核/50_运行记录",
    "05_数据与审核/60_版本归档/10_正式知识历史",
    "05_数据与审核/60_版本归档/20_控制文件迁移历史",
    "05_数据与审核/60_版本归档/30_机器文件迁移快照",
    "05_数据与审核/60_版本归档/40_源资料版本记录",
]
PROJECT_ID = re.compile(r"^[A-Z]-\d{3}$")


def project_info(args: argparse.Namespace, project: Path) -> str:
    rows = [
        ("项目ID", args.project_id), ("项目名称", args.name), ("官网", args.website),
        ("客户原始资料路径", str(Path(args.source_root).resolve())), ("净化原文", "库内 `02_源资料/10_净化原文`"),
        ("Obsidian项目路径", str(project)),
        ("内容运营负责人", args.owner), ("知识库构建", f"compile-enterprise-knowledge {SKILL_VERSION}"),
        ("建库日期", date.today().isoformat()),
    ]
    lines = ["# 项目基础信息", ""]
    lines.extend(f"- {label}：{value}" for label, value in rows)
    lines.extend(["", "## 说明", "",
                  "- 客户原始资料路径指向初筛包，它和知识库一起长期保存；净化后的原文在库内 `02_源资料/10_净化原文`。",
                  "- 运行目录可以删掉；需要时用初筛包和知识库重建。",
                  "- `03_正式知识/10_客户知识` 里由脚本生成的文件不要手改；补充内容另建文件，脚本不会碰。"])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a knowledge-base project vault after the user confirms.")
    parser.add_argument("--vault-root", required=True, help="Folder that holds all project vaults")
    parser.add_argument("--project-id", required=True, help="Like P-007")
    parser.add_argument("--name", required=True, help="Company Chinese name, used in the folder name")
    parser.add_argument("--website", required=True)
    parser.add_argument("--owner", required=True, help="Content operations owner")
    parser.add_argument("--source-root", required=True, help="The preparation package, which is kept long term")
    parser.add_argument("--confirm", action="store_true", help="Only after the user replied 确认建库")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not PROJECT_ID.match(args.project_id):
        raise SystemExit("项目ID格式应为 P-007 这样。")
    project = Path(args.vault_root).resolve() / f"{args.project_id}_{safe_name(args.name)}知识库"
    summary = {"项目名称": args.name, "官网": args.website, "客户原始资料路径": str(Path(args.source_root).resolve()),
               "Obsidian知识库统一根目录": str(Path(args.vault_root).resolve()), "内容运营负责人": args.owner,
               "将创建": str(project)}
    if not args.confirm:
        print(json.dumps({"confirmed": False, "basics": summary, "next": "把这五项给用户看，用户回复“确认建库”后加 --confirm 重跑"},
                         ensure_ascii=False, indent=2))
        return 3
    for directory in DIRECTORIES:
        (project / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "01_工作台/10_项目基础信息.md": project_info(args, project),
        "01_工作台/00_知识库总览.md": "# 知识库总览\n\n发布后自动更新。\n",
        "01_工作台/30_版本与变更入口.md": f"# 版本与变更入口\n\n- {date.today().isoformat()}：用 compile-enterprise-knowledge {SKILL_VERSION} 建库。\n",
    }
    created = []
    for relative, text in files.items():
        if not (project / relative).exists():
            write_text(project / relative, text)
            created.append(relative)
    print(json.dumps({"confirmed": True, "project": str(project), "created_files": created}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
