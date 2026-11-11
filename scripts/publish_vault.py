#!/usr/bin/env python3
"""Publish a verified build into a project vault made by kb_project.py.

Files written here are listed in a receipt, so a rerun replaces only its own output. A
file that changes is archived first. A content change a person made stops the publish, but
the spacing Obsidian's table formatter adds does not. Ledgers are merged, keeping what
people wrote in them. Pictures and 已失效 records stay for good. The state a rebuild needs
is saved into the vault, so the run folder can be deleted afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from governance import merge_ledger
from kb_common import BUILD_DIRECTORY, CLEAN_DIRECTORY, SKILL_VERSION, frontmatter, load_json, parse_frontmatter, write_json, write_text
from kb_index import build as build_index
from kb_state import save as save_state
from samples import PENDING, PLAN_PAGE, STRUCTURE_PREFIX, TYPES, load_decisions, parse_method, save_decisions


SOURCE = Path("02_源资料/10_净化原文")
ASSETS = Path("03_正式知识/90_素材")
ARCHIVE = Path("05_数据与审核/60_版本归档/10_正式知识历史")
RECEIPT = Path("05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json")
RUN_LOG = Path("05_数据与审核/50_运行记录/知识库构建记录.jsonl")
PROJECT_INFO = Path("01_工作台/10_项目基础信息.md")
OLD_TODO = Path("01_工作台/20_当前待办.md")
OLD_REGISTER = Path("02_源资料/知识库资料台账.md")
MARKER = "<!-- compile-enterprise-knowledge -->"
LEDGER_ROOT = "05_数据与审核/30_异常与待决定/"
TABLE_RULE = re.compile(r"^:?-{3,}:?$")
HASH_MODE = "normalized-v1"
# More 已失效 records than this in one publish usually means missing input, not retired documents.
WITHDRAW_LIMIT = 5
WITHDRAW_SHARE = 0.2


def normalized_hash(path: Path) -> str:
    """Hash of the content, ignoring spacing, blank lines, and table alignment."""
    data = path.read_bytes()
    if path.suffix not in {".md", ".json", ".jsonl", ".csv"}:
        return hashlib.sha256(data).hexdigest()
    lines = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            stripped = "|" + "|".join("---" if TABLE_RULE.match(cell) else cell for cell in cells) + "|"
        else:
            stripped = re.sub(r"\s+", " ", stripped)
        if stripped:
            lines.append(stripped)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def planned_files(run: Path) -> dict[str, Path]:
    build, clean = run / BUILD_DIRECTORY, run / CLEAN_DIRECTORY
    files: dict[str, Path] = {}
    for path in sorted(p for p in clean.rglob("*") if p.is_file()):
        files[(SOURCE / path.relative_to(clean)).as_posix()] = path
    vault = build / "vault"
    for path in sorted(p for p in vault.rglob("*") if p.is_file()):
        files[path.relative_to(vault).as_posix()] = path
    # Images of removed rows were extracted too; only the pictures the build named may leave.
    for picture_id, entry in sorted(pictures_of(build).items()):
        if (build / "assets" / picture_id).is_file():
            files[(ASSETS / entry["file"]).as_posix()] = build / "assets" / picture_id
    return files


def pictures_of(build: Path) -> dict[str, dict[str, str]]:
    return load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}


def move_pictures(vault: Path, build: Path, previous: dict[str, str], renamed: list[str]) -> dict[str, str]:
    """Pictures published under their old numbered names move to their new names; links in the
    pages people wrote follow. Returns old file name -> new file name."""
    moved: dict[str, str] = {}
    pictures = pictures_of(build)
    for key in sorted(previous):
        path = Path(key)
        if not key.startswith(ASSETS.as_posix() + "/") or path.name not in pictures:
            continue
        new_key = (ASSETS / pictures[path.name]["file"]).as_posix()
        if new_key == key or not (vault / key).is_file():
            continue
        if not (vault / new_key).exists():
            (vault / new_key).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(vault / key), vault / new_key)
            previous[new_key] = previous.pop(key)
            moved[path.name] = Path(new_key).name
            renamed.append(f"{key} → {new_key}")
    return moved


def follow_picture_links(vault: Path, moved: dict[str, str], ours_now: set[str]) -> int:
    """Obsidian finds a picture by its file name, so a renamed one is relinked by name."""
    if not moved:
        return 0
    pattern = re.compile(r"(?<![\w.-])(" + "|".join(map(re.escape, sorted(moved, key=len, reverse=True))) + r")(?![\w.-])")
    changed = 0
    for page in vault.rglob("*.md"):
        key = page.relative_to(vault).as_posix()
        if key in ours_now or key.startswith(ARCHIVE.as_posix()):
            continue  # Pages this publish writes already carry the new names.
        text = page.read_text(encoding="utf-8")
        new = pattern.sub(lambda m: moved[m.group(1)], text)
        if new != text:
            page.write_text(new, encoding="utf-8", newline="\n")
            changed += 1
    return changed


def ours(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return "本 Skill 自动登记" in text or "compile-enterprise-knowledge 自动登记" in text


def empty_ledger(path: Path) -> bool:
    """Another tool's ledger template with no entries in it yet."""
    return not re.search(r"(?m)^\|\s*(?:P-\d+-)?[A-Z]{2,5}-\d+", path.read_text(encoding="utf-8"))


