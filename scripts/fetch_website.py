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
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

from kb_common import BUILD_DIRECTORY, MODULES, SKILL_VERSION, load_config, load_json, normalize_space, run_lock, short_hash, write_json, write_text
from kb_state import restore


USER_AGENT = f"compile-enterprise-knowledge/{SKILL_VERSION} (+client website reader)"
SKIP_EXTENSIONS = re.compile(r"(?i)\.(?:jpe?g|png|gif|webp|svg|ico|pdf|zip|rar|mp4|mov|mp3|css|js|xml|json|docx?|xlsx?|pptx?)$")
SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe", "select", "button"}
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "figcaption", "dt", "dd", "blockquote", "summary"}
# Standalone <strong>/<b> outside a paragraph or heading (common for FAQ questions in accordion divs).
INLINE_BLOCK_TAGS = {"strong", "b"}
VALUE_DIV_CLASS = re.compile(r"(?i)(?:^|[\s_-])(?:counter|count|number|stat|metric|tit|title)(?:$|[\s_-])")
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
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
        self.stack: list[tuple[str, bool]] = []
        # (index of the text block it belongs to, address, alt text) for each picture in the content.
        self.images: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        # Navigation is often a div rather than a semantic <nav>. Do not interpret its labels
        # as product/company prose. Track the opening element, so a nested div cannot end a skip.
        semantic_skip = (tag in SKIP_TAGS or values.get("role") in {"navigation", "search"}
                         or bool(re.search(r"(?i)(?:^|[\s_-])(?:breadcrumb|pagination|pagenav)(?:$|[\s_-])",
                                           values.get("class") or "")))
        if tag not in VOID_TAGS:
            self.stack.append((tag, semantic_skip))
        if semantic_skip:
            self.skip += 1
        if tag == "img" and not self.skip:
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
        elif tag in INLINE_BLOCK_TAGS and self.current is None and not self.skip:
            self.current, self.tag = [], tag
        elif (tag == "div" and self.current is None and not self.skip
              and VALUE_DIV_CLASS.search(values.get("class") or "")):
            # Number cards and standalone div titles were previously dropped because only
            # paragraph/heading tags opened a text block. Preserve their real text in place.
            self.current, self.tag = [], "div"

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID_TAGS:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    self.skip -= sum(int(skipped) for _, skipped in self.stack[index:])
                    del self.stack[index:]
                    break
        if tag == "title":
            self.in_title = False
        if tag in BLOCK_TAGS or (tag in INLINE_BLOCK_TAGS and self.tag == tag) or (tag == "div" and self.tag == "div"):
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


def read_robots(base: str) -> RobotFileParser | None:
    """Read robots.txt with the same User-Agent as the crawl.

    RobotFileParser.read() sends Python-urllib's default header and turns any 401/403 from a WAF
    into disallow_all = True, falsely blocking the whole site.
    """
    url = urljoin(base, "/robots.txt")
    try:
        raw, kind = fetch(url)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None
    if "html" in kind.casefold():
        return None
    robots = RobotFileParser(url)
    robots.parse(raw.decode("utf-8", errors="replace").splitlines())
    return robots


def normalize_crawl_url(raw: str, base: str) -> str:
    """Strip fragments while keeping a path's trailing slash (stripping it breaks some servers/TLS redirects)."""
    url = urldefrag(raw)[0] or base
    parsed = urlparse(url)
    return url if parsed.path else url + "/"


def crawl(config: dict[str, Any], store: Path, pages: dict[str, dict[str, Any]], max_pages: int, delay: float,
          pictures: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    hosts = allowed_hosts(config)
    report: dict[str, Any] = {"fetched": 0, "blocked_by_robots": 0, "failed": [], "needs_browser": [], "pictures": 0}
    budget = {"left": MAX_PICTURES}
    for site in config.get("websites", []):
        base = site["url"]
        robots = read_robots(base)
        queue = [base] + (sitemap_urls(base, robots) if robots else [])
        seen: set[str] = set()
        while queue and report["fetched"] < max_pages:
            url = normalize_crawl_url(queue.pop(0), base)
            key = url.rstrip("/")
            host = (urlparse(url).hostname or "").casefold()
            if key in seen or host not in hosts or SKIP_EXTENSIONS.search(urlparse(url).path) or NOISE_PATH.search(url):
                continue
            seen.add(key)
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


def canonical_snapshot_url(url: str) -> str:
    """An identity key only: changing slash spelling must not change an existing source id."""
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError(f"快照里不是可接受的官网网址：{url}")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/") or "/",
                       parsed.query, ""))


def is_listing_page(title: str, raw: bytes | None = None) -> bool:
    """Explicit archive titles/body classes identify listings even below the site's first level."""
    if re.search(r"(?i)\barchives?\s*(?:[-|—]|$)", title):
        return True
    if raw:
        match = re.search(r"(?is)<body\b[^>]*\bclass\s*=\s*['\"]([^'\"]*)", raw.decode("utf-8", errors="replace"))
        if match and re.search(r"(?i)(?:^|\s)(?:archive|category|tag|post-type-archive)(?:\s|$|-)", match[1]):
            return True
    return False


