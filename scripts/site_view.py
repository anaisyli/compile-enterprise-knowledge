#!/usr/bin/env python3
"""建站资料视图: the client's material laid out the way a website is, page by page.

For each page of a company website (首页, 关于我们 and its parts, products by 大类 / 小类 / 产品,
解决方案, 服务, 案例, 资源中心, 博客, FAQ, 联系我们) the view lists what the client gave for it:
documents already cleaned (link in the vault, and the original), pictures by kind, videos, the
website's own pages, and files that look related but are not processed yet, with why.

Where something belongs is read from names only, never guessed from content:
- under the package's 产品 folder, the folder levels are 大类 / 小类 / 产品; folders that say what is
  inside (白底图, 实拍图, 英文版, "1.上架推广资料") are kinds of picture or batches, not levels;
- the same product given in several rounds (DS50系列1, DS50系列2) is one product, its rounds listed;
- kb-config.json "products" can rename and merge: {"name", "category", "sub", "aliases"};
- other pages by words in folder names and titles (公司简介, 工厂, 证书, 案例, Solution, OEM, FAQ…);
- what fits nothing goes to 待归类 with the reason. Nothing is forced into a page.

home-candidates export / collect: the model picks home-page candidates from the products' main
pictures; they are listed on 首页 as 候选，待确认 until a person confirms them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

from kb_common import BUILD_DIRECTORY, SKILL_VERSION, frontmatter, load_json, load_jsonl, safe_name, write_json, write_jsonl, write_text
from guard_blocks import HARD_PATTERNS
from products import NOT_A_NAME, OTHER_PICTURES, PICTURE_KINDS, PRODUCT_ROOT, VIDEO, parts_of, picture_kind, squeeze

ROOT = "03_正式知识/80_建站资料"
OVERVIEW = "00_建站资料总览"
HOME_STATE = "site-home.json"
HOME_FOLDER = "site-home"
GALLERY = 8
# A round of the same product: DS50系列1, DS50系列2, 第二批, (2), v2, 新版.
# A number glued to Latin letters is a model (DS20), not a round, so it stays.
ROUND = re.compile(r"(?:(?<=[一-鿿])\d{1,2}|[\s_-]+\d{1,2}|[（(]\d+[)）]|[\s_-]*第[一二三四五六七八九十\d]+[批轮版期]|[\s_-]+v\d+|[\s_-]*(?:新版|旧版|最新))$", re.I)
# A model code inside a name: DS50 in "DS50系列" (Chinese letters count as word characters, so no \b).
MODEL_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{2,4}(?![0-9])")
KIND_FOLDER = re.compile("|".join(p.pattern.replace("(?i)", "") for _, p in PICTURE_KINDS)
                         + r"|实拍|规格书|说明书|视频|图片|照片|图$|^新建文件夹|^new folder|^未命名|^untitled|副本$|^copy of", re.I)
RESOURCE = re.compile(r"(?i)画册|catalog|brochure|目录|规格书|规格|spec|datasheet|说明书|manual|guide|下载|download")
UNPROCESSED = {"awaiting_sample": "等样例确认后提取", "unresolved_item": "D 类待决定", "no_extractable_content": "读不出文字",
               "image_needs_review": "图片还没识别文字", "media_awaiting_client": "整个文件夹待客户确认", "needs_libreoffice": "要 LibreOffice 才能读",
               "failed": "提取失败", "awaiting_client": "待客户确认"}

# key, 中文, English, words in folder names or titles. Earlier entries win; products are decided first.
SECTIONS = [
    ("home", "首页", "Home", r"(?i)banner|首页|轮播|宣传片|宣传视频|homepage|home\s*page|\bhero\b"),
    ("cases", "案例", "Cases", r"(?i)案例|case|客户合作|合照|project"),
    ("solutions", "解决方案", "Solutions", r"(?i)solutions?\b|解决方案"),
    ("services", "服务", "Services", r"(?i)services?\b|服务|(?<![a-z])(?:oem|odm)(?![a-z])|代工|定制|customi[sz]|打样|售后|warranty|support"),
    ("faq", "FAQ", "FAQ", r"(?i)\bfaq\b|常见问题|问答|questions?"),
    ("blog", "博客 / 新闻", "Blog & News", r"(?i)\bblog|\bnews\b|新闻|资讯|文章|article"),
    ("contact", "联系我们", "Contact Us", r"(?i)contact|联系"),
    ("about-factory", "工厂与生产", "Factory", r"(?i)工厂|factory|车间|workshop|生产线|production|装柜|发货|出货|warehouse|仓库"),
    ("about-rnd", "研发", "R&D", r"(?i)研发|r\s*&\s*d|research|专利|patent|实验室|\blab\b"),
    ("about-certs", "资质认证", "Certificates", r"(?i)证书|资质|认证|certif|\biso\b|\bce\b|\bfcc\b|\brohs\b"),
    ("about-exhibitions", "展会", "Exhibitions", r"(?i)展会|exhibition|(?<![a-z])[il]se\s*20\d\d|\bexpo\b|\bfair\b|canton"),
    ("about-profile", "企业介绍", "Company Profile", r"(?i)公司简介|公司介绍|关于我们|\babout\b|公司照片|公司视频|展厅|团队|\bteam\b|企业介绍|品牌故事|why\s*\w*"),
]
ABOUT = [key for key, *_ in SECTIONS if key.startswith("about-")]
PAGES = {key: (cn, en) for key, cn, en, _ in SECTIONS}
PAGES.update({"about": ("关于我们", "About Us"), "products": ("产品", "Products"), "resources": ("资源中心", "Resources"),
              "unsorted": ("待归类", "To Sort")})
ORDER = ["home", "about", *ABOUT, "products", "solutions", "services", "cases", "resources", "blog", "faq", "contact", "unsorted"]
MATCHERS = [(key, re.compile(pattern)) for key, _, _, pattern in SECTIONS]
# What a page of each kind usually has; missing ones are listed under 还缺什么.
EXPECTED = {
    "product": [("白底图", "白底图"), ("实物图", "实拍或实物图"), ("规格书", "规格书或参数表"), (VIDEO, "产品视频")],
    "about-profile": [("doc", "公司介绍文字")], "about-factory": [("picture", "工厂照片")], "about-certs": [("picture", "证书图片")],
    "faq": [("doc", "问答资料")], "contact": [("doc", "联系方式")],
}


def risky(text: str, forbidden: re.Pattern[str] | None) -> bool:
    """A name not to print: a customer's or denied word, or a price, a payment or trade term."""
    return bool(text) and (bool(forbidden and forbidden.search(text)) or any(p.search(text) for p in HARD_PATTERNS.values()))


