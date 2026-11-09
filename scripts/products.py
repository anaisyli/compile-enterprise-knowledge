#!/usr/bin/env python3
"""产品与方案素材总览: for each product and each solution, everything the client gave about it.

A client files the same product in several places: its pictures under 产品\\<大类>\\<产品>, the
website's pictures under the page's title, the spec sheet as a PDF somewhere else. One page per
product gathers the documents, website pages, pictures by kind, and videos, so a person sees at a
glance what exists.

Which product a file belongs to is decided by names, never guessed from content:
- The list of products comes from kb-config.json "products" when it is there:
  [{"name": "壁挂广告机", "category": "广告机", "kind": "product",
    "aliases": ["Wall mounted Digital Signage", "DS20", "DS50"]}]
  Without it, the list is read from the package's 产品\\<大类>\\<产品> folders, the website's
  categories, the catalogs, and pages or folders named "... Solution"; English and Chinese
  names of one product then stay apart until someone writes the aliases.
- A file belongs to the product whose name or alias appears, longest first, in its folder path or
  title. What matches nothing is listed under 未归类, not put somewhere.

propose  writes the list it would use and where each name came from, for an AI or a person to
         merge into kb-config.json "products".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from kb_common import BUILD_DIRECTORY, SKILL_VERSION, frontmatter, load_config, load_json, load_jsonl, safe_name, write_json, write_text


HUB = "03_正式知识/80_产品与方案素材"
PRODUCT_ROOT = re.compile(r"(?i)^(?:产品|产品资料|产品图片|产品图|products?)$")
SOLUTION = re.compile(r"(?i)solutions?\b|解决方案")
# Folder names that say what kind of picture, not which product.
PICTURE_KINDS = [
    ("白底图", re.compile(r"(?i)白底|white\s*(?:background|bg)")),
    ("尺寸图", re.compile(r"(?i)尺寸|dimension|drawing")),
    ("效果图", re.compile(r"(?i)效果|渲染|render")),
    ("场景图", re.compile(r"(?i)场景|scene|应用|application")),
    ("实物图", re.compile(r"(?i)实物|实拍|real\s*shot|actual|photo")),
    ("详情图", re.compile(r"(?i)详情|主图|detail|banner")),
]
OTHER_PICTURES = "其他图片"
VIDEO = "视频"
# A folder like these says what is inside (出货图片, 照片，视频), not which product.
NOT_A_NAME = re.compile(r"(?i)^(?:\d+[.、_\s-].*|.*(?:图片|照片|视频|素材)|资料|英文版|中文版|大尺寸|小尺寸|白底图|实物图|效果图|场景图|尺寸图|详情图)$")
PRODUCT_TAIL = re.compile(r"(?:产品资料|产品)$")
WEBSITE_SKIP = {"公司与服务", "常见问题", "新闻与博客", ""}
GALLERY = 8
# Company material has no product but is asked for just as often: fixed groups, matched by name too.
COMPANY_GROUPS = [
    {"name": "工厂与生产", "kind": "company", "category": "公司", "aliases": ["工厂", "车间", "生产线", "Factory", "Workshop", "装柜", "发货", "出货"]},
    {"name": "公司介绍", "kind": "company", "category": "公司", "aliases": ["公司简介", "公司照片", "公司视频", "About Us", "展厅"]},
    {"name": "展会", "kind": "company", "category": "公司", "aliases": ["展会", "Exhibition"]},
    {"name": "合作案例", "kind": "company", "category": "公司", "aliases": ["案例", "客户合作", "合照", "Case Study", "Cases"]},
    {"name": "资质证书", "kind": "company", "category": "公司", "aliases": ["证书", "资质", "认证", "Certificate"]},
]
MIN_ALIAS = 4


def squeeze(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).casefold()


def usable_alias(alias: str) -> bool:
    key = squeeze(alias)
    return len(key) >= MIN_ALIAS or (len(key) >= 2 and bool(re.search(r"[一-鿿]", key)))


def parts_of(relpath: str) -> list[str]:
    """Folders of a path, with a folder repeated inside itself (建站资料/建站资料) counted once."""
    parts: list[str] = []
    for part in relpath.replace("\\", "/").split("/")[:-1]:
        if part and (not parts or parts[-1] != part):
            parts.append(part)
    return parts


def picture_kind(relpath: str, kind: str) -> str:
    if kind in {"video", "audio"}:
        return VIDEO
    folders = " ".join(parts_of(relpath)[-3:])
    return next((name for name, pattern in PICTURE_KINDS if pattern.search(folders)), OTHER_PICTURES)


def site_suffix(titles: list[str]) -> re.Pattern[str] | None:
    """The tail most page titles share ("-Smart Board Manufacturer Smartline"), to be dropped."""
    tails = Counter(m.group(0) for t in titles for m in [re.search(r"\s*[-|–]\s*[^-|–]{3,60}$", t)] if m)
    common = [tail for tail, n in tails.most_common(3) if n >= 3]
    return re.compile("(?:" + "|".join(map(re.escape, common)) + r")$") if common else None


def clean_title(title: str, suffix: re.Pattern[str] | None) -> str:
    return (suffix.sub("", title) if suffix else title).strip(" -|–")


def proposed(config: dict[str, Any], media: list[dict[str, Any]], docs: dict[str, dict[str, Any]],
             forbidden: re.Pattern[str] | None) -> list[dict[str, Any]]:
    """Products and solutions read from folder names, website categories, catalogs, and titles."""
    found: dict[str, dict[str, Any]] = {}

    def add(name: str, kind: str, category: str, source: str, *aliases: str) -> None:
        name = name.strip()
        if not usable_alias(name) or NOT_A_NAME.match(name) or (forbidden and forbidden.search(name)):
            return
        entry = found.setdefault(squeeze(name), {"name": name, "kind": kind, "category": category, "aliases": [], "from": []})
        for alias in (name, *aliases):
            if alias and alias not in entry["aliases"] and usable_alias(alias):
                entry["aliases"].append(alias)
        if source not in entry["from"]:
            entry["from"].append(source)

    titles = {d.get("original_name") or d["title"] for d in docs.values() if d.get("origin") == "website"}
    suffix = site_suffix(sorted(titles | {p for m in media for p in parts_of(m["relpath"])}))
    english = {cn: slug.replace("-", " ") for slug, cn in config.get("categories", {}).items()}
    for entry in media:
        parts = parts_of(entry["relpath"])
        root = next((i for i, p in enumerate(parts) if PRODUCT_ROOT.match(p)), None)
        if root is not None and root + 1 < len(parts):
            category = parts[root + 1]
            # The first folder under the category that names something; 分类图片 and the like are skipped.
            named = [p for p in parts[root + 2:] if not NOT_A_NAME.match(p)]
            product = PRODUCT_TAIL.sub("", named[0]).strip() if named else category
            add(product or category, "product", category, "资料包文件夹")
        for part in parts:
            if SOLUTION.search(part):
                add(clean_title(part, suffix), "solution", "解决方案", "资料包文件夹")
    for doc in docs.values():
        folder = doc.get("folder", "")
        if folder.startswith("官网页面/") and folder.split("/", 1)[1] not in WEBSITE_SKIP:
            category = folder.split("/", 1)[1]
            add(category, "product", category, "官网大类", english.get(category, ""))
        if folder == "产品画册":
            add(doc["title"].split("-", 1)[-1], "product", "产品画册", "产品画册")
        title = doc.get("original_name") or ""
        if doc.get("origin") == "website" and SOLUTION.search(title):
            add(clean_title(title, suffix), "solution", "解决方案", "官网页面")
    return sorted(found.values(), key=lambda e: (e["kind"], e["category"], e["name"]))


def catalog(config: dict[str, Any], media: list[dict[str, Any]], docs: dict[str, dict[str, Any]],
            forbidden: re.Pattern[str] | None) -> list[dict[str, Any]]:
    given = config.get("products")
    company = [{**g, "from": ["固定分组"]} for g in COMPANY_GROUPS if g["name"] not in {p.get("name") for p in given or []}]
    if not given:
        return proposed(config, media, docs, forbidden) + company
    entries = []
    for item in given:
        aliases = [a for a in [item["name"], *item.get("aliases", [])] if usable_alias(a)]
        entries.append({"name": item["name"], "kind": item.get("kind", "product"), "category": item.get("category", ""),
                        "aliases": list(dict.fromkeys(aliases)), "from": ["kb-config.json"]})
    return entries + company


class Matcher:
    """The product whose name or alias appears in a text; the longest alias wins, so
    "Outdoor Wall mounted Digital Signage" is not filed under "Wall mounted Digital Signage"."""

    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.keys = sorted(((squeeze(a), p) for p in products for a in p["aliases"]), key=lambda kv: len(kv[0]), reverse=True)

    def __call__(self, *texts: str) -> dict[str, Any] | None:
        for text in texts:
            folded = squeeze(text or "")
            for key, product in self.keys:
                if key and key in folded:
                    return product
        return None


def place_media(media: list[dict[str, Any]], match: Matcher) -> dict[str, dict[str, Any]]:
    """item_id -> {"product": ..., "kind": picture kind}. Folders decide first, the file name only after."""
    places = {}
    for entry in media:
        folders = "/".join(parts_of(entry["relpath"]))
        product = match(folders) or match(Path(entry["relpath"]).name)
        places[entry["item_id"]] = {"product": product, "kind": picture_kind(entry["relpath"], entry["kind"])}
    return places


def material_folders(place: dict[str, Any] | None, namer_forbidden: re.Pattern[str] | None) -> list[str] | None:
    """Where a matched picture goes under 90_素材/资料包素材: 大类/产品/图片类型."""
    if not place or not place.get("product"):
        return None
    product = place["product"]
    category = product["category"] or ("解决方案" if product["kind"] == "solution" else "产品")
    # A product folder straight under its category (产品\X100\渲染图) is not written twice.
    parts = [category, place["kind"]] if squeeze(category) == squeeze(product["name"]) else [category, product["name"], place["kind"]]
    return [("其他" if namer_forbidden and namer_forbidden.search(p) else safe_name(p, "其他")) for p in parts]


KIND_WORDS = {"product": "产品", "solution": "方案", "company": "公司"}


def page_name(product: dict[str, Any]) -> str:
    return safe_name(f"{KIND_WORDS.get(product['kind'], '产品')}素材-{product['name']}")


def write_pages(build: Path, products: list[dict[str, Any]], match: Matcher, docs: dict[str, dict[str, Any]],
                published: list[str], statuses: dict[str, str], media: list[dict[str, Any]], places: dict[str, dict[str, Any]],
                pictures: dict[str, dict[str, Any]], escape: Callable[[str], str]) -> dict[str, Any]:
    """One page per product or solution that has anything, an overview, and what matched nothing."""
    hub = build / "vault" / HUB
    by_product: dict[str, dict[str, Any]] = defaultdict(lambda: {"docs": [], "web": [], "pictures": defaultdict(list),
                                                                  "outside": defaultdict(list), "videos": []})
    for doc_id in published:
        doc = docs[doc_id]
        product = match(doc.get("original_name") or "", doc["title"], doc.get("package_path") or "")
        if product:
            by_product[product["name"]]["web" if doc.get("origin") == "website" else "docs"].append(doc)
    unmatched: dict[str, Counter] = defaultdict(Counter)
    for entry in media:
        place = places.get(entry["item_id"], {})
        product = place.get("product")
        folder = "/".join(parts_of(entry["relpath"])[-3:]) or "根目录"
        if not product:
            unmatched[folder][VIDEO if place.get("kind") == VIDEO else "图片"] += 1
            continue
        group = by_product[product["name"]]
        if place["kind"] == VIDEO:
            group["videos"].append(entry)
        elif entry.get("id") in pictures:
            group["pictures"][place["kind"]].append(pictures[entry["id"]])
        else:
            group["outside"][str(Path(entry["package"]) / Path(entry["package_path"]).parent)].append(entry)
    rows = []
    for product in products:
        group = by_product.get(product["name"])
        if not group:
            continue
        counts = {"docs": len(group["docs"]), "web": len(group["web"]),
                  "pictures": sum(len(v) for v in group["pictures"].values()) + sum(len(v) for v in group["outside"].values()),
                  "videos": len(group["videos"])}
        name = page_name(product)
        write_text(hub / f"{name}.md", product_page(product, group, counts, statuses, pictures, escape))
        rows.append((product, name, counts))
    lines = [frontmatter({"doc_id": "HUB-overview", "title": "产品与方案素材总览", "layer": "素材总览", "status": "有效",
                          "visibility": "internal", "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}), "",
             "# 产品与方案素材总览", "",
             "> 每个产品、每个解决方案一页：客户给了哪些资料、官网有哪些页面、有哪些图片和视频。按名字和文件夹归类，不按内容猜；"
             "归错或中英文名没合并的，在 kb-config.json 的 `products` 里写别名（见 SKILL.md）。", "",
             "| 名称 | 类别 | 资料 | 官网页面 | 图片 | 视频 |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    rank = {"product": 0, "solution": 1, "company": 2}
    for product, name, counts in sorted(rows, key=lambda r: (rank.get(r[0]["kind"], 3), r[0]["category"], r[0]["name"])):
        lines.append(f"| [[{name}|{escape(product['name'])}]] | {escape(product['category'])} | {counts['docs']} | {counts['web']} "
                     f"| {counts['pictures']} | {counts['videos']} |")
    if unmatched:
        lines += ["", "## 未归类的素材文件夹", "",
                  "这些文件夹的名字里找不到任何产品或方案名。属于某个产品的，把文件夹里的名字加进那个产品的别名。", "",
                  "| 文件夹（最后三层） | 内容 |", "| --- | --- |"]
        for folder, kinds in sorted(unmatched.items()):
            lines.append(f"| {escape(folder)} | {'、'.join(f'{k} {n}' for k, n in kinds.items())} |")
    write_text(hub / "00_产品与方案素材总览.md", "\n".join(lines) + "\n")
    return {"pages": len(rows), "unmatched_folders": len(unmatched)}


def product_page(product: dict[str, Any], group: dict[str, Any], counts: dict[str, int], statuses: dict[str, str],
                 pictures: dict[str, dict[str, Any]], escape: Callable[[str], str]) -> str:
    kind = KIND_WORDS.get(product["kind"], "产品")
    lines = [frontmatter({"doc_id": f"HUB-{squeeze(product['name'])[:40]}", "title": f"{product['name']} 素材总览", "layer": "素材总览",
                          "status": "有效", "visibility": "internal", "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}), "",
             f"# {product['name']}：客户给的资料和素材", "",
             f"> {kind}素材总览，自动汇总。类别：{product['category'] or '—'}；按这些名字归类：{'、'.join(product['aliases'])}。"
             "回到 [[00_产品与方案素材总览]]。", "",
             f"资料 {counts['docs']} 份 · 官网页面 {counts['web']} 页 · 图片 {counts['pictures']} 张 · 视频 {counts['videos']} 个", ""]
    if group["docs"]:
        lines += ["## 资料", ""]
        lines += [f"- [[{Path(d['file_name']).stem}]]（{d.get('folder', '')}，{statuses.get(d['doc_id'], '')}）"
                  for d in sorted(group["docs"], key=lambda d: d["title"])]
        lines.append("")
    if group["web"]:
        lines += ["## 官网页面", ""]
        lines += [f"- [[{Path(d['file_name']).stem}]]" + (f" · [原网页]({d['package_path']})" if str(d.get("package_path", "")).startswith("http") else "")
                  for d in sorted(group["web"], key=lambda d: d["title"])]
        lines.append("")
    if group["pictures"] or group["outside"]:
        lines += ["## 图片", ""]
        order = [name for name, _ in PICTURE_KINDS] + [OTHER_PICTURES]
        for picture_kind_name in sorted(group["pictures"], key=lambda k: order.index(k) if k in order else len(order)):
            items = sorted(group["pictures"][picture_kind_name], key=lambda e: e["file"])
            lines += [f"### {picture_kind_name}（{len(items)} 张）", ""]
            lines.append(" ".join(f"![[{Path(e['file']).name}|150]]" for e in items[:GALLERY]))
            if len(items) > GALLERY:
                lines += ["", "其余：" + " · ".join(f"[[{Path(e['file']).name}]]" for e in items[GALLERY:])]
            lines.append("")
        if group["outside"]:
            lines += ["### 留在初筛包里的图片", "", "较大的图片不复制进知识库，点“打开文件夹”看原图。", ""]
            for folder, entries in sorted(group["outside"].items()):
                lines.append(f"- {len(entries)} 张 · [打开文件夹]({Path(folder).resolve().as_uri()})")
            lines.append("")
    if group["videos"]:
        lines += ["## 视频", "", "视频不复制进知识库，原件在初筛包。讲的内容转写以后才能当知识用。", "",
                  "| 封面 | 文件 | 大小 | 时长 | 打开 |", "| --- | --- | ---: | --- | --- |"]
        for entry in sorted(group["videos"], key=lambda e: e["relpath"]):
            cover = pictures.get(entry.get("cover") or "")
            shown = f"![[{Path(cover['file']).name}|120]]" if cover else ""
            source = Path(entry["package"]) / entry["package_path"]
            lines.append(f"| {shown} | {escape(Path(entry['relpath']).name)} "
                         f"| {entry.get('bytes', 0) / 1024 / 1024:.1f} MB | {entry.get('duration', '')} "
                         f"| [打开]({source.resolve().as_uri()}) · [文件夹]({source.parent.resolve().as_uri()}) |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def propose(out: Path, config: dict[str, Any]) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    media = load_json(build / "media.json") if (build / "media.json").is_file() else []
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    terms = load_json(build / "guard-terms.json") if (build / "guard-terms.json").is_file() else {}
    from guard_blocks import compile_terms
    found = proposed(config, media, docs, compile_terms(terms.get("hard", []) + terms.get("soft", [])))
    places = place_media(media, Matcher(found + COMPANY_GROUPS))
    left = Counter("/".join(parts_of(m["relpath"])[-3:]) for m in media if not places[m["item_id"]]["product"])
    folder = build / "products"
    write_json(folder / "proposal.json", found)
    lines = ["# 产品和方案清单（草稿）", "",
             "脚本从文件夹名、官网大类、画册和带 Solution/方案 的标题里读出来的名字。同一个产品的中英文名、型号会分成几条。",
             "把它整理好写进 kb-config.json 的 \"products\"：同一个产品合成一条，其他名字和型号写进 aliases；不是产品的删掉。", "",
             '例：{"name": "壁挂广告机", "category": "广告机", "kind": "product", "aliases": ["Wall mounted Digital Signage", "DS20", "DS50"]}', "",
             "| 名字 | 类别 | 产品/方案 | 来自 |", "| --- | --- | --- | --- |"]
    lines += [f"| {e['name']} | {e['category']} | {'方案' if e['kind'] == 'solution' else '产品'} | {'、'.join(e['from'])} |" for e in found]
    if left:
        lines += ["", "## 还没归到任何产品的素材文件夹", "",
                  "文件夹名里有产品名或型号的，把那个名字加进对应产品的 aliases；是新产品就加一条。工厂、公司、展会、案例、证书有固定分组，不用写。", "",
                  "| 文件夹（最后三层） | 文件数 |", "| --- | ---: |"]
        lines += [f"| {f} | {n} |" for f, n in left.most_common()]
    write_text(folder / "README.md", "\n".join(lines) + "\n")
    return {"proposed": len(found), "unplaced_folders": len(left), "folder": str(folder)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Propose the product and solution list for the material overview.")
    parser.add_argument("command", choices=["propose"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", help="kb-config.json")
    args = parser.parse_args()
    config = load_config(Path(args.config) if args.config else None)
    print(json.dumps(propose(Path(args.out).resolve(), config), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
