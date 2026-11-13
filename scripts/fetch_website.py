#!/usr/bin/env python3
"""Read the client's own website into page-level units, as an official (T1) source.

Only hosts listed in kb-config.json are visited, robots.txt is respected, links to other
sites are never followed, and the crawl stops at a page limit. Pages built by JavaScript
come back nearly empty; save them with a browser and import the capture instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

from kb_common import BUILD_DIRECTORY, MODULES, SKILL_VERSION, load_config, load_json, normalize_space, short_hash, write_json, write_text
from kb_state import restore


USER_AGENT = f"compile-enterprise-knowledge/{SKILL_VERSION} (+client website reader)"
SKIP_EXTENSIONS = re.compile(r"(?i)\.(?:jpe?g|png|gif|webp|svg|ico|pdf|zip|rar|mp4|mov|mp3|css|js|xml|json|docx?|xlsx?|pptx?)$")
SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe", "select", "button"}
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "figcaption", "dt", "dd", "blockquote"}
HEADINGS = {"h1", "h2", "h3", "h4"}
# Customer stories need the client's consent, like cases in documents.
# Whole words only: "pillowcase" is a product, not a case study.
CASE_PATH = re.compile(r"(?i)(?:^|[/_\-])(?:cases?|case-stud(?:y|ies)|clients?|customers?|partners?|testimonials?|reviews?|案例|客户)(?:$|[/_\-])")
MODULE_PATHS = [
    ("company", re.compile(r"(?i)about|company|profile|factory|history|team|certif|关于|公司|工厂")),
    ("product", re.compile(r"(?i)product|collection|catalog|shop|item|category|产品")),
    ("solution", re.compile(r"(?i)service|solution|oem|odm|custom|process|服务|定制")),
    ("faq", re.compile(r"(?i)faq|question|问答")),
]
EMPTY_SHELL_CHARS = 80
# Page-builder fragments, feeds, and archive listings repeat other pages' content.
NOISE_PATH = re.compile(r"(?i)elementskit|wp-json|/feed|/tag/|/author/|/page/\d|/wp-content/|\?|xmlrpc")
# Product and company photos for the material folder; icons and sprites are too small to matter.
PICTURE_EXTENSIONS = re.compile(r"(?i)\.(?:jpe?g|png|webp)$")
PICTURE_MIN_BYTES = 15_000
MAX_PICTURES_PER_PAGE = 12
MAX_PICTURES = 800


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.current: list[str] | None = None
        self.tag = ""
        self.blocks: list[tuple[str, str]] = []
        self.links: list[str] = []
        self.title = ""
        self.in_title = False
        # (index of the text block it belongs to, address, alt text) for each picture in the content.
        self.images: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip += 1
        if tag == "img" and not self.skip:
            values = dict(attrs)
            # Lazy-loading themes keep the real address in data-src.
            src = values.get("data-src") or values.get("data-lazy-src") or values.get("src") or ""
            if src and not src.startswith("data:"):
                # Inside an open paragraph the picture belongs to it; otherwise to the text before it.
                position = len(self.blocks) if self.current is not None else len(self.blocks) - 1
                self.images.append((position, src, normalize_space(values.get("alt") or values.get("title") or "")))
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag == "title":
            self.in_title = True
        if tag in BLOCK_TAGS and not self.skip:
            self.flush()
            self.current, self.tag = [], tag

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
        if tag in BLOCK_TAGS:
            self.flush()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif self.current is not None and not self.skip:
            self.current.append(data)

    def flush(self) -> None:
        if self.current is not None:
            text = normalize_space("".join(self.current))
            if text:
                self.blocks.append((self.tag, text))
        self.current = None


def sections(blocks: list[tuple[str, str]], owners: list[int] | None = None) -> list[dict[str, str]]:
    """One unit per heading section, so a page reads in the order it was written. owners, when
    given, receives the section number of every block."""
    units: list[dict[str, str]] = []
    current: list[str] = []
    for tag, text in blocks:
        if tag in HEADINGS and current:
            units.append({"locator": f"section {len(units) + 1}", "text": "\n".join(current)})
            current = []
        if not current or current[-1] != text:
            current.append(text)
        if owners is not None:
            owners.append(len(units) + 1)
    if current:
        units.append({"locator": f"section {len(units) + 1}", "text": "\n".join(current)})
    return units


def picture_host(host: str, hosts: set[str]) -> bool:
    """The site's own host or one of its subdomains, such as img.example.com."""
    return host in hosts or any(host.endswith("." + h.removeprefix("www.")) for h in hosts)