class SnapshotReader:
    """Read a saved website folder or run-record ZIP without extracting arbitrary archive paths."""
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.archive: zipfile.ZipFile | None = None
        self.prefix = ""
        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self.archive = zipfile.ZipFile(self.path)
            choices = [entry.filename for entry in self.archive.infolist()
                       if entry.filename == "pages.json" or entry.filename.endswith("/website/pages.json")]
            if len(choices) != 1:
                self.archive.close()
                raise ValueError("ZIP 中必须恰好有一份 website/pages.json，不能猜选快照。")
            self.prefix = choices[0][:-len("pages.json")]
        elif not self.path.is_dir() or not (self.path / "pages.json").is_file():
            raise ValueError("--import-snapshot 要给含 pages.json 的官网目录，或包含它的运行记录 ZIP。")

    def close(self) -> None:
        if self.archive is not None:
            self.archive.close()

    def read(self, relative: str, required: bool = True) -> bytes | None:
        # Names below are constructed from checked ids/hashes, never from archive member paths.
        if any(part in {"..", ""} for part in relative.split("/")) or "\\" in relative or relative.startswith("/"):
            raise ValueError("不接受快照路径穿越。")
        if self.archive is not None:
            name = self.prefix + relative
            matches = [entry for entry in self.archive.infolist() if entry.filename == name]
            if len(matches) > 1:
                raise ValueError(f"ZIP 里有重名成员：{relative}")
            if not matches:
                if required:
                    raise ValueError(f"快照缺少 {relative}")
                return None
            if matches[0].file_size > 20 * 1024 * 1024:
                raise ValueError(f"单个官网快照成员超过 20MB：{relative}")
            return self.archive.read(matches[0])
        item = (self.path / relative).resolve()
        if not item.is_relative_to(self.path):
            raise ValueError("快照成员指向目录外部。")
        if not item.is_file():
            if required:
                raise ValueError(f"快照缺少 {relative}")
            return None
        return item.read_bytes()