MASKED = "（名字含客户或报价信息，见对照表）"


def page_title(key: str) -> str:
    cn, en = PAGES[key]
    return f"{cn} {en}" if cn != en else cn


def file_stem(key: str) -> str:
    """Every page name starts with 建站-: no clash with the knowledge pages (产品大类-…) or the sources."""
    return safe_name(f"建站-{page_title(key)}".replace(" / ", "、"))  # A slash would cut the name at the folder line.


def round_base(name: str) -> str:
    base = ROUND.sub("", name).strip(" -_")
    return base if len(squeeze(base)) >= 2 and base != name else name


class Products:
    """The product tree from the package's 产品 folder, renamed and merged by the config's products."""

    def __init__(self, config: dict[str, Any], forbidden: re.Pattern[str] | None) -> None:
        self.forbidden = forbidden
        self.leaves: dict[tuple[str, str, str], dict[str, Any]] = {}
        # A person's names decide even inside the 产品 folder; names read off folders only find files elsewhere.
        self.configured: list[tuple[str, tuple[str, str, str]]] = []
        self.aliases: list[tuple[str, tuple[str, str, str]]] = []
        # Folders a person named exactly: (小类 folder, folder) -> product. For names like 01…11, which as
        # aliases would also match every date and number in other paths.
        self.exact: dict[tuple[str, str], tuple[str, str, str]] = {}
        for item in config.get("products") or []:
            key = (item.get("category") or "产品", item.get("sub") or item["name"], item["name"] if item.get("sub") else "")
            self.leaves[key] = {"rounds": set(), "aliases": [item["name"], *item.get("aliases", [])], "note": item.get("note", "")}
            self.configured += [(squeeze(a), key) for a in [item["name"], *item.get("aliases", [])] if len(squeeze(a)) >= 2]
            for folder in item.get("folders", []):
                self.exact[(squeeze(item.get("sub") or ""), squeeze(folder))] = key
        self.aliases += self.configured
        self.questions: dict[str, set[str]] = defaultdict(set)

    def clean(self, name: str) -> str:
        return "其他" if risky(name, self.forbidden) else name

    def from_folders(self, relpath: str) -> tuple[tuple[str, str, str], str] | None:
        """(category, sub, product) and the round's folder, for a file under the 产品 folder."""
        parts = parts_of(relpath)
        root = next((i for i, p in enumerate(parts) if PRODUCT_ROOT.match(p)), None)
        if root is None or root + 1 >= len(parts):
            return None
        named = [p for p in parts[root + 2:] if not NOT_A_NAME.match(p) and not KIND_FOLDER.search(p)]
        category = self.clean(parts[root + 1])
        sub = self.clean(named[0]) if named else ""
        model = self.clean(round_base(named[1])) if len(named) > 1 else ""
        # A person's names rename a level; they never flatten the levels below it.
        hit_model = (self.exact.get((squeeze(named[0]), squeeze(named[1]))) or self.by_alias([named[1]], self.configured)) if len(named) > 1 else None
        hit_sub = self.by_alias([named[0]], self.configured) if named else None
        if hit_model:
            key = hit_model
        elif hit_sub:
            key = hit_sub if not model or hit_sub[2] else (hit_sub[0], hit_sub[1], model)
        else:
            key = (category, sub, model)
            if model and re.fullmatch(r"\d{1,3}", model):
                self.questions[f"{category} / {sub}"].add(model)  # 01…11: models or rounds? A person says.
        leaf = self.leaves.setdefault(key, {"rounds": set(), "aliases": []})
        round_name = named[1] if len(named) > 1 else (named[0] if named else category)
        leaf["rounds"].add(round_name)
        # Names found here find the same product elsewhere: a spec sheet in another folder, a website page.
        for name, target in ((sub, (category, sub, "")), (model, key)):
            code = MODEL_CODE.search(name or "")
            for alias in filter(None, [name, code.group(0) if code else ""]):
                if len(squeeze(alias)) >= 4 and (squeeze(alias), target) not in self.aliases:
                    self.aliases.append((squeeze(alias), target))
        return key, round_name

    def by_alias(self, segments: list[str], aliases: list[tuple[str, tuple[str, str, str]]] | None = None) -> tuple[str, str, str] | None:
        """The deepest folder naming a product wins; in one folder, the longest name."""
        for segment in reversed(segments):
            folded = squeeze(segment)
            hits = [(len(a), key) for a, key in (self.aliases if aliases is None else aliases) if a and a in folded]
            if hits:
                return max(hits)[1]
        return None