def save_pictures(url: str, images: list[tuple[int, str, str]], owners: list[int], hosts: set[str], assets: Path,
                  budget: dict[str, int], delay: float) -> list[dict[str, Any]]:
    """Download the page's own pictures once each, named by content, so the vault keeps one copy."""
    saved: list[dict[str, Any]] = []
    for position, src, alt in images:
        if budget["left"] <= 0 or len(saved) >= MAX_PICTURES_PER_PAGE:
            break
        address = urljoin(url, src)
        parsed = urlparse(address)
        if parsed.scheme not in {"http", "https"} or not picture_host((parsed.hostname or "").casefold(), hosts) \
                or not PICTURE_EXTENSIONS.search(parsed.path):
            continue
        try:
            data, kind = fetch(address)
        except (HTTPError, URLError, OSError, TimeoutError):
            continue
        if not kind.casefold().startswith("image/") or len(data) < PICTURE_MIN_BYTES:
            continue
        suffix = PICTURE_EXTENSIONS.search(parsed.path).group(0).casefold().replace(".jpeg", ".jpg")
        name = f"img-{hashlib.sha256(data).hexdigest()[:16]}{suffix}"
        if not (assets / name).is_file():
            assets.mkdir(parents=True, exist_ok=True)
            (assets / name).write_bytes(data)
        section = owners[min(max(position, 0), len(owners) - 1)] if owners else 1
        if all(p["id"] != name for p in saved):
            saved.append({"id": name, "alt": alt, "section": section, "src": address})
        budget["left"] -= 1
        time.sleep(delay / 2)
    return saved


def module_for(url: str) -> str:
    """Judge by the first path segment: product slugs often contain words like "factory"."""
    segments = [part for part in urlparse(url).path.split("/") if part]
    if not segments:
        return MODULES["company"]
    for module, pattern in MODULE_PATHS:
        if pattern.search(segments[0]):
            return MODULES[module]
    # /category/item is how shops lay out products.
    return MODULES["product"] if len(segments) >= 2 else MODULES["other"]


def allowed_hosts(config: dict[str, Any]) -> set[str]:
    hosts = set()
    for site in config.get("websites", []):
        host = (urlparse(site["url"]).hostname or "").casefold()
        hosts.update({host, host.removeprefix("www."), f"www.{host.removeprefix('www.')}"})
    return {host for host in hosts if host}


def fetch(url: str, timeout: int = 30) -> tuple[bytes, str]:
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
        kind = response.headers.get("Content-Type", "")
        return response.read(5 * 1024 * 1024), kind


def sitemap_urls(base: str, robots: RobotFileParser) -> list[str]:
    found, queue = [], list(robots.site_maps() or []) or [urljoin(base, "/sitemap.xml")]
    for _ in range(20):
        if not queue:
            break
        try:
            data, _ = fetch(queue.pop(0))
            root = ET.fromstring(data)
        except (HTTPError, URLError, OSError, ET.ParseError, TimeoutError):
            continue
        for loc in root.iter():
            if loc.tag.endswith("loc") and loc.text:
                (queue if loc.text.strip().endswith(".xml") else found).append(loc.text.strip())
    return found


def page_kind(url: str, text_length: int) -> str:
    if NOISE_PATH.search(url):
        return "noise"
    if text_length < EMPTY_SHELL_CHARS:
        return "needs_browser"
    return "case" if CASE_PATH.search(urlparse(url).path) else "page"


def record_page(store: Path, pages: dict[str, dict[str, Any]], url: str, title: str, units: list[dict[str, str]], raw: bytes) -> dict[str, Any]:
    page_id = f"web-{short_hash(url, 16)}"
    html_sha = hashlib.sha256(raw).hexdigest()
    previous = pages.get(page_id, {})
    length = sum(len(unit["text"]) for unit in units)
    page = {
        "page_id": page_id, "url": url, "title": normalize_space(title) or urlparse(url).path or url,
        "module": module_for(url), "kind": page_kind(url, length), "html_sha256": html_sha,
        # Unchanged pages keep their date, so a refresh shows what really changed.
        "fetched_at": previous.get("fetched_at") if previous.get("html_sha256") == html_sha else datetime.now().astimezone().isoformat(timespec="seconds"),
        "characters": length,
    }
    (store / "raw").mkdir(parents=True, exist_ok=True)
    (store / "raw" / f"{html_sha[:16]}.html").write_bytes(raw)
    write_json(store / "units" / f"{page_id}.json", {"item_id": page_id, "url": url, "units": units})
    pages[page_id] = page
    return page


