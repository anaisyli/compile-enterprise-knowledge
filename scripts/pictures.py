#!/usr/bin/env python3
"""Material pictures: named for what they show, sorted by what they are for.

A picture's name comes from the words beside it in the source: the row's product name, the
catalog page's heading and the company's own style code, the website's alt text. Nothing is
guessed. A buyer's style number, a customer's name, or a passage carrying ⚠ never goes into a
name; such a picture keeps a plain numbered name until a person or an AI look gives it one.

export   lists the pictures that still have only a numbered name, for an AI to look at.
collect  reads the AI's answers; they are used on the next build, marked 待核对.

A name, once given, stays: links in the vault and on the website must not break.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from kb_common import BUILD_DIRECTORY, load_json, load_jsonl, safe_name, write_json, write_jsonl, write_text
from products import material_folders


ASSETS = "03_正式知识/90_素材"
INDEX = f"{ASSETS}/00_素材索引.md"
LIST_CSV = f"{ASSETS}/00_素材清单.csv"
STATE = "picture-names.json"
AI_FOLDER = "picture-names"
AI_ANSWERS = "picture-names/answers.json"
WEBSITE_PICTURES = "website/pictures.json"
# A picture on more pages than this is a logo or a background, not material.
REPEATED = 3
LABEL_MAX = 40
PUBLIC, CHECK, INTERNAL = "可用于官网", "待核对", "内部"
NUMBERED, FROM_SOURCE, FROM_AI = "编号", "原文", "AI看图"
CERTIFICATE = re.compile(r"(?i)certif|证书|认证|\bISO\s?\d|\bBSCI\b|OEKO|\bSGS\b|\bGRS\b|test report|检测报告")
FACTORY = re.compile(r"(?i)factory|workshop|machine|equipment|\bloom|warehouse|工厂|车间|设备|织机|仓库")
MEANINGLESS_ALT = re.compile(r"(?i)^(?:img|image|picture|photo|pic|dsc|banner|logo|icon)?[\s_\-]*\d*$|\.(?:jpe?g|png|webp|gif)$")
FOLDERS = ("产品", "资料包素材", "工厂与设备", "证书", "公司", "报价单参考图", "产品表图片", "页面图", "其他")
# Pictures screening kept by folder sit under their original folders, the last few levels of them:
# enough to tell one product's 白底图 from another's without paths too long for Windows.
MEDIA_ROOT = "资料包素材"
MEDIA_DEPTH = 3
MEDIA_LIST = "media.json"
KIND_LABELS = {"image": "图片", "video": "视频", "audio": "音频"}


def own_code_pattern(prefixes: list[str]) -> re.Pattern[str] | None:
    if not prefixes:
        return None
    return re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, prefixes), key=len, reverse=True)) + r")[\s-]?[A-Z]?-?\d{2,4}(?:-\d+)?\b")


class Namer:
    """Turns the words beside a picture into a safe label, or nothing."""

    def __init__(self, config: dict[str, Any], forbidden: re.Pattern[str] | None) -> None:
        self.codes = own_code_pattern(config.get("style_prefixes", []))
        self.forbidden = forbidden
        # Website categories are English slugs in the config; the English file name can use them.
        self.english = {cn: slug for slug, cn in config.get("categories", {}).items()}

    def code(self, text: str) -> str:
        found = sorted({re.sub(r"\s+", "-", m.group(0).upper()) for m in self.codes.finditer(text)}) if self.codes else []
        return found[0] if len(found) == 1 else ""

    def label(self, text: str) -> str:
        """Words only: a token with digits is a buyer's number unless it is the company's own code."""
        if not text or (self.forbidden and self.forbidden.search(text)):
            return ""
        own = {m.group(0) for m in self.codes.finditer(text)} if self.codes else set()
        words = [w for w in re.split(r"\s+", re.sub(r"[|/\\:：;；,，()（）\[\]{}]+", " ", text)) if w]
        kept = [w for w in words if w in own or not re.search(r"\d", w)]
        label = " ".join(kept).strip(" -_.")
        return label[:LABEL_MAX].rstrip(" -_.") if re.search(r"[A-Za-z一-鿿]{2}", label) else ""


def english_name(entry: dict[str, Any], namer: Namer, suffix: str) -> str:
    """For the website team only: internal pictures do not go online, so they get none."""
    if entry["use"] == INTERNAL:
        return ""
    words = re.findall(r"[A-Za-z0-9]+", entry.get("english_source") or entry["alt"])
    if not any(re.search(r"[A-Za-z]", w) for w in words):
        words = []
    slug = "-".join(w.casefold() for w in words)[:60].strip("-") or namer.english.get(entry.get("line", ""), "")
    return f"{slug}-{entry['id'][4:12]}{suffix}" if slug else ""


def first_line(blocks: list[dict[str, Any]]) -> str:
    for block in blocks:
        for line in block["text"].splitlines():
            line = line.strip(" |#*-")
            if 2 <= len(line) <= LABEL_MAX:
                return line
    return ""


def kind_folder(text: str, fallback: str) -> str:
    if CERTIFICATE.search(text):
        return "证书"
    if FACTORY.search(text):
        return "工厂与设备"
    return fallback


def page_group(info: dict[str, Any], page: str) -> str:
    """The heading a page or slide has in the source layer."""
    return f"第 {page} 张幻灯片" if info.get("page_unit") == "slide" else f"第 {page} 页"


def candidates(build: Path, docs: dict[str, dict[str, Any]], by_doc: dict[str, list[dict[str, Any]]], order: list[str],
               statuses: dict[str, str], unsafe: set[tuple[str, str]], status_info: dict[str, Any], namer: Namer,
               max_row_images: int, held: set[tuple[str, str]], places: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every picture the vault will show or keep, with the words that can name it. held: (doc, locator)
    of every passage removed or waiting for the client."""
    found: list[dict[str, Any]] = []
    assets = build / "assets"
    website = load_json(build / WEBSITE_PICTURES) if (build / WEBSITE_PICTURES).is_file() else {}
    pages_of: dict[str, set[str]] = defaultdict(set)
    for doc_id in order:
        for page, names in status_info.get(docs[doc_id]["item_id"], {}).get("page_pictures", {}).items():
            for name in names:
                pages_of[name].add(f"{doc_id}:{page}")
    for page_id, pictures in website.items():
        for picture in pictures:
            pages_of[picture["id"]].add(page_id)

    def add(name: str, doc_id: str, group: str, **fields: Any) -> None:
        if (assets / name).is_file():
            found.append({"id": name, "doc_id": doc_id, "group": group, **fields})

    for doc_id in order:
        doc, blocks = docs[doc_id], by_doc[doc_id]
        reference = doc.get("role") == "reference"
        quotation = doc.get("folder", "").startswith("产品表/报价单")
        for block in blocks:
            if block["kind"] != "row" or block["status"] == "pending_client":
                continue
            cells = block.get("cells", {})
            clean = not block.get("flags")
            code = namer.code(cells.get("style", "")) if clean else ""
            words = namer.label(cells.get("name") or cells.get("style", "")) if clean else ""
            label = " ".join(dict.fromkeys(p for p in (code, words) if p))
            for name in block.get("images", [])[:max_row_images]:
                add(name, doc_id, block["group"], folder="报价单参考图" if quotation else "产品表图片",
                    prefix="报价单参考图" if quotation else "产品表", label=label, alt=label, use=INTERNAL,
                    english_source=words, location=block["locator"])
        info = status_info.get(doc["item_id"], {})
        for page, name in info.get("page_images", {}).items():
            group = page_group(info, page)
            if (doc_id, group) not in unsafe:
                add(name, doc_id, group, folder="页面图", prefix="页面图", label=f"{doc['title']}-{group.replace(' ', '')}",
                    alt=f"{doc['title']} {group}原图", use=INTERNAL, location=group)
        catalog = doc.get("folder") == "产品画册"
        line = doc["title"].split("-", 1)[-1] if catalog else ""
        for page, names in info.get("page_pictures", {}).items():
            group = page_group(info, page)
            if (doc_id, group) in unsafe:
                continue
            on_page = [b for b in blocks if b["group"] == group]
            text = "\n".join(b["text"] for b in on_page)
            clean = [b for b in on_page if not b.get("flags")]
            heading = namer.label(first_line(clean))
            code = namer.code("\n".join(b["text"] for b in clean))
            label = " ".join(dict.fromkeys(p for p in (code, heading) if p))
            folder = kind_folder(text, f"产品/{line}" if catalog else "公司")
            use = PUBLIC if statuses.get(doc_id) == "有效" and not reference and len(clean) == len(on_page) else CHECK
            for name in names:
                if len(pages_of[name]) <= REPEATED:
                    add(name, doc_id, group, folder=folder, prefix=line or folder.split("/")[-1], label=label, line=line,
                        alt=" ".join(p for p in (line, label) if p), use=use, english_source=heading, location=group)
        if website.get(doc["item_id"]):
            category = doc.get("folder", "").split("/", 1)[-1]
            title = namer.label(doc["title"].split("-", 1)[-1])
            for picture in website.get(doc["item_id"], []):
                if len(pages_of[picture["id"]]) > REPEATED:
                    continue
                alt = "" if MEANINGLESS_ALT.search(picture.get("alt", "").strip()) else namer.label(picture.get("alt", ""))
                section = f"section {picture.get('section', 1)}"
                if (doc_id, section) in held:
                    continue  # Its section was removed or waits for the client: so does the picture.
                own = [b for b in blocks if b.get("locator") == section]
                flagged = any(b.get("flags") for b in own)
                company = category in {"公司与服务", ""}
                folder = kind_folder(f"{alt} {doc['title']}", "公司") if company else f"产品/{category}"
                add(picture["id"], doc_id, own[0]["group"] if own else blocks[0]["group"], folder=folder, prefix=folder.split("/")[-1], label=alt or title,
                    line="" if company else category, alt=alt or title, use=CHECK if flagged else PUBLIC,
                    english_source=alt or doc.get("original_name", ""), location=section)
    for entry in load_media(build):
        # A product's pictures go under 大类/产品/图片类型; what matches no product keeps its own folders.
        place = (places or {}).get(entry["item_id"])
        placed = material_folders(place, namer.forbidden)
        folders = placed or media_folders(entry["relpath"], namer)
        stem = Path(entry["relpath"]).stem
        label = "" if namer.forbidden and namer.forbidden.search(stem) else safe_name(stem)[:LABEL_MAX]
        prefix = (safe_name(place["product"]["name"], "其他") if placed else folders[-1]) if folders else ""
        location = "/".join(media_folders(entry["relpath"], namer)) or "根目录"
        if entry.get("id"):
            add(entry["id"], "", "", folder="/".join([MEDIA_ROOT, *folders]), prefix=prefix, label=label,
                alt=" ".join(p for p in (prefix, label) if p), use=PUBLIC, english_source=stem, location=location)
        if entry.get("cover"):
            add(entry["cover"], "", "", folder="/".join([MEDIA_ROOT, *folders]), prefix=prefix, label=f"{label}-封面" if label else "视频封面",
                alt=f"视频封面：{' '.join(p for p in (prefix, label) if p)}", use=PUBLIC, english_source=stem, location=location)
    return found


