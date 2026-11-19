"""Offline website evidence remains attributable, and importing it never accesses the network."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_website as website
from kb_common import BUILD_DIRECTORY, short_hash
from kb_state import WEBSITE, merge_pages, restore, save


class OfflineSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "source"
        self.store = self.root / "run" / BUILD_DIRECTORY / "website"
        self.config = {"websites": [{"url": "https://www.example.com/"}]}

    def snapshot(self, records: list[tuple[str, str]]) -> list[dict]:
        pages = []
        for url, html in records:
            data = html.encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            page_id = f"web-{short_hash(url, 16)}"
            page = {"page_id": page_id, "url": url, "title": "Original title", "html_sha256": digest,
                    "fetched_at": "2026-10-01T15:22:03+08:00", "kind": "page", "characters": 90}
            for relative, content in ((f"raw/{digest[:16]}.html", data),
                                      (f"units/{page_id}.json", json.dumps({"item_id": page_id, "url": url,
                                       "units": [{"locator": "section 1", "text": "Old extracted text."}]}).encode())):
                target = self.source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            pages.append(page)
        (self.source / "pages.json").write_text(json.dumps(pages), encoding="utf-8")
        (self.source / "fetch-report.json").write_text(json.dumps({"fetched": len(pages), "failed": []}), encoding="utf-8")
        return pages

    def archive(self) -> Path:
        target = self.root / "snapshot.zip"
        with zipfile.ZipFile(target, "w") as archive:
            for path in self.source.rglob("*"):
                if path.is_file():
                    archive.write(path, f"run/{BUILD_DIRECTORY}/website/{path.relative_to(self.source).as_posix()}")
        return target

    def test_number_divs_stay_paired_with_their_labels_and_navigation_is_ignored(self) -> None:
        parser = website.PageParser()
        parser.feed('<h1>Our factory</h1><div class="stat-number"><span>24000</span>+</div><p>㎡ manufacturing area</p>'
                    '<div class="card-tit">12.7</div><p>tons per day</p>'
                    '<div role="navigation"><div><p>MENU</p></div><p>MORE MENU</p></div>'
                    '<p>After navigation.</p><div class="counter"><span>80+</span></div><p>countries</p>')
        parser.flush()
        text = "\n".join(unit["text"] for unit in website.sections(parser.blocks))
        self.assertIn("24000+\n㎡ manufacturing area\n12.7\ntons per day", text)
        self.assertIn("After navigation.\n80+\ncountries", text)
        self.assertNotIn("MENU", text)

    def test_zip_import_preserves_published_identity_hash_and_source_date_without_fetching(self) -> None:
        original = self.snapshot([("https://www.example.com", '<title>Factory</title><h1>Factory</h1>'
                                  '<div class="metric-number">24000+</div><p>square metres of manufacturing area.</p>'
                                  '<p>We manufacture several product families and provide technical documentation.</p>')])[0]
        old_id = f"web-{short_hash('https://www.example.com/', 16)}"
        known = {old_id: {**original, "page_id": old_id, "url": "https://www.example.com/",
                         "fetched_at": "2026-09-01T15:00:00+08:00"}}
        with patch.object(website, "fetch", side_effect=AssertionError("network must not be used")):
            report = website.import_snapshot(self.archive(), self.config, self.store, known, reparse=True)
        self.assertEqual(report["imported"], 1)
        self.assertEqual(set(known), {old_id})
        self.assertEqual(known[old_id]["fetched_at"], original["fetched_at"])
        self.assertEqual(known[old_id]["html_sha256"], original["html_sha256"])
        record = json.loads((self.store / "units" / f"{old_id}.json").read_text())
        self.assertEqual(record["url"], "https://www.example.com/")
        self.assertIn("24000+", record["units"][0]["text"])
        self.assertEqual(hashlib.sha256((self.store / "raw" / f"{original['html_sha256'][:16]}.html").read_bytes()).hexdigest(),
                         original["html_sha256"])

    def test_archive_titles_or_body_classes_mark_nested_listings(self) -> None:
        self.snapshot([("https://www.example.com/news/company-news", '<title>Company News Archives - Example</title>'
                        '<h1>Company News</h1><p>Lists of published stories and links to their articles.</p>'),
                       ("https://www.example.com/product/ball-valves", '<title>Ball Valves</title><body class="archive tax-product">'
                        '<h1>Ball Valves</h1><p>Lists of products and links to each specification.</p></body>')])
        pages = {}
        website.import_snapshot(self.source, self.config, self.store, pages, reparse=True)
        self.assertTrue(all(p["is_listing"] and p["kind"] == "listing" for p in pages.values()))
        self.assertFalse(website.is_listing_page("How to archive inspection records - Example"))

    def test_save_restore_index_does_not_resurrect_stale_slash_alias(self) -> None:
        self.root.mkdir(exist_ok=True)
        source, target = self.root / "source-pages.json", self.root / "vault-pages.json"
        old = {"page_id": "web-old", "url": "https://www.example.com", "fetched_at": "2026-11-13"}
        current = {"page_id": "web-current", "url": "https://www.example.com/", "fetched_at": "2026-11-19"}
        source.write_text(json.dumps([current]), encoding="utf-8")
        target.write_text(json.dumps([old, current]), encoding="utf-8")
        self.assertTrue(merge_pages(source, target))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), [current])
        self.assertFalse(merge_pages(source, target))

    def test_online_refresh_preserves_published_identity_across_trailing_slash(self) -> None:
        pages = {}
        original = website.record_page(self.store, pages, "https://www.example.com/product/a", "Old",
                                       [{"locator": "section 1", "text": "Old evidence."}], b"old")
        refreshed = website.record_page(self.store, pages, "https://www.example.com/product/a/", "New",
                                        [{"locator": "section 1", "text": "New evidence."}], b"new")
        self.assertEqual(set(pages), {original["page_id"]})
        self.assertEqual(refreshed["url"], original["url"])
        record = json.loads((self.store / "units" / f"{original['page_id']}.json").read_text())
        self.assertEqual(record["units"][0]["text"], "New evidence.")

    def test_old_slash_duplicates_merge_latest_evidence_into_original_identity(self) -> None:
        pages = {}
        first = website.record_page(self.store, pages, "https://www.example.com/product/a", "Old",
                                    [{"locator": "section 1", "text": "Old evidence."}], b"old")
        aliases = {}
        latest = website.record_page(self.store, aliases, "https://www.example.com/product/a/", "New",
                                     [{"locator": "section 1", "text": "New evidence."}], b"new")
        first["fetched_at"], latest["fetched_at"] = "2026-11-13", "2026-11-19"
        pages.update(aliases)
        self.assertEqual(website.merge_duplicate_page_urls(self.store, pages), 1)
        self.assertEqual(set(pages), {first["page_id"]})
        self.assertEqual(pages[first["page_id"]]["html_sha256"], latest["html_sha256"])
        self.assertEqual(pages[first["page_id"]]["url"], first["url"])
        record = json.loads((self.store / "units" / f"{first['page_id']}.json").read_text())
        self.assertEqual(record["item_id"], first["page_id"])
        self.assertEqual(record["units"][0]["text"], "New evidence.")
        self.assertTrue((self.store / "units" / f"{latest['page_id']}.json").is_file())

    def test_invalid_hash_refuses_the_entire_import_before_writing_pages(self) -> None:
        pages = self.snapshot([("https://www.example.com/first", '<h1>First page</h1><p>A useful description.</p>'),
                               ("https://www.example.com/second", '<h1>Second page</h1><p>Another description.</p>')])
        (self.source / "raw" / f"{pages[1]['html_sha256'][:16]}.html").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "哈希不一致"):
            website.import_snapshot(self.source, self.config, self.store, {}, reparse=True)
        self.assertFalse(self.store.exists())

    def test_unregistered_hosts_and_missing_raw_are_not_silently_accepted(self) -> None:
        pages = self.snapshot([("https://other.example.net/", '<p>Not an authorized site.</p>')])
        with self.assertRaisesRegex(ValueError, "域名"):
            website.import_snapshot(self.source, self.config, self.store, {}, reparse=True)
        self.config = {"websites": [{"url": "https://other.example.net/"}]}
        (self.source / "raw" / f"{pages[0]['html_sha256'][:16]}.html").unlink()
        with self.assertRaisesRegex(ValueError, "缺少"):
            website.import_snapshot(self.source, self.config, self.store, {}, reparse=True)

    def test_import_cannot_overwrite_an_existing_different_url_by_reusing_its_id(self) -> None:
        page = self.snapshot([("https://www.example.com/new", '<h1>New</h1><p>New content.</p>')])[0]
        existing = {page["page_id"]: {**page, "url": "https://www.example.com/unrelated"}}
        with self.assertRaisesRegex(ValueError, "编号.*冲突"):
            website.import_snapshot(self.source, self.config, self.store, existing, reparse=True)
        self.assertFalse(self.store.exists())

    def test_exact_repeated_sections_keep_one_attributable_copy_and_unique_prose(self) -> None:
        shared = "We provide documented manufacturing and after-sales support for all product families."
        pages = {}
        for name, url in (("a", "https://www.example.com/product/a"), ("b", "https://www.example.com/product/b"),
                          ("home", "https://www.example.com/")):
            page_id = f"web-{short_hash(url, 16)}"
            pages[page_id] = {"url": url, "module": website.module_for(url)}
            record = {"item_id": page_id, "url": url, "units": [{"locator": "section 1", "text": name},
                      {"locator": "section 2", "text": shared}]}
            target = self.store / "units" / f"{page_id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(record), encoding="utf-8")
        removed = website.deduplicate_shared_sections(self.store, pages)
        self.assertEqual(len(removed), 2)
        all_units = [json.loads(p.read_text())["units"] for p in (self.store / "units").glob("*.json")]
        self.assertEqual(sum(u["text"] == shared for units in all_units for u in units), 1)
        self.assertEqual({u["text"] for units in all_units for u in units if u["text"] != shared}, {"a", "b", "home"})

    def test_original_html_and_offline_parse_record_survive_save_restore(self) -> None:
        self.snapshot([("https://www.example.com/", '<h1>Factory</h1><p>Manufacturing documentation.</p>')])
        website.import_snapshot(self.source, self.config, self.store, {}, reparse=True)
        vault = self.root / "vault"
        save(self.root / "run", vault)
        raw = next((vault / WEBSITE / "raw").glob("*.html"))
        self.assertIn(b"Manufacturing", raw.read_bytes())
        self.assertTrue((vault / WEBSITE / "parse-report.json").is_file())
        fresh = self.root / "fresh-run"
        restore(fresh, vault)
        self.assertEqual((fresh / BUILD_DIRECTORY / "website" / "raw" / raw.name).read_bytes(), raw.read_bytes())

    def test_animated_counter_placeholders_read_target_attribute(self) -> None:
        html = (
            "<html><body>"
            '<h3><span class="odometer" data-count="18">00</span>+</h3><p>Years</p>'
            '<h3><span class="odometer" data-count="98">00</span>%</h3><p>Retention</p>'
            '<p>Clients: <span class="purecounter" data-purecounter-end="232"></span></p>'
            '<div class="elementor-counter-number" data-to-value="1,200">0</div>'
            '<button data-count="3">Comments</button>'
            "</body></html>"
        )
        parser = website.PageParser()
        parser.feed(html)
        parser.flush()
        texts = [t for _, t in parser.blocks]
        self.assertIn("18+", texts)
        self.assertIn("98%", texts)
        self.assertIn("Clients: 232", texts)
        self.assertIn("1,200", texts)
        self.assertNotIn("00+", texts)
        self.assertNotIn("00%", texts)


if __name__ == "__main__":
    unittest.main()