def crawl(config: dict[str, Any], store: Path, pages: dict[str, dict[str, Any]], max_pages: int, delay: float,
          pictures: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    hosts = allowed_hosts(config)
    report: dict[str, Any] = {"fetched": 0, "blocked_by_robots": 0, "failed": [], "needs_browser": [], "pictures": 0}
    budget = {"left": MAX_PICTURES}
    for site in config.get("websites", []):
        base = site["url"]
        robots = RobotFileParser(urljoin(base, "/robots.txt"))
        try:
            robots.read()
        except (URLError, OSError):
            robots = None  # type: ignore[assignment]
        queue = [base] + (sitemap_urls(base, robots) if robots else [])
        seen: set[str] = set()
        while queue and report["fetched"] < max_pages:
            url = urldefrag(queue.pop(0))[0].rstrip("/") or base
            host = (urlparse(url).hostname or "").casefold()
            if url in seen or host not in hosts or SKIP_EXTENSIONS.search(urlparse(url).path) or NOISE_PATH.search(url):
                continue
            seen.add(url)
            if robots and not robots.can_fetch(USER_AGENT, url):
                report["blocked_by_robots"] += 1
                continue
            try:
                raw, kind = fetch(url)
            except (HTTPError, URLError, OSError, TimeoutError) as exc:
                report["failed"].append({"url": url, "error": str(exc)[:120]})
                continue
            if "html" not in kind.casefold():
                continue
            parser = PageParser()
            parser.feed(raw.decode("utf-8", errors="replace"))
            parser.flush()
            owners: list[int] = []
            page = record_page(store, pages, url, parser.title, sections(parser.blocks, owners), raw)
            report["fetched"] += 1
            # Customer stories wait for consent, so their pictures are not taken either.
            if pictures is not None and page["kind"] == "page":
                pictures[page["page_id"]] = save_pictures(url, parser.images, owners, hosts, store.parent / "assets", budget, delay)
                report["pictures"] += len(pictures[page["page_id"]])
            if page["kind"] == "needs_browser":
                report["needs_browser"].append(url)
            queue.extend(urljoin(url, link) for link in parser.links)
            time.sleep(delay)
    return report


def import_capture(path: Path, config: dict[str, Any], store: Path, pages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A browser capture: {"url": ..., "title": ..., "blocks": [{"tag": "h1", "text": ...}]}."""
    capture = load_json(path)
    host = (urlparse(capture["url"]).hostname or "").casefold()
    if host not in allowed_hosts(config):
        raise SystemExit(f"{capture['url']} 不在 kb-config.json 登记的官网域名里，不导入。")
    blocks = [(entry.get("tag", "p"), normalize_space(entry.get("text", ""))) for entry in capture.get("blocks", [])]
    raw = json.dumps(capture, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return record_page(store, pages, capture["url"], capture.get("title", ""), sections([b for b in blocks if b[1]]), raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the client's own website into the build.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", required=True, help="kb-config.json with a websites list")
    parser.add_argument("--max-pages", type=int, default=150)
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    parser.add_argument("--browser-capture", action="append", default=[], help="Saved browser capture JSON; repeat")
    parser.add_argument("--vault", help="Published project: start from the website snapshot kept there")
    parser.add_argument("--no-pictures", action="store_true", help="Read the text only, without the site's photos")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config))
    if not config.get("websites"):
        raise SystemExit('kb-config.json 里没有 "websites"，例如 [{"url": "https://www.example.com", "role": "primary"}]')
    if args.vault:
        restore(Path(args.out).resolve(), Path(args.vault).resolve())
    store = Path(args.out).resolve() / BUILD_DIRECTORY / "website"
    index = store / "pages.json"
    pages = {page["page_id"]: page for page in load_json(index)} if index.is_file() else {}
    if args.browser_capture:
        imported = [import_capture(Path(p), config, store, pages) for p in args.browser_capture]
        report = {"imported": len(imported)}
        previous_report = store / "fetch-report.json"
        if previous_report.is_file():
            previous = load_json(previous_report)
            crawl_report = previous.get("last_crawl", previous)
            if "fetched" in crawl_report:
                report["last_crawl"] = crawl_report
    else:
        picture_index = store / "pictures.json"
        pictures = load_json(picture_index) if picture_index.is_file() else {}
        report = crawl(config, store, pages, args.max_pages, args.delay, None if args.no_pictures else pictures)
        if not args.no_pictures:
            write_json(picture_index, pictures)
    usable = [p for p in pages.values() if p["kind"] in {"page", "case"}]
    snapshot = min((p["fetched_at"][:10] for p in usable), default=None)
    report.update(checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                  sites=[site["url"] for site in config["websites"]], usable_pages=len(usable))
    if not args.browser_capture and not report["fetched"] and usable:
        report["using_snapshot"] = snapshot  # Pages read earlier stay; a failed refresh removes nothing.
    write_json(store / "fetch-report.json", report)
    write_json(index, sorted(pages.values(), key=lambda page: page["url"]))
    counts: dict[str, int] = {}
    for page in pages.values():
        counts[page["kind"]] = counts.get(page["kind"], 0) + 1
    lines = ["# 官网页面", "", f"共 {len(pages)} 页：" + "，".join(f"{k} {v}" for k, v in counts.items()), "",
             "| 页面 | 模块 | 类型 | 字数 |", "| --- | --- | --- | --- |"]
    lines.extend(f"| [{p['title']}]({p['url']}) | {p['module']} | {p['kind']} | {p['characters']} |"
                 for p in sorted(pages.values(), key=lambda page: page["url"]))
    write_text(store / "官网页面.md", "\n".join(lines) + "\n")
    print(json.dumps({**{k: v for k, v in report.items() if k != "failed"}, "failed": len(report.get("failed", [])),
                      "pages": counts}, ensure_ascii=False))
    for failure in report.get("failed", [])[:5]:
        print(f"读取失败：{failure['url']}：{failure['error']}")
    if report.get("using_snapshot"):
        print(f"这次一页也没读到，沿用已保存的官网快照（最早 {report['using_snapshot']}）。")
    elif not usable:
        print("官网一页可用正文也没读到，详情见 04_知识库构建/website/fetch-report.json。"
              "检查网址和网络；网站拦截时用浏览器保存页面，再 --browser-capture 导入。")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