def load_media(build: Path) -> list[dict[str, Any]]:
    return load_json(build / MEDIA_LIST) if (build / MEDIA_LIST).is_file() else []


def media_folders(relpath: str, namer: Namer) -> list[str]:
    """The last folders above a file, each safe for a path; one naming a customer becomes 其他."""
    parts = [p for p in relpath.replace("\\", "/").split("/")[:-1] if p]
    return [("其他" if namer.forbidden and namer.forbidden.search(p) else safe_name(p, "其他")) for p in parts[-MEDIA_DEPTH:]]


def assign(found: list[dict[str, Any]], saved: dict[str, Any], answers: dict[str, Any], namer: Namer) -> dict[str, dict[str, Any]]:
    """id -> the picture's place in 90_素材 and what to say about it. The first use of a picture wins."""
    entries: dict[str, dict[str, Any]] = {}
    for item in found:
        if item["id"] in entries:
            entries[item["id"]]["more"] += 1
            continue
        suffix = Path(item["id"]).suffix.casefold()
        short = item["id"][4:12]
        named_by, label, alt = (FROM_SOURCE, item["label"], item["alt"]) if item["label"] else (NUMBERED, "", item["alt"])
        answer = answers.get(item["id"]) or {}
        if answer.get("drop"):
            continue  # The AI saw a person, a customer's mark or a price: not material.
        if named_by == NUMBERED and answer.get("name"):
            named_by, label, alt = FROM_AI, namer.label(answer["name"]), answer.get("alt") or answer["name"]
            item["english_source"] = answer.get("english") or answer["name"]
            item["use"] = CHECK if item["use"] == PUBLIC else item["use"]
        stem = safe_name("-".join(p for p in (item["prefix"], label) if p) or "图片")
        file = f"{item['folder']}/{stem}-{short}{suffix}"
        old = saved.get(item["id"])
        # A name stays once given; only a plain numbered name gives way to a better one.
        if old and old.get("file") and not (old.get("named_by") == NUMBERED and named_by != NUMBERED):
            file, named_by = old["file"], old.get("named_by", named_by)
        entries[item["id"]] = {**item, "file": file, "named_by": named_by, "alt": alt or label, "more": 0,
                               "english": english_name({**item, "alt": alt or label}, namer, suffix)}
    taken: dict[str, str] = {}
    for picture_id, entry in sorted(entries.items()):
        key = Path(entry["file"]).name.casefold()
        if key in taken:  # Two ids sharing eight characters: the second gets the whole id.
            entry["file"] = f"{Path(entry['file']).parent.as_posix()}/{Path(entry['file']).stem[:-8]}{picture_id[4:]}"
        taken[Path(entry["file"]).name.casefold()] = picture_id
    return entries