def side_by_side(key: str) -> str:
    path = Path(key)
    return (path.parent / f"02_知识库构建_{path.name.split('_', 1)[-1]}").as_posix()


def tombstone(old_text: str, stamp: str) -> str:
    fields = parse_frontmatter(old_text)
    keep = {key: fields.get(key, "") for key in ("doc_id", "title", "tier", "module", "source_item_id")}
    keep.update(layer="来源层", visibility="withdrawn", uses=[], status="已失效", withdrawn_at=stamp,
                generated_by=f"compile-enterprise-knowledge {SKILL_VERSION}")
    return "\n".join([frontmatter(keep), "", f"# {keep['title']}", "",
                      "> 本资料在最近一次构建中不再出现（来源被移除、合并为同一份，或没有可入库的内容）。",
                      "> 内容不再使用，旧版本见 05_数据与审核/60_版本归档。", ""])


def archive(vault: Path, key: str, stamp: str, archived: list[str], move: bool = False) -> None:
    target, backup = vault / key, vault / ARCHIVE / stamp / key
    backup.parent.mkdir(parents=True, exist_ok=True)
    (shutil.move if move else shutil.copy2)(str(target), backup)
    archived.append(key)


def update_project_info(vault: Path, packages: list[str]) -> None:
    """Point at what is kept long term: the package, and the cleaned text inside the vault."""
    path = vault / PROJECT_INFO
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    source = "；".join(packages) if packages else "（未记录）"
    text = re.sub(r"(?m)^- 客户原始资料路径：.*$", lambda _: f"- 客户原始资料路径：{source}", text)
    if "- 净化原文：" not in text:
        text = re.sub(r"(?m)^(- 客户原始资料路径：.*)$", lambda m: m.group(1) + "\n- 净化原文：库内 `02_源资料/10_净化原文`", text, count=1)
    if "- 原名对照：" not in text:
        # The vault uses new names only; the package keeps which original each one is.
        text = re.sub(r"(?m)^(- 净化原文：.*)$", lambda m: m.group(1) + "\n- 原名对照：初筛包里的 `审计记录/知识库文件对照.md`，"
                      "知识库名字和原文件名一一对应", text, count=1)
    path.write_text(text, encoding="utf-8", newline="\n")


def merge_plan(current: str, new: str) -> tuple[str, dict[str, dict[str, str]]]:
    """Keep the 确认状态 and 处理方式 a person wrote in the plan page; return
    (page, {kind or structure key: {"status": …, "method": …}}), with only what the person set."""
    labels = {label: key for key, (label, _) in TYPES.items()}

    def rows(text: str) -> dict[str, list[str]]:
        found = {}
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 5 and not set(cells[0]) <= set("-: ") and cells[0] not in {"类型", "结构", "初筛类别"}:
                found[cells[0]] = cells
        return found

    written = rows(current)
    people = {label: cells[-1] for label, cells in written.items() if cells[-1] and cells[-1] != PENDING}
    # The 处理方式 column sits before 确认状态 in rows of nine cells; “（默认）” means nobody chose yet.
    methods = {label: cells[-2] for label, cells in written.items()
               if label in labels and len(cells) >= 9 and "默认" not in cells[-2] and parse_method(cells[-2])}
    answers: dict[str, dict[str, str]] = {}
    for label, status in people.items():
        answers.setdefault(labels.get(label, f"{STRUCTURE_PREFIX}{label}"), {})["status"] = status
    for label, text in methods.items():
        answers.setdefault(labels[label], {})["method"] = parse_method(text)
    lines = []
    for line in new.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 5 and (cells[0] in people or cells[0] in methods):
            if cells[0] in people:
                cells[-1] = people[cells[0]]
            if cells[0] in methods and len(cells) >= 9:
                cells[-2] = methods[cells[0]]
            line = "| " + " | ".join(cells) + " |"
        lines.append(line)
    return "\n".join(lines) + "\n", answers