def section_of(text: str) -> str | None:
    return next((key for key, pattern in MATCHERS if pattern.search(text)), None)


def write_site(build: Path, config: dict[str, Any], routes: dict[str, Any], docs: dict[str, dict[str, Any]], order: list[str],
               statuses: dict[str, str], originals: dict[str, str], media: list[dict[str, Any]], pictures: dict[str, dict[str, Any]],
               forbidden: re.Pattern[str] | None, escape: Callable[[str], str]) -> dict[str, Any]:
    tree = Products(config, forbidden)

    def shown(name: str) -> str:
        return MASKED if risky(name, forbidden) else name
    pages: dict[Any, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    reasons: dict[str, str] = {}

    def place(item: dict[str, Any], relpath: str, title: str = "") -> None:
        """Put one thing on its page(s): a product, a section, or 待归类."""
        found = tree.from_folders(relpath)
        if found:
            key, round_name = found
            item["round"] = shown(round_name)
            pages[("product", key)][item["what"]].append(item)
            return
        segments = parts_of(relpath) + [Path(relpath).stem, title]
        # A whole folder or title a person named; a part would also catch “About Us-<site name>”.
        names = {squeeze(s) for s in segments if s}
        chosen = next((k for k, words in (config.get("site_sections") or {}).items()
                       if k in PAGES and any(squeeze(w) in names for w in words)), None)
        if chosen:
            item["round"] = shown((parts_of(relpath) or [""])[-1])
            pages[chosen][item["what"]].append(item)  # A person put this folder in a section: that decides.
            return
        key = tree.by_alias(segments)
        if key:
            item["round"] = shown((parts_of(relpath) or ["官网"])[-1])
            pages[("product", key)][item["what"]].append(item)
            return
        section = section_of(" ".join(segments))
        if section:
            item["round"] = shown((parts_of(relpath) or [""])[-1])
            pages[section][item["what"]].append(item)
        else:
            item["round"] = shown("/".join(parts_of(relpath)[-2:]) or "根目录")
            reasons.setdefault(item["round"], "文件夹名和标题里没有产品名或栏目名")
            pages["unsorted"][item["what"]].append(item)

    # First learn the product tree from every path, so a document read before its product's pictures still finds it.
    for path in [r.get("source_relpath") or r.get("path") or "" for r in routes.values()] + [m["relpath"] for m in media]:
        tree.from_folders(path)
    picture_of = {entry["item_id"]: entry.get("id") for entry in media}
    for doc_id in order:
        doc = docs[doc_id]
        route = routes.get(doc["item_id"], {})
        relpath = route.get("source_relpath") or doc.get("package_path") or ""
        web = doc.get("origin") == "website"
        if web:
            relpath = f"官网/{doc.get('folder', '').split('/', 1)[-1]}/{doc.get('original_name') or doc['title']}.html"
        item = {"what": "web" if web else "docs", "title": doc["title"], "stem": Path(doc["file_name"]).stem,
                "status": statuses.get(doc_id, ""), "original": originals.get(doc_id) or (doc.get("package_path") if web else ""),
                "folder": doc.get("folder", "")}
        place(item, relpath, doc.get("original_name") or "")
        if not web and (doc.get("folder") == "产品画册" or RESOURCE.search(f"{relpath} {doc['title']}")):
            pages["resources"]["docs"].append({**item, "round": doc.get("folder", "")})
    for picture_id, entry in pictures.items():
        if entry["use"] == "内部" or not entry["doc_id"] or entry["doc_id"] not in docs:
            continue  # Folder-kept pictures come below with their folders; quotes and page shots stay out.
        doc = docs[entry["doc_id"]]
        relpath = routes.get(doc["item_id"], {}).get("source_relpath") or doc.get("package_path") or ""
        if doc.get("origin") == "website":
            relpath = f"官网/{doc.get('folder', '').split('/', 1)[-1]}/{doc.get('original_name') or doc['title']}.html"
        place({"what": "pictures", "file": entry["file"], "alt": entry["alt"], "use": entry["use"],
               "kind": picture_kind(entry["file"], "image")}, relpath, doc.get("original_name") or doc["title"])
    for entry in media:
        relpath = entry["relpath"]
        folder_uri = (Path(entry["package"]) / entry["package_path"]).parent.resolve().as_uri()
        hidden = risky(relpath, forbidden)
        if entry["kind"] in {"video", "audio"}:
            source = (Path(entry["package"]) / entry["package_path"]).resolve().as_uri()
            cover = pictures.get(entry.get("cover") or "")
            place({"what": "videos", "name": MASKED if hidden else Path(relpath).name, "bytes": entry.get("bytes", 0),
                   "duration": entry.get("duration", ""), "open": "" if hidden else source, "folder_uri": "" if hidden else folder_uri,
                   "cover": cover["file"] if cover else "", "waiting": entry["status"] == "media_awaiting_client"}, relpath)
        elif (entry.get("id") or picture_of.get(entry["item_id"])) in pictures:
            picture = pictures[entry.get("id") or picture_of[entry["item_id"]]]
            place({"what": "pictures", "file": picture["file"], "alt": picture["alt"], "use": picture["use"],
                   "kind": entry.get("picture_kind") or picture_kind(relpath, "image"), "folder_uri": "" if hidden else folder_uri}, relpath)
        else:
            reason = (UNPROCESSED["media_awaiting_client"] if entry["status"] == "media_awaiting_client"
                      else "图纸，没能转成图片" if entry["status"] == "drawing_unrendered" else "图片较大，原图留在初筛包")
            place({"what": "waiting", "name": MASKED if hidden else Path(relpath).name, "reason": reason,
                   "open": "" if hidden else folder_uri}, relpath)
    published_items = {docs[d]["item_id"] for d in order}
    for item_id, route in routes.items():
        reason = UNPROCESSED.get(route.get("reason") or "") or UNPROCESSED.get(route.get("status") or "")
        if item_id in published_items or route.get("route") == "media" or not reason or route.get("decision") == "E":
            continue
        relpath = route.get("source_relpath") or route.get("path") or ""
        hidden = risky(relpath, forbidden)
        # A file with no path of its own (say, inside an archive) has no folder to open.
        source = Path(route["package"]) / route["path"] if route.get("package") and route.get("path") else None
        place({"what": "waiting", "name": MASKED if hidden else (route.get("display_name") or Path(relpath).name),
               "reason": reason, "open": "" if hidden or not source else source.parent.resolve().as_uri()}, relpath)

    home = load_json(build / HOME_STATE) if (build / HOME_STATE).is_file() else {}
    root = build / "vault" / ROOT
    counts = write_pages(root, tree, pages, home, pictures, reasons, escape, cus_questions(build))
    write_json(build / "site-view.json", {"pages": counts, "questions": {k: sorted(v) for k, v in tree.questions.items()}})
    return {"pages": len(counts)}


def cus_questions(build: Path) -> list[str]:
    path = build / "governance.json"
    return [r.get("title", "") for r in (load_json(path).get("CUS", []) if path.is_file() else []) if r.get("title")]


def progress(groups: dict[str, list]) -> str:
    docs = len(groups.get("docs", []))
    return (f"资料 {docs} 份 · 官网页面 {len(groups.get('web', []))} 页 · 图片 {len(groups.get('pictures', []))} 张 · "
            f"视频 {len(groups.get('videos', []))} 个 · 还没处理 {len(groups.get('waiting', []))} 个")


def body(groups: dict[str, list], escape: Callable[[str], str], expected: list[tuple[str, str]] | None = None) -> list[str]:
    lines = [progress(groups), ""]
    if groups.get("docs"):
        lines += ["## 已处理的文件", "", "| 资料 | 状态 | 原件 | 来自 |", "| --- | --- | --- | --- |"]
        for item in sorted(groups["docs"], key=lambda i: (i.get("round", ""), i["title"])):
            opener = f"[打开原件]({item['original']})" if item.get("original") else "见初筛包对照表"
            lines.append(f"| [[{item['stem']}]] | {item['status']} | {opener} | {escape(item.get('round', ''))} |")
        lines.append("")
    if groups.get("web"):
        lines += ["## 官网页面", ""]
        lines += [f"- [[{i['stem']}]]" + (f" · [原网页]({i['original']})" if str(i.get("original", "")).startswith("http") else "")
                  for i in sorted(groups["web"], key=lambda i: i["title"])]
        lines.append("")
    if groups.get("pictures"):
        lines += ["## 图片", ""]
        order = [name for name, _ in PICTURE_KINDS] + [OTHER_PICTURES]
        by_kind: dict[str, list] = defaultdict(list)
        for item in groups["pictures"]:
            by_kind[item["kind"]].append(item)
        for kind in sorted(by_kind, key=lambda k: order.index(k) if k in order else len(order)):
            items = sorted(by_kind[kind], key=lambda i: i["file"])
            lines += [f"### {kind}（{len(items)} 张）", "", " ".join(f"![[{Path(i['file']).name}|140]]" for i in items[:GALLERY])]
            if len(items) > GALLERY:
                lines += ["", "其余：" + " · ".join(f"[[{Path(i['file']).name}]]" for i in items[GALLERY:])]
            folders = sorted({i["folder_uri"] for i in items if i.get("folder_uri")})
            if folders:
                lines += ["", "原图所在文件夹：" + " · ".join(f"[打开]({uri})" for uri in folders[:6]) + (" 等" if len(folders) > 6 else "")]
            lines.append("")
    if groups.get("videos"):
        lines += ["## 视频", "", "| 封面 | 文件 | 大小 | 时长 | 打开 |", "| --- | --- | ---: | --- | --- |"]
        for item in sorted(groups["videos"], key=lambda i: i["name"]):
            cover = f"![[{Path(item['cover']).name}|100]]" if item.get("cover") else ""
            links = " · ".join(filter(None, [f"[视频]({item['open']})" if item.get("open") else "", f"[文件夹]({item['folder_uri']})" if item.get("folder_uri") else ""]))
            lines.append(f"| {cover} | {escape(item['name'])}{'（待客户确认）' if item.get('waiting') else ''} "
                         f"| {item['bytes'] / 1024 / 1024:.1f} MB | {item.get('duration', '')} | {links or '见对照表'} |")
        lines.append("")
    if groups.get("waiting"):
        lines += ["## 相关但还没处理的文件", "", "| 文件 | 为什么还没处理 | 原件 |", "| --- | --- | --- |"]
        for item in sorted(groups["waiting"], key=lambda i: (i["reason"], i["name"]))[:200]:
            opener = f"[打开文件夹]({item['open']})" if item.get("open") else "见对照表"
            lines.append(f"| {escape(item['name'])} | {item['reason']} | {opener} |")
        if len(groups["waiting"]) > 200:
            lines.append(f"| 另有 {len(groups['waiting']) - 200} 个 | | |")
        lines.append("")
    if expected:
        kinds = {i["kind"] for i in groups.get("pictures", [])} | ({VIDEO} if groups.get("videos") else set())
        names = " ".join(i.get("title", "") for i in groups.get("docs", []))
        missing = [label for need, label in expected if (need == "doc" and not groups.get("docs")) or (need == "picture" and not groups.get("pictures"))
                   or (need == "规格书" and not re.search(r"规格|spec|参数", names, re.I)) or (need not in {"doc", "picture", "规格书"} and need not in kinds)]
        if missing:
            lines += ["## 还缺什么", "", *[f"- {label}" for label in missing], ""]
    return lines


def header(key: str, title: str, note: str = "") -> list[str]:
    return [frontmatter({"doc_id": f"SITE-{squeeze(key)[:40]}", "title": title, "layer": "建站资料视图", "status": "有效",
                         "visibility": "internal", "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}), "",
            f"# {title}", "", f"> 建站资料视图，每次建库自动更新。{note}回到 [[{OVERVIEW}]]。", ""]


def write_pages(root: Path, tree: Products, pages: dict, home: dict[str, Any], pictures: dict[str, dict[str, Any]],
                reasons: dict[str, str], escape: Callable[[str], str], questions: list[str]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    nav: list[str] = []

    def note(key: str, groups: dict[str, list]) -> None:
        counts[key] = {what: len(items) for what, items in groups.items()}

    # Sections
    for key in ORDER:
        if key in {"about", "products"}:
            continue
        groups = pages.get(key, {})
        title = page_title(key)
        extra: list[str] = []
        if key == "home":
            confirmed = [p for p, v in home.items() if v.get("confirmed") and p in pictures]
            candidates = [p for p, v in home.items() if not v.get("confirmed") and p in pictures]
            if confirmed or candidates:
                extra += ["## AI 从产品主图里挑的候选", ""]
                for picture_id in confirmed + candidates:
                    flag = "已确认" if picture_id in confirmed else "候选，待确认"
                    extra.append(f"- ![[{Path(pictures[picture_id]['file']).name}|160]] {flag}：{escape(home[picture_id].get('reason', ''))}")
                extra.append("")
        if key == "faq":
            extra += ["## 待补充问题", "", "客户确认表里在问、资料里还没写清楚的事，建站写 FAQ 时可以从这里挑：", ""]
            extra += [f"- {escape(q)}" for q in questions[:40]] or ["- （暂无）"]
            extra.append("")
        if key == "unsorted":
            extra += ["归不进任何栏目的文件夹和文件，原因写在这里。属于某个产品的，把文件夹名加进 kb-config.json 的 products 别名；"
                      "属于某个栏目的，告诉 AI 改规则。不为了分完而硬分。", ""]
            if reasons:
                extra += ["| 文件夹 | 原因 |", "| --- | --- |", *[f"| {escape(f)} | {r} |" for f, r in sorted(reasons.items())], ""]
        if key.startswith("about-"):
            stem = safe_name(f"建站-关于我们-{PAGES[key][0]} {PAGES[key][1]}")
        else:
            stem = file_stem(key)
        lines = header(key, title if not key.startswith("about-") else f"关于我们 · {title}") + body(groups, escape, EXPECTED.get(key)) + extra
        write_text(root / f"{stem}.md", "\n".join(lines).rstrip() + "\n")
        note(key, groups)
        nav.append(f"- [[{stem}|{title}]]：{progress(groups)}")
    about_stem = file_stem("about")
    about = header("about", page_title("about")) + [
        f"- [[{safe_name(f'建站-关于我们-{PAGES[k][0]} {PAGES[k][1]}')}|{page_title(k)}]]：{progress(pages.get(k, {}))}" for k in ABOUT]
    write_text(root / f"{about_stem}.md", "\n".join(about) + "\n")
    # Products: 大类 / 小类 / 产品
    tree_lines: list[str] = []
    by_category: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for (category, sub, model) in sorted({k[1] for k in pages if isinstance(k, tuple)} | set(tree.leaves)):
        by_category[category][sub].append(model)
    for category, subs in sorted(by_category.items()):
        tree_lines.append(f"- **{escape(category)}**")
        for sub, models in sorted(subs.items()):
            if sub and not any(pages.get(("product", (category, sub, "")), {}).values()):
                tree_lines.append(f"  - {escape(sub)}")  # A 小类 with products only, no material of its own.
            for model in sorted(set(models)):
                key = (category, sub, model)
                groups = pages.get(("product", key), {})
                if not any(groups.values()):
                    continue
                name = " / ".join(filter(None, [category, sub, model]))
                stem = safe_name(f"建站-产品-{name.replace(' / ', '-')}")
                rounds = sorted(tree.leaves.get(key, {}).get("rounds", set()))
                note_text = f"客户分 {len(rounds)} 批给的资料：{'、'.join(rounds)}，下面合在一起。" if len(rounds) > 1 else ""
                # Where the name came from, said where the name is shown: a name the AI read off file names is a guess.
                named_by = tree.leaves.get(key, {}).get("note", "")
                if named_by:
                    note_text += f"名称说明：{named_by}（原文件夹：{'、'.join(rounds) or '—'}）。"
                lines = header(f"product-{name}", f"产品 · {name}", note_text) + body(groups, escape, EXPECTED["product"])
                write_text(root / "产品" / f"{stem}.md", "\n".join(lines).rstrip() + "\n")
                counts[f"产品/{name}"] = {what: len(items) for what, items in groups.items()}
                indent = "    " if model else "  "
                mark = f"（{named_by}）" if named_by else ""
                tree_lines.append(f"{indent}- [[{stem}|{escape(model or sub or category)}]]{mark}：{progress(groups)}")
    questions_lines = [f"- {escape(where)} 下面的编号文件夹 {'、'.join(sorted(set(v)))}：是不同型号，还是同一产品分几批给的？" for where, v in tree.questions.items()]
    products = header("products", page_title("products")) + ["按客户建站资料里“产品”文件夹的层级：大类 / 小类 / 产品。", "", *tree_lines]
    if questions_lines:
        products += ["", "## 要人确认的", "", *questions_lines]
    write_text(root / f"{file_stem('products')}.md", "\n".join(products) + "\n")
    # Overview
    lines = [frontmatter({"doc_id": "SITE-overview", "title": "建站资料总览", "layer": "建站资料视图", "status": "有效", "visibility": "internal",
                          "generated_by": f"compile-enterprise-knowledge {SKILL_VERSION}"}), "", "# 建站资料总览", "",
             "> 按网站栏目排的客户资料：每页能用哪些文件、图片、视频，处理到哪一步，原件在哪，还有哪些没处理。"
             "按文件夹名和标题归类，不看内容猜；归不了的在“待归类”。", "",
             f"- [[{file_stem('home')}|{page_title('home')}]]", f"- [[{about_stem}|{page_title('about')}]]",
             *[f"  - [[{safe_name(f'建站-关于我们-{PAGES[k][0]} {PAGES[k][1]}')}|{page_title(k)}]]" for k in ABOUT],
             f"- [[{file_stem('products')}|{page_title('products')}]]", *[f"  {line}" for line in tree_lines],
             *[f"- [[{file_stem(k)}|{page_title(k)}]]" for k in ORDER if k not in {"home", "about", "products", *ABOUT}], "",
             "## 各页进度", "", *nav]
    if questions_lines:
        lines += ["", "## 要人确认的", "", *questions_lines]
    write_text(root / f"{OVERVIEW}.md", "\n".join(lines) + "\n")
    return counts


HOME_README = """# 首页候选图

`todo.jsonl` 每行一张图：各产品的主图、白底图、效果图里挑出来的。请逐张看，挑适合放在网站首页（Banner、主推产品区）的：
画面完整、主体清楚、没有水印和杂乱背景、能代表这个产品。每个产品最多挑 2 张。

写进同目录 `answers.jsonl`，每行一个：{"id": "img-…", "reason": "白底完整，适合主推产品区"}
不合适的不用写。写完运行 `site_view.py home-candidates collect --out <运行目录>`，再重跑构建。
挑出来的在《首页 Home》里标“候选，待确认”，人确认后（告诉 AI 或在 site-home.json 里把 confirmed 改成 true）才算首页素材。
"""


def export_home(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    pictures = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
    wanted = re.compile(r"白底|主图|效果|渲染|white|main|render", re.I)
    todo = [{"id": i, "path": str(build / "assets" / i), "file": e["file"]} for i, e in sorted(pictures.items())
            if e["use"] != "内部" and e["file"].startswith(("资料包素材/", "产品/")) and wanted.search(e["file"])]
    folder = build / HOME_FOLDER
    write_jsonl(folder / "todo.jsonl", todo)
    write_text(folder / "README.md", HOME_README)
    return {"todo": len(todo), "folder": str(folder)}


def collect_home(out: Path) -> dict[str, Any]:
    build = out / BUILD_DIRECTORY
    todo = {r["id"] for r in load_jsonl(build / HOME_FOLDER / "todo.jsonl")}
    state = load_json(build / HOME_STATE) if (build / HOME_STATE).is_file() else {}
    taken = 0
    for row in load_jsonl(build / HOME_FOLDER / "answers.jsonl"):
        if row.get("id") in todo:
            state.setdefault(row["id"], {"confirmed": False})["reason"] = (row.get("reason") or "")[:80]
            taken += 1
    write_json(build / HOME_STATE, state)
    return {"candidates": taken}


def update(out: Path, vault: Path) -> dict[str, Any]:
    """Write only the site view into a published vault, from what the last build already made.

    Nothing is extracted, read, or uploaded again, and nothing outside 80_建站资料 is touched.
    The pages go into the publish receipt, so the next full publish keeps treating them as its own."""
    from datetime import datetime
    from guard_blocks import compile_terms
    from kb_common import load_config
    from publish_vault import ARCHIVE, RECEIPT, archive, normalized_hash
    from render_kb import ALIAS_FOLDER, escape_cell

    build = out / BUILD_DIRECTORY
    for name in ("routes.json", "docs.jsonl", "published.json", "statuses.json"):
        if not (build / name).is_file():
            raise SystemExit(f"运行目录里没有 {name}：要用发布这个知识库的那次运行目录（知识库构建记录里的 run）。")
    receipt_path = vault / RECEIPT
    if not receipt_path.is_file():
        raise SystemExit("知识库里没有发布记录，先正常发布一次。")
    # The run's config and the one kept in the vault, merged entry by entry like a publish does:
    # a product list written into the vault after that run still counts.
    copies = [p for p in (build / "kb-config.json", vault / "05_数据与审核/70_构建状态/kb-config.json") if p.is_file()]
    config: dict[str, Any] = {}
    for path in sorted(copies, key=lambda p: p.stat().st_mtime):
        config.update(load_json(path))
    config = {**load_config(None), **config}
    for name in ("site-home.json",):
        kept = vault / "05_数据与审核/70_构建状态" / name
        if kept.is_file() and not (build / name).is_file():
            (build / name).write_bytes(kept.read_bytes())
    routes = load_json(build / "routes.json")
    docs = {d["doc_id"]: d for d in load_jsonl(build / "docs.jsonl")}
    published = load_json(build / "published.json")
    statuses = load_json(build / "statuses.json")
    order = [d for d in docs if d in published]
    media = load_json(build / "media.json") if (build / "media.json").is_file() else []
    pictures = load_json(build / "pictures.json") if (build / "pictures.json").is_file() else {}
    terms = load_json(build / "guard-terms.json") if (build / "guard-terms.json").is_file() else {}
    forbidden = compile_terms(terms.get("hard", []) + terms.get("soft", []))
    # The one-click originals the last build made; none is made here.
    originals = {}
    for doc_id in order:
        route = routes.get(docs[doc_id]["item_id"], {})
        if route.get("package") and route.get("path"):
            alias = Path(route["package"]) / ALIAS_FOLDER / f"{Path(docs[doc_id]['file_name']).stem}{Path(route['path']).suffix}"
            if alias.is_file():
                originals[doc_id] = alias.resolve().as_uri()
    site = build / "vault" / ROOT
    if site.exists():
        import shutil
        shutil.rmtree(site)
    write_site(build, config, routes, docs, order, statuses, originals, media, pictures, forbidden, escape_cell)
    receipt = load_json(receipt_path)
    files: dict[str, str] = receipt.setdefault("files", {})
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived: list[str] = []
    planned = {(Path(ROOT) / p.relative_to(site)).as_posix(): p for p in site.rglob("*.md")}
    for key, source in planned.items():
        target = vault / key
        if target.exists() and key not in files:
            raise SystemExit(f"目标位置已有不是本 Skill 写的文件，不覆盖：{key}")
        if target.exists() and normalized_hash(target) != files.get(key):
            raise SystemExit(f"这个页面被人改过，先把改动移到单独的文件再更新：{key}")
        if target.exists() and normalized_hash(target) != normalized_hash(source):
            archive(vault, key, stamp, archived)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        files[key] = normalized_hash(target)
    for key in [k for k in files if k.startswith(ROOT + "/") and k not in planned]:
        if (vault / key).is_file():
            archive(vault, key, stamp, archived, move=True)  # A page no longer made, say a product renamed.
        files.pop(key)
    write_json(receipt_path, receipt)
    if (build / HOME_STATE).is_file():
        write_json(vault / "05_数据与审核/70_构建状态" / HOME_STATE, load_json(build / HOME_STATE))
    if archived:
        write_text(vault / ARCHIVE / stamp / "00_版本说明.md", f"# 版本说明 {stamp}\n\n只更新建站资料视图，归档的旧版：\n\n"
                   + "\n".join(f"- {k}" for k in sorted(set(archived))) + "\n")
    view = load_json(build / "site-view.json")
    return {"pages": len(planned), "archived": len(set(archived)), "questions": view.get("questions", {}),
            "folder": str(vault / ROOT)}


def main() -> int:
    parser = argparse.ArgumentParser(description="The site view: update it alone in a published vault, or pick home-page candidates.")
    parser.add_argument("command", choices=["update", "home-candidates"])
    parser.add_argument("action", nargs="?", choices=["export", "collect"])
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--vault", help="Published project (update)")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if args.command == "update":
        if not args.vault:
            parser.error("update 要指定 --vault")
        report = update(out, Path(args.vault).resolve())
    elif args.action == "export":
        report = export_home(out)
    elif args.action == "collect":
        report = collect_home(out)
    else:
        parser.error("home-candidates 后面写 export 或 collect")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