def write_index(build: Path, entries: dict[str, dict[str, Any]], docs: dict[str, dict[str, Any]], escape: Callable[[str], str]) -> None:
    """One page for people, one CSV for the website team; no previews, thousands of pictures make Obsidian crawl."""
    counts = defaultdict(int)
    for entry in entries.values():
        counts[entry["use"]] += 1
    lines = ["# 素材索引", "",
             "图片发布后一直保留，不随资料变化删除。名字取自图片旁边的原文：产品名、画册页标题、公司自己的款号、官网图片说明；"
             "买家款号和客户名不进名字。点文件名看图。", "",
             f"共 {len(entries)} 张：" + "，".join(f"{use} {counts[use]}" for use in (PUBLIC, CHECK, INTERNAL) if counts[use]) + "。", "",
             f"- **{PUBLIC}**：来自官网或公司画册，原文没有 ⚠，建站可以直接用；“说明”一列可以当 alt 文字。",
             f"- **{CHECK}**：原文有 ⚠ 或名字是 AI 看图起的，用前看一眼原图和原文。",
             f"- **{INTERNAL}**：报价单、产品表里的图（多是买家询价时发来的款式）和整页截图，不要上官网。",
             f"- 名字是“{NUMBERED}”的，旁边没有能用的文字；可以让 AI 看图起名（pictures.py export / collect）。",
             f"- **{MEDIA_ROOT}**：初筛抽看样本后按文件夹整批保留的图片（比如建站资料里的渲染图、白底图），"
             "放在原来的文件夹下（保留最后三层），名字取自文件夹和原文件名。视频、音频和较大的图片见最后一节。",
             f"- 建站用的清单（含英文文件名）：[[00_素材清单.csv]]。", ""]
    by_folder: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries.values():
        by_folder[Path(entry["file"]).parent.as_posix()].append(entry)
    order = {name: i for i, name in enumerate(FOLDERS)}
    for folder in sorted(by_folder, key=lambda f: (order.get(f.split("/")[0], len(FOLDERS)), f)):
        rows = sorted(by_folder[folder], key=lambda e: e["file"])
        lines += [f"## {folder}（{len(rows)} 张）", "", "| 图片 | 说明 | 能否用于官网 | 名字来自 | 出现在 | 位置 |", "| --- | --- | --- | --- | --- | --- |"]
        for entry in rows:
            doc = docs.get(entry["doc_id"])
            link = (f"[[{Path(doc['file_name']).stem}#{entry['group']}|{doc['title']}]]" if doc else "资料包原文件夹") \
                + (f" 等 {entry['more'] + 1} 处" if entry["more"] else "")
            lines.append(f"| [[{Path(entry['file']).name}]] | {escape(entry['alt'])} | {entry['use']} | {entry['named_by']} | {link} "
                         f"| {escape(entry['location'])} |")
        lines.append("")
    lines += media_section(build, entries, escape)
    write_text(build / "vault" / INDEX, "\n".join(lines).rstrip() + "\n")
    target = build / "vault" / LIST_CSV
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["文件", "说明（alt）", "能否用于官网", "英文文件名建议", "名字来自", "出处", "位置", "编号"])
        for entry in sorted(entries.values(), key=lambda e: e["file"]):
            source = docs[entry["doc_id"]]["title"] if entry["doc_id"] in docs else "资料包原文件夹"
            writer.writerow([entry["file"], entry["alt"], entry["use"], entry["english"], entry["named_by"],
                             source, entry["location"], entry["id"]])