def run_manage(scripts: Path, project: Path) -> dict[str, int]:
    results = {}
    for name, extra in (("build_source_index.py", ["--allow-removed"]), ("validate_v06_project.py", ["--all"])):
        result = subprocess.run([sys.executable, str(scripts / name), "--project", str(project), *extra],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        results[name] = result.returncode
        if result.returncode:
            print((result.stdout + result.stderr)[-1200:])
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a verified build into a project vault.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--vault", required=True, help="Project folder made by kb_project.py")
    parser.add_argument("--manage-skill", help="Optional manage-article-knowledge directory")
    parser.add_argument("--allow-withdraw", action="store_true", help="Publish even when many documents would become 已失效")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write nothing: list what would become 已失效, be archived, renamed, added or changed")
    return parser.parse_args()


def kept_warnings(kept: list[str], limit: int = 5) -> list[str]:
    """Settings merged entry by entry say which entries differed; plain files, by folder, counted."""
    # Config and decisions first: thirty MinerU files must not push kb-config.json out of sight.
    settings = sorted((p for p in kept if "两边不一样" in p), key=lambda p: ("70_构建状态" not in p, p))
    files = [p for p in kept if "两边不一样" not in p]
    lines = [f"两份都有、内容不同，已逐项合并（不同的项用较新的那份）：{p}" for p in settings[:limit]]
    if len(settings) > limit:
        lines.append(f"…另有 {len(settings) - limit} 个设置文件同样逐项合并")
    folders = Counter(str(Path(p.split("（", 1)[0]).parent) for p in files)
    lines += [f"知识库里的版本比运行目录新，保留知识库的：{folder} 下 {n} 个文件" for folder, n in folders.most_common()]
    return lines


def dry_run_report(vault: Path, planned: dict[str, Path], previous: dict[str, str], new_paths: dict[str, str],
                   material: set[str], leaving: list[str], live: int, allowed: bool) -> dict[str, Any]:
    """What a publish would do, from the same lists the publish itself uses; nothing is written."""
    added = sorted(k for k in planned if k not in previous and not (vault / k).exists())
    changed = sorted(k for k, source in planned.items() if (vault / k).is_file() and k in previous
                     and normalized_hash(vault / k) != normalized_hash(source))
    renamed, drawings = [], []
    for key in sorted(set(previous) - set(planned)):
        target = vault / key
        if not (key.startswith(SOURCE.as_posix()) and target.suffix == ".md" and target.is_file()):
            continue
        doc_id = parse_frontmatter(target.read_text(encoding="utf-8")).get("doc_id")
        if doc_id in new_paths:
            renamed.append(f"{key} → {new_paths[doc_id]}")
        elif doc_id in material:
            drawings.append(key)
    folders = Counter(Path(key).relative_to(SOURCE).parts[0] for key in leaving)
    stops = len(leaving) > max(WITHDRAW_LIMIT, live * WITHDRAW_SHARE) and not allowed
    return {
        "dry_run": True,
        "would_stop": stops,
        "withdrawn": len(leaving), "withdrawn_by_folder": dict(folders.most_common()), "withdrawn_files": leaving,
        "renamed": len(renamed), "renamed_files": renamed,
        "drawings_to_material": len(drawings), "drawing_files": drawings,
        "added": len(added), "changed": len(changed),
        "next": ("会因为撤下太多而停下：先看 withdrawn_files，确认确实要撤下再加 --allow-withdraw" if stops
                 else "去掉 --dry-run 正式发布"),
    }


def main() -> int:
    args = parse_args()
    run, vault = Path(args.out).resolve(), Path(args.vault).resolve()
    build = run / BUILD_DIRECTORY
    report = load_json(build / "校验结果.json")
    if report.get("verified") is not True:
        raise SystemExit("这次构建没有通过校验。先运行 verify_kb.py 并修好报告的问题。")
    if not (vault / "01_工作台").is_dir():
        raise SystemExit("知识库项目还不存在。先运行 kb_project.py，用户确认建库后再发布。")

    receipt_path = vault / RECEIPT
    receipt = load_json(receipt_path) if receipt_path.is_file() else {}
    previous: dict[str, str] = receipt.get("files", {})
    legacy = receipt.get("hash_mode") != HASH_MODE
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived: list[str] = []
    warnings: list[str] = []
    unsure = 0
    for key, digest in previous.items():
        target = vault / key
        if not target.is_file() or key.startswith(LEDGER_ROOT) or key == PLAN_PAGE or normalized_hash(target) == digest:
            continue
        if legacy:
            # Old receipts hashed raw bytes, so any spacing looks like an edit. Whatever is replaced
            # below is archived first anyway, so only count it.
            unsure += 1
        else:
            raise SystemExit(f"这个已发布文件的内容被人改过，先把改动移到单独的补充文件再发布：{key}")
    if unsure:
        warnings.append(f"旧版发布记录无法分辨格式调整和人工修改：{unsure} 个文件按新内容发布，被替换的旧版都在本次归档里")

    planned = planned_files(run)
    # A document whose path changed is the same document, not a withdrawn one.
    new_paths = {parse_frontmatter(path.read_text(encoding="utf-8")).get("doc_id"): key
                 for key, path in planned.items() if key.startswith(SOURCE.as_posix()) and path.suffix == ".md"}
    # A PDF now read through its Word or PowerPoint original: its old page is that page, not withdrawn.
    routes = load_json(build / "routes.json") if (build / "routes.json").is_file() else {}
    for route in routes.values():
        kept = routes.get(route.get("same_as") or "")
        if route.get("reason") == "other_format" and kept and route.get("source_hash"):
            page = new_paths.get(f"DOC-{kept.get('source_hash', '')[:10]}")
            if page:
                new_paths.setdefault(f"DOC-{route['source_hash'][:10]}", page)
    # A drawing now filed as pictures in 90_素材: its old text page goes to the archive, no 已失效 stub.
    material = {f"DOC-{r['source_hash'][:10]}" for r in routes.values() if r.get("reason") == "drawing" and r.get("source_hash")}
    leaving = []
    for key in sorted(set(previous) - set(planned)):
        target = vault / key
        if key.startswith(SOURCE.as_posix()) and target.suffix == ".md" and target.is_file():
            fields = parse_frontmatter(target.read_text(encoding="utf-8"))
            if fields.get("doc_id") and fields.get("status") != "已失效" and fields["doc_id"] not in new_paths and fields["doc_id"] not in material:
                leaving.append(key)
    live = sum(1 for key in previous if key.startswith(SOURCE.as_posix()) and key.endswith(".md"))
    if len(leaving) > max(WITHDRAW_LIMIT, live * WITHDRAW_SHARE) and not args.allow_withdraw:
        folders = Counter(Path(key).relative_to(SOURCE).parts[0] for key in leaving)
        raise SystemExit(f"这次发布会让 {len(leaving)} / {live} 份资料变成已失效（{'、'.join(f'{f} {n}' for f, n in folders.most_common())}）。"
                         "通常是官网没读到或少给了初筛包。查清后重跑；确实要撤下，加 --allow-withdraw")
    if args.dry_run:
        print(json.dumps(dry_run_report(vault, planned, previous, new_paths, material, leaving, live, args.allow_withdraw),
                         ensure_ascii=False, indent=2))
        return 0
    written: dict[str, str] = {}
    renamed: list[str] = []
    moved = move_pictures(vault, build, previous, renamed)
    relinked = follow_picture_links(vault, moved, set(planned))
    for key, source in list(planned.items()):
        target = vault / key
        if key.startswith(LEDGER_ROOT):
            side = vault / side_by_side(key)
            if target.exists() and not ours(target) and empty_ledger(target):
                # This skill keeps the ledgers; another tool's empty template would only be a second copy.
                archive(vault, key, stamp, archived, move=True)
            if side.exists() and not target.exists():
                # Written beside a template that is gone now: back to the plain name, people's notes kept.
                shutil.move(str(side), target)
                renamed.append(f"{side_by_side(key)} → {key}")
        if key.startswith(LEDGER_ROOT) and (vault / side_by_side(key)).exists() or (
                key.startswith(LEDGER_ROOT) and target.exists() and not ours(target)):
            # Another tool keeps a ledger with real entries here; ours goes beside it, nothing is lost.
            del planned[key]
            key = side_by_side(key)
            planned[key] = source
            target = vault / key
        if key == PLAN_PAGE and target.is_file():
            # People confirm samples in this page; their answers stay and steer the next build.
            current = target.read_text(encoding="utf-8")
            merged, answers = merge_plan(current, source.read_text(encoding="utf-8"))
            if answers:
                decisions = load_decisions(build)
                for answer_key, answer in answers.items():
                    entry = dict(decisions.get(answer_key) or {})
                    if any(entry.get(field) != value for field, value in answer.items()):
                        entry.update(answer, at=stamp[:8])
                        decisions[answer_key] = entry
                save_decisions(build, decisions)
            if merged.rstrip() != current.rstrip():
                archive(vault, key, stamp, archived)
                write_text(target, merged)
            written[key] = normalized_hash(target)
            continue
        if key.startswith(LEDGER_ROOT) and target.is_file():
            # People fill in stages, owners, and notes; merge instead of overwriting.
            current = target.read_text(encoding="utf-8")
            merged = merge_ledger(current, source.read_text(encoding="utf-8"))
            if merged.rstrip() != current.rstrip():
                archive(vault, key, stamp, archived)
                write_text(target, merged)
            written[key] = normalized_hash(target)
            continue
        if target.exists() and key not in previous:
            raise SystemExit(f"目标位置已有不是本 Skill 写的文件，不覆盖：{key}")
        if target.exists() and normalized_hash(target) != normalized_hash(source):
            archive(vault, key, stamp, archived)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written[key] = normalized_hash(target)

    withdrawn = 0
    for key in sorted(set(previous) - set(planned)):
        target = vault / key
        if not target.is_file():
            continue
        if key.startswith(ASSETS.as_posix()) or key.startswith(LEDGER_ROOT):
            written[key] = normalized_hash(target)  # Pictures and ledgers are never removed.
            continue
        old = target.read_text(encoding="utf-8") if target.suffix == ".md" else ""
        if parse_frontmatter(old).get("doc_id") in new_paths and key.startswith(SOURCE.as_posix()):
            archive(vault, key, stamp, archived, move=True)
            renamed.append(f"{key} → {new_paths[parse_frontmatter(old)['doc_id']]}")
        elif key.startswith(SOURCE.as_posix()) and parse_frontmatter(old).get("doc_id") in material:
            archive(vault, key, stamp, archived, move=True)
            renamed.append(f"{key} → 90_素材（图纸，改按素材处理，每页一张图）")
        elif key.startswith(SOURCE.as_posix()) and parse_frontmatter(old).get("doc_id"):
            # Only documents this vault really published leave a 已失效 record.
            if parse_frontmatter(old).get("status") != "已失效":
                archive(vault, key, stamp, archived)
                write_text(target, tombstone(old, stamp))
                withdrawn += 1
            written[key] = normalized_hash(target)
        else:
            archive(vault, key, stamp, archived, move=True)
    for old_page in (OLD_TODO, OLD_REGISTER):
        path = vault / old_page
        # Pages earlier versions of this skill wrote, now replaced by the overview and the register.
        if path.is_file() and (MARKER in path.read_text(encoding="utf-8") or path.read_text(encoding="utf-8").startswith("# 知识库资料台账")):
            archive(vault, old_page.as_posix(), stamp, archived, move=True)
    if archived:
        lines = [f"# 版本说明 {stamp}", "", f"由 compile-enterprise-knowledge {SKILL_VERSION} 发布时归档，共 {len(set(archived))} 个文件。", ""]
        if renamed:
            lines.extend(["## 改名（同一份资料或图片换了名字或目录）", "", *(f"- {entry}" for entry in renamed), "", "## 归档的旧版", ""])
        lines.extend(f"- {key}" for key in sorted(set(archived)))
        write_text(vault / ARCHIVE / stamp / "00_版本说明.md", "\n".join(lines) + "\n")

    run_info = load_json(build / "run-info.json") if (build / "run-info.json").is_file() else {}
    update_project_info(vault, run_info.get("packages", []))
    state = save_state(run, vault)
    warnings += kept_warnings(state["kept_newer_in_vault"])
    write_json(receipt_path, {"generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}", "published_at": stamp,
                              "hash_mode": HASH_MODE, "files": written})
    index = build_index(vault)
    entry = {"published_at": stamp, "version": SKILL_VERSION, "documents": report.get("documents"),
             "blocks": report.get("blocks_kept"), "withdrawn": withdrawn, "renamed": len(renamed), "files": len(written),
             "archived": len(set(archived)), "state_saved": state["saved"], "pictures_renamed": len(moved),
             "pages_relinked": relinked, "run": str(run)}
    with (vault / RUN_LOG).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    subprocess.run([sys.executable, str(Path(__file__).with_name("kb_status.py")), "--out", str(run), "--vault", str(vault)],
                   capture_output=True)
    manage = run_manage(Path(args.manage_skill).resolve() / "scripts", vault) if args.manage_skill else {}
    print(json.dumps({**entry, "index_chunks": index["chunks"], "manage": manage, "warnings": warnings}, ensure_ascii=False))
    return 1 if any(manage.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