def deduplicate_shared_sections(store: Path, pages: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep an exact shared section once, favouring company/home pages over product detail pages.

    This removes repeated template sections without deleting every copy of legitimate company
    claims. No text is rewritten, and single-page content is never dropped by this rule.
    """
    records: dict[str, dict[str, Any]] = {}
    occurrences: dict[str, set[str]] = {}
    for page_id, page in pages.items():
        target = store / "units" / f"{page_id}.json"
        if not target.is_file() or page.get("is_listing"):
            continue
        record = load_json(target)
        records[page_id] = record
        for unit in record.get("units", []):
            text = unit.get("text", "")
            if text:
                occurrences.setdefault(text, set()).add(page_id)

    def rank(page_id: str) -> tuple[int, int, str]:
        page = pages[page_id]
        parts = [p for p in urlparse(page["url"]).path.split("/") if p]
        return (0 if not parts else 1 if page.get("module") == MODULES["company"] else 2,
                len(parts), page["url"])

    owners = {text: min(ids, key=rank) for text, ids in occurrences.items() if len(ids) >= 3}
    removed = []
    for page_id, record in records.items():
        kept = []
        for unit in record.get("units", []):
            text = unit.get("text", "")
            owner = owners.get(text)
            if owner and owner != page_id:
                removed.append({"page_id": page_id, "locator": unit.get("locator"),
                                "retained_on": owner, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                "characters": len(text)})
            else:
                kept.append(unit)
        if kept != record.get("units", []):
            write_json(store / "units" / f"{page_id}.json", {**record, "units": kept})
            pages[page_id]["characters"] = sum(len(u.get("text", "")) for u in kept)
    return removed


def import_snapshot(path: Path, config: dict[str, Any], store: Path,
                    pages: dict[str, dict[str, Any]], reparse: bool = False) -> dict[str, Any]:
    """Validate and merge already supplied local snapshots. This function never calls fetch()."""
    reader = SnapshotReader(path)
    try:
        source_pages = json.loads(reader.read("pages.json").decode("utf-8-sig"))
        if not isinstance(source_pages, list):
            raise ValueError("快照 pages.json 必须是页面数组。")
        previous_report = reader.read("fetch-report.json", required=False)
        previous_report = json.loads(previous_report.decode("utf-8-sig")) if previous_report else None
        known_urls = {canonical_snapshot_url(page["url"]): page for page in pages.values()}
        hosts = allowed_hosts(config)
        pending = []
        seen: set[str] = set()
        seen_ids: set[str] = set()
        # Validate the complete supplied snapshot before writing any imported page.
        for source in source_pages:
            page_id, digest, url = source.get("page_id", ""), source.get("html_sha256", ""), source.get("url", "")
            if not re.fullmatch(r"web-[a-f0-9]{16}", page_id) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("快照的页面编号或 HTML SHA-256 格式无效。")
            if page_id in seen_ids:
                raise ValueError(f"快照里页面编号重复：{page_id}")
            seen_ids.add(page_id)
            key = canonical_snapshot_url(url)
            if (urlparse(url).hostname or "").casefold() not in hosts:
                raise ValueError(f"快照网址不在登记的官网域名内：{url}")
            if key in seen:
                raise ValueError(f"快照里同一规范化网址出现两次：{url}")
            seen.add(key)
            if not source.get("fetched_at"):
                raise ValueError(f"快照缺少原始读取时间：{page_id}")
            record = json.loads(reader.read(f"units/{page_id}.json").decode("utf-8-sig"))
            if record.get("item_id") != page_id or canonical_snapshot_url(record.get("url", "")) != key:
                raise ValueError(f"正文与快照页面身份不对应：{page_id}")
            raw = reader.read(f"raw/{digest[:16]}.html", required=reparse)
            if raw is not None and hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError(f"原始 HTML 哈希不一致，不导入：{page_id}")
            units = record.get("units", [])
            title = source.get("title", "")
            if reparse:
                parser = PageParser()
                parser.feed(raw.decode("utf-8", errors="replace"))
                parser.flush()
                units = sections(parser.blocks)
                title = parser.title or title
            if not isinstance(units, list) or any(not isinstance(u, dict) or not isinstance(u.get("text"), str) for u in units):
                raise ValueError(f"快照正文格式无效：{page_id}")
            known = known_urls.get(key)
            if not known and page_id in pages and canonical_snapshot_url(pages[page_id]["url"]) != key:
                raise ValueError(f"快照页面编号与现有其他网址冲突：{page_id}")
            if known and source["fetched_at"] < known.get("fetched_at", ""):
                continue
            target_id = known["page_id"] if known else page_id
            target_url = known["url"] if known else url
            page = {**source, "page_id": target_id, "url": target_url, "title": normalize_space(title),
                    "characters": sum(len(u["text"]) for u in units), "module": module_for(target_url),
                    "is_listing": is_listing_page(title, raw)}
            page["kind"] = "listing" if page["is_listing"] else page_kind(target_url, page["characters"])
            pending.append((page, units, raw))
        for page, units, raw in pending:
            write_json(store / "units" / f"{page['page_id']}.json",
                       {"item_id": page["page_id"], "url": page["url"], "units": units})
            if raw is not None:
                target = store / "raw" / f"{page['html_sha256'][:16]}.html"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            pages[page["page_id"]] = page
        removed = deduplicate_shared_sections(store, pages) if reparse else []
        write_json(store / "parse-report.json", {"offline": True, "reparsed": reparse,
                   "imported_pages": len(pending), "shared_sections_removed": removed})
        return {"offline": True, "imported": len(pending), "reparsed": len(pending) if reparse else 0,
                "failed": [], "needs_browser": [], "pictures": 0,
                "shared_sections_removed": len(removed),
                **({"last_crawl": previous_report} if previous_report else {})}
    finally:
        reader.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the client's own website into the build.")
    parser.add_argument("--out", required=True, help="Run directory")
    parser.add_argument("--config", required=True, help="kb-config.json with a websites list")
    parser.add_argument("--max-pages", type=int, default=150)
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    parser.add_argument("--browser-capture", action="append", default=[], help="Saved browser capture JSON; repeat")
    parser.add_argument("--import-snapshot", help="Offline: website folder or run-record ZIP; never crawl or download")
    parser.add_argument("--reparse", action="store_true", help="With --import-snapshot: reparse its hash-checked original HTML")
    parser.add_argument("--vault", help="Published project: start from the website snapshot kept there")
    parser.add_argument("--no-pictures", action="store_true", help="Read the text only, without the site's photos")
    args = parser.parse_args()
    if args.import_snapshot and args.browser_capture:
        parser.error("--import-snapshot 和 --browser-capture 不能同时使用")
    if args.reparse and not args.import_snapshot:
        parser.error("--reparse 必须搭配 --import-snapshot")
    return args


def main() -> int:
    args = parse_args()
    if not run_lock(Path(args.out).resolve(), "fetch_website.py"):
        return 3
    config = load_config(Path(args.config))
    if not config.get("websites"):
        raise SystemExit('kb-config.json 里没有 "websites"，例如 [{"url": "https://www.example.com", "role": "primary"}]')
    if args.vault:
        restore(Path(args.out).resolve(), Path(args.vault).resolve())
    store = Path(args.out).resolve() / BUILD_DIRECTORY / "website"
    index = store / "pages.json"
    pages = {page["page_id"]: page for page in load_json(index)} if index.is_file() else {}
    if args.import_snapshot:
        try:
            report = import_snapshot(Path(args.import_snapshot), config, store, pages, args.reparse)
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            print(f"官网离线快照未导入：{exc}")
            return 2
    elif args.browser_capture:
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
    if not args.browser_capture and not args.import_snapshot and not report["fetched"] and usable:
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