def media_section(build: Path, entries: dict[str, dict[str, Any]], escape: Callable[[str], str]) -> list[str]:
    """What screening kept by folder but the vault does not hold: video, audio, pictures above the
    size limit, and folders waiting for the client. One row per original folder, with a link that
    opens it in the package, because thousands of rows help nobody."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in load_media(build):
        if entry.get("id") in entries:
            continue
        if entry["status"] == "media_awaiting_client":
            state = "待客户确认（整个文件夹）"
        elif entry["status"] == "drawing_unrendered":
            state = "图纸，没能转成图片（本机没装 PyMuPDF 或文件损坏），原件在初筛包"
        elif entry["kind"] == "image":
            state = "图片较大，原图在初筛包"
        else:
            state = "已保留，内容待转写"
        folder = str(Path(entry["package_path"]).parent.as_posix())
        groups[(entry["package"], folder, state)].append(entry)
    if not groups:
        return []
    lines = ["## 留在初筛包里的素材", "",
             "视频、音频和较大的图片不复制进知识库，原件在初筛包里，点“打开文件夹”就能看。视频里讲的内容转写以后才能当知识用。"
             "待客户确认的文件夹，客户回复“保留”后重跑，图片会进上面的素材文件夹。", "",
             "| 原来的文件夹 | 内容 | 状态 | 打开文件夹 |", "| --- | --- | --- | --- |"]
    for (package, folder, state), members in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][1])):
        kinds = defaultdict(int)
        for entry in members:
            kinds[KIND_LABELS.get(entry["kind"], entry["kind"])] += 1
        original = str(Path(members[0]["relpath"]).parent.as_posix())
        what = "、".join(f"{label} {count} 个" for label, count in kinds.items())
        uri = (Path(package) / folder).resolve().as_uri()
        lines.append(f"| {escape(original if original != '.' else '根目录')} | {what} | {state} | [打开文件夹]({uri}) |")
    return [*lines, ""]


def build_pictures(build: Path, config: dict[str, Any], docs: dict[str, dict[str, Any]], by_doc: dict[str, list[dict[str, Any]]],
                   order: list[str], statuses: dict[str, str], unsafe: set[tuple[str, str]], status_info: dict[str, Any],
                   forbidden: re.Pattern[str] | None, max_row_images: int, held: set[tuple[str, str]],
                   places: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    namer = Namer(config, forbidden)
    found = candidates(build, docs, by_doc, order, statuses, unsafe, status_info, namer, max_row_images, held, places)
    saved = load_json(build / STATE) if (build / STATE).is_file() else {}
    answers = load_json(build / AI_ANSWERS) if (build / AI_ANSWERS).is_file() else {}
    entries = assign(found, saved, answers, namer)
    # Names given before stay known even when a picture is not shown this time.
    write_json(build / STATE, {**saved, **{i: {"file": e["file"], "named_by": e["named_by"]} for i, e in entries.items()}})
    write_json(build / "pictures.json", {i: {k: e[k] for k in ("file", "alt", "use", "named_by", "english", "doc_id", "group")}
                                          for i, e in entries.items()})
    return entries


README = """# 看图起名

`todo.jsonl` 每行一张图：`id`、图片路径 `path`、它在哪份资料哪一页 `source`。这些图旁边没有能用来起名的文字。

逐张打开图片看，写进同目录的 `answers.jsonl`，每行一个：
{"id": "img-…", "name": "提花挂毯 大象图案", "alt": "大象图案机织提花挂毯，挂在客厅墙上", "english": "elephant jacquard wall tapestry"}

规则：
1. 只写图里看得见的东西：产品是什么、颜色、图案、场景。看不出的不写，不猜面料、克重、款号、品牌。
2. 图里有人名、客户名、品牌标志、价格的，name 写“不用”，这张图不进素材。
3. name 不超过 20 个字，alt 一句话。english 是给网站文件名用的英文，几个词。
写完运行 `pictures.py collect --out <运行目录>`，再重跑构建。AI 起的名字在索引里标“AI看图”“待核对”。
"""


def export(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    pictures = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    todo = [{"id": i, "path": str(build / "assets" / i),
             "source": f"{docs[e['doc_id']]['title']} {e['group']}" if e["doc_id"] in docs else "资料包原文件夹"}
            for i, e in sorted(pictures.items()) if e["named_by"] == NUMBERED and e["use"] != INTERNAL]
    folder = build / AI_FOLDER
    write_jsonl(folder / "todo.jsonl", todo)
    write_text(folder / "README.md", README)
    return {"todo": len(todo), "folder": str(folder)}


def collect(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    path = build / AI_FOLDER / "answers.jsonl"
    answers = load_json(build / AI_ANSWERS) if (build / AI_ANSWERS).is_file() else {}
    todo = {row["id"] for row in load_jsonl(build / AI_FOLDER / "todo.jsonl")} if (build / AI_FOLDER / "todo.jsonl").is_file() else set()
    taken, skipped = 0, []
    for row in load_jsonl(path) if path.is_file() else []:
        if row.get("id") not in todo:
            skipped.append(row.get("id"))
            continue
        if (row.get("name") or "").strip() == "不用":
            answers[row["id"]] = {"name": "", "drop": True}
        else:
            answers[row["id"]] = {k: (row.get(k) or "").strip()[:80] for k in ("name", "alt", "english")}
        taken += 1
    write_json(build / AI_ANSWERS, answers)
    return {"collected": taken, "not_in_todo": skipped[:10]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Let an AI name the pictures that have no words beside them.")
    parser.add_argument("command", choices=["export", "collect"])
    parser.add_argument("--out", required=True, help="Run directory")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    print(json.dumps(export(out) if args.command == "export" else collect(out), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
