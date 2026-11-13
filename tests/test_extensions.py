#!/usr/bin/env python3
"""MinerU, website reading, cell review, answers, vault building, and maintenance."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

from test_pipeline import build_package  # noqa: E402
import mineru_client  # noqa: E402
from mineru_ocr import page_units  # noqa: E402
from extract_blocks import document_blocks, ocr_additions  # noqa: E402


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.package, self.text, self.config = build_package(self.root)
        self.out = self.root / "run"
        self.build = self.out / "04_知识库构建"
        self.clean = self.out / "03_净化md"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def pipeline(self, *extra: str) -> subprocess.CompletedProcess:
        return run("run_pipeline.py", "--package", str(self.package), "--text-dir", str(self.text), "--out", str(self.out),
                   "--config", str(self.config), "--images", "none", *extra)

    def published(self) -> str:
        pages = list(self.clean.rglob("*.md")) + list((self.build / "views").rglob("*.md"))
        return "\n".join(p.read_text(encoding="utf-8") for p in pages)

    def edit_config(self, **changes) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update(changes)
        self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def clear_catalog_pending(self) -> None:
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"][0].update(pending_client_review=False, pending_units=[])
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


class MinerUTests(Base):
    def test_ocr_duplicate_format_is_accounted_but_new_model_cannot_vanish(self) -> None:
        self.clear_catalog_pending()
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        (ocr / "a1.json").write_text(json.dumps({"units": [
            {"locator": "page 1 ocr", "text": "\\- MACHINE WASHABLE\nNTX-777"}]}), encoding="utf-8")
        built = self.pipeline()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        blocks_path = self.build / "blocks.jsonl"
        blocks = [json.loads(line) for line in blocks_path.read_text(encoding="utf-8").splitlines()]
        # Simulate losing an actually new OCR model after a successful build.
        blocks = [block for block in blocks if block.get("locator") != "page 1 ocr"]
        blocks_path.write_text("".join(json.dumps(block) + "\n" for block in blocks), encoding="utf-8")
        checked = run("verify_kb.py", "--package", str(self.package), "--text-dir", str(self.text),
                      "--out", str(self.out), "--config", str(self.config))
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("source line lost", checked.stdout)

    def test_ai_ocr_markdown_bullets_are_not_new_source_content(self) -> None:
        units = [{"locator": "page 1", "text": "• MACHINE WASHABLE\n• REVERSIBLE DESIGN\nNTX S-018"},
                 {"locator": "page 1 ocr", "text": "\\- MACHINE WASHABLE - REVERSIBLE DESIGN\nNTX S-018\nNTX S-019",
                  "engine": "ai"}]
        result = ocr_additions(units)
        self.assertEqual(result[0], units[0])
        self.assertEqual(result[1]["text"], "NTX S-019")

    def test_full_page_ocr_replaces_native_columns_and_tables_but_not_checks(self) -> None:
        for engine in (None, "", "mineru", "reducto"):
            with self.subTest(engine=engine):
                reading = {"locator": "page 2 ocr", "engine": engine,
                           "text": "VALVE SERIES\n\n| Model | Size |\n| --- | --- |\n| V-100 | DN50 |"}
                untouched = {"locator": "page 3 col 1", "text": "Unrecognised page stays"}
                check = {"locator": "page 2 check", "text": "Image label", "engine": "ai", "checked": True}
                units = [{"locator": "page 2", "text": "VALVE SERIES"},
                         {"locator": "page 2 col 1", "text": "100 V- DN 50 wrong order"},
                         {"locator": "page 2 col 2", "text": "VALVE SERIES"},
                         {"locator": "page 2 table 1", "text": "| Model | Size |"},
                         check, untouched, reading]
                self.assertEqual(ocr_additions(units), [check, reading, untouched])
                self.assertEqual(units[-1], reading)
                blocks = document_blocks("doc", "item", units)
                self.assertEqual([b["locator"] for b in blocks], ["page 2 check", "page 2 ocr", "page 3 col 1"])
                self.assertEqual(blocks[1]["text"], reading["text"])
                self.assertTrue(blocks[1]["ocr"])
                self.assertEqual(blocks[1]["reader"], engine)
                self.assertEqual(blocks[1]["ai_read"], engine == "reducto")

    def test_empty_or_unpaged_ocr_does_not_replace_native_text(self) -> None:
        for reading in ({"locator": "page 1 ocr", "text": " \n", "engine": "reducto", "empty": True},
                        {"locator": "mineru ocr", "text": "Whole file, no page mapping"}):
            with self.subTest(locator=reading["locator"]):
                native = {"locator": "page 1 col 1", "text": "Native page stays"}
                blocks = document_blocks("doc", "item", [native, reading])
                self.assertIn(native["text"], [b["text"] for b in blocks])

    def test_platform_pages_keep_repeated_headers_and_footers(self) -> None:
        from guard_blocks import strip_repeated_lines
        for engine in (None, "", "mineru", "reducto"):
            with self.subTest(engine=engine):
                blocks = document_blocks("doc", "item", [
                    {"locator": f"page {page} ocr", "engine": engine,
                     "text": f"VALVE HEADER\nPage {page} specifications\nwww.example.com"}
                    for page in range(1, 4)])
                self.assertEqual(strip_repeated_lines(blocks), 0)
                self.assertTrue(all("www.example.com" in b["text"] for b in blocks))

    def test_image_footnotes_keep_style_labels(self) -> None:
        result = self.root / "result"
        result.mkdir()
        content = [
            {"type": "image", "image_caption": ["SHERPA JACKET"],
             "image_footnote": ["NTX S-018", "NTX S-019"], "page_idx": 15},
            {"type": "image", "img_caption": "NTX S-023", "page_idx": 17},
        ]
        (result / "x_content_list.json").write_text(json.dumps(content), encoding="utf-8")
        units = page_units(result)
        self.assertIn("NTX S-018", units[0]["text"])
        self.assertIn("NTX S-019", units[0]["text"])
        self.assertEqual(units[1]["text"], "NTX S-023")

    def test_only_limits_plan_and_approval_limits_run(self) -> None:
        self.pipeline()
        # Catalogs go to MinerU only after a person chose that for them on the sample.
        (self.build / "sample-decisions.json").write_text(json.dumps({"catalog_pdf": {"status": "已确认", "method": "mineru"}}),
                                                          encoding="utf-8")
        result = run("mineru_ocr.py", "plan", "--out", str(self.out), "--package", str(self.package),
                     "--config", str(self.config), "--only", "a1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads((self.build / "mineru-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["item_id"] for entry in plan], ["a1"])
        self.edit_config(mineru={"approved": True, "approved_by": "test", "approved_at": "2026-11-04",
                                 "approved_item_ids": []})
        result = run("mineru_ocr.py", "run", "--out", str(self.out), "--package", str(self.package),
                     "--config", str(self.config), "--only", "a1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("超出", result.stdout)

    def test_missing_adds_kb_files_mineru_has_not_read(self) -> None:
        self.pipeline()
        args = ("mineru_ocr.py", "plan", "--out", str(self.out), "--package", str(self.package), "--config", str(self.config))

        def planned(*extra: str) -> list[str]:
            result = run(*args, *extra)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return [e["item_id"] for e in json.loads((self.build / "mineru-plan.json").read_text(encoding="utf-8"))]

        self.assertNotIn("a1", planned())
        self.assertIn("a1", planned("--missing"))
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        # Read by the model only: MinerU has still not seen it.
        (ocr / "a1.json").write_text(json.dumps({"item_id": "a1", "source": "ai", "units": [
            {"locator": "page 1", "text": "x", "engine": "ai"}]}), encoding="utf-8")
        self.assertIn("a1", planned("--missing"))
        (ocr / "a1.json").write_text(json.dumps({"item_id": "a1", "source": "mineru", "units": []}), encoding="utf-8")
        self.assertNotIn("a1", planned("--missing"))
        from kb_status import gather
        reading = gather(self.out, None)["reading"]["PDF"]
        self.assertEqual((reading["mineru"], reading["text"]), (1, reading["files"] - 1))

    def test_content_list_becomes_separate_ocr_page_units(self) -> None:
        result = self.root / "result"
        result.mkdir()
        content = [{"type": "text", "text": "TAPESTRY SHIRT NTXT-003", "page_idx": 1},
                   {"type": "table", "table_body": "<table><tr><td>100% COTTON</td></tr></table>", "page_idx": 1}]
        (result / "x_content_list.json").write_text(json.dumps(content), encoding="utf-8")
        units = page_units(result)
        self.assertEqual(units[0]["locator"], "page 2 ocr")
        self.assertIn("100% COTTON", units[0]["text"])

    def test_full_page_ocr_is_published_without_native_text_and_caches_stay_intact(self) -> None:
        self.clear_catalog_pending()
        native = self.text / "a1.json"
        native_record = json.loads(native.read_text(encoding="utf-8"))
        native_record["units"][0]["text"] += "\nSHERPA BROKEN NATIVE ORDER"
        native_record["units"][1]["text"] = "SHERPA SECOND PAGE"
        native.write_text(json.dumps(native_record), encoding="utf-8")
        original_native = native.read_bytes()
        reducto = self.build / "reducto-units" / "a1.json"
        mineru = self.build / "mineru-units" / "a1.json"
        for path in (reducto, mineru):
            path.parent.mkdir(parents=True, exist_ok=True)
        reducto.write_text(json.dumps({"source": "reducto", "units": [
            {"locator": "page 1 ocr", "text": "SHERPA SERIES\nSHERPA REDUCTO NTX-778", "engine": "reducto"}]}),
            encoding="utf-8")
        original_reducto = reducto.read_bytes()
        for use_mineru in (False, True):
            with self.subTest(use_mineru=use_mineru):
                if use_mineru:
                    mineru.write_text(json.dumps({"source": "mineru", "units": [
                        {"locator": "page 1 ocr", "text": "SHERPA SERIES\nSHERPA HOODIE NTX-777"}]}), encoding="utf-8")
                    original_mineru = mineru.read_bytes()
                built = self.pipeline("--tiers", "T1,T2")
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
                self.assertEqual(page.count("SHERPA SERIES"), 1)
                self.assertNotIn("BROKEN NATIVE ORDER", page)
                self.assertIn("NTX-777" if use_mineru else "NTX-778", page)
                if use_mineru:
                    self.assertNotIn("NTX-778", page)
                    self.assertEqual(mineru.read_bytes(), original_mineru)
                self.assertIn("SHERPA SECOND PAGE", page)
                self.assertEqual(native.read_bytes(), original_native)
                self.assertEqual(reducto.read_bytes(), original_reducto)
                checked = run("verify_kb.py", "--package", str(self.package), "--text-dir", str(self.text),
                              "--out", str(self.out), "--config", str(self.config))
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_unknown_word_read_from_a_picture_is_published_with_a_mark(self) -> None:
        self.clear_catalog_pending()
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        (ocr / "a1.json").write_text(json.dumps({"units": [{"locator": "page 1 ocr", "text": "Megabrand licensed jacket"}]}), encoding="utf-8")
        self.pipeline("--tiers", "T1,T2")
        page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
        self.assertIn("Megabrand", page)
        self.assertIn("⚠ 含没核实的词：Megabrand", page)

    def test_picture_read_by_reducto_enters_the_knowledge_base(self) -> None:
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"].append({**manifest["assets"][0], "item_id": "p1", "display_name": "label.png",
                                   "asset_kind": "image", "path": "A/label.png", "source_hash": "p" * 64})
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (self.package / "A").mkdir(exist_ok=True)
        (self.package / "A/label.png").write_bytes(b"\x89PNG")
        self.pipeline()
        self.assertEqual(json.loads((self.build / "routes.json").read_text(encoding="utf-8"))["p1"]["reason"], "image_needs_review")
        (self.build / "reducto-units").mkdir(exist_ok=True)
        (self.build / "reducto-units/p1.json").write_text(json.dumps({"item_id": "p1", "source": "reducto", "units": [
            {"locator": "page 1 ocr", "text": "NTX S-555 LABEL", "engine": "reducto"}]}), encoding="utf-8")
        self.pipeline()
        self.assertEqual(json.loads((self.build / "routes.json").read_text(encoding="utf-8"))["p1"]["reason"], "image_read")

    def test_ai_read_size_drawing_image_is_filed_as_a_dimension_picture_and_unknown_model_goes_to_mat(self) -> None:
        manifest_path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assets"].append({**manifest["assets"][0], "item_id": "p1", "display_name": "unnamed.png",
                                   "asset_kind": "image", "path": "A/unnamed.png", "source_hash": "p" * 64})
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        (self.package / "A").mkdir(exist_ok=True)
        (self.package / "A/unnamed.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
        self.assertEqual(self.pipeline().returncode, 0)
        ai_read = self.build / "ai-read"
        ai_read.mkdir()
        (ai_read / "todo.jsonl").write_text(        json.dumps({"key": "p1|1", "item_id": "p1",
                                                        "page": 1, "image": "unused"}) + "\n", encoding="utf-8")
        (ai_read / "answer-001.jsonl").write_text(json.dumps({
            "key": "p1|1", "kind": "尺寸图", "drawing_model": "型号待确认", "drawing_name": "外形尺寸图", "text": ""
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        collected = run("ai_read.py", "collect", "--out", str(self.out))
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        unit = json.loads((self.build / "mineru-units/p1.json").read_text(encoding="utf-8"))["units"][0]
        self.assertEqual((unit["locator"], unit["text"]), ("page 1 drawing", "型号待确认 的外形尺寸图"))
        self.assertTrue(unit["dimension_drawing"])
        self.assertEqual(self.pipeline().returncode, 0)
        route = json.loads((self.build / "routes.json").read_text(encoding="utf-8"))["p1"]
        self.assertEqual((route["route"], route["reason"], route["picture_kind"]), ("media", "dimension_drawing", "尺寸图"))
        picture = next(iter(json.loads((self.build / "pictures.json").read_text(encoding="utf-8")).values()))
        self.assertIn("产品/型号待确认/尺寸图/型号待确认-尺寸图-", picture["file"])
        self.assertEqual(picture["use"], "待核对")
        ledger = next((self.build / "vault").rglob("01_源资料处理台账.md")).read_text(encoding="utf-8")
        self.assertIn("尺寸图型号待确认", ledger)

    def test_reducto_figures_publish_separately_on_their_pages_without_becoming_verified_text(self) -> None:
        self.clear_catalog_pending()
        reducto = self.build / "reducto-units" / "a1.json"
        mineru = self.build / "mineru-units" / "a1.json"
        for path in (reducto, mineru):
            path.parent.mkdir(parents=True, exist_ok=True)
        reducto.write_text(json.dumps({"source": "reducto", "units": [
            {"locator": "page 1 ocr", "text": "SHERPA REDUCTO TRANSCRIPT", "engine": "reducto"}],
            "figures": {"1": ["图示：夹克轮廓", "图示：夹克轮廓", "图示：袖口结构", "价格 USD9.20"],
                        "4": ["图示：口袋位置"], "6": ["图示：仅有图片说明的页面"]}}), encoding="utf-8")
        mineru.write_text(json.dumps({"source": "mineru", "units": [
            {"locator": "page 1 ocr", "text": "SHERPA MINERU TRANSCRIPT"}]}), encoding="utf-8")
        caches = {path: path.read_bytes() for path in (reducto, mineru)}
        blocks_path = self.build / "blocks.jsonl"
        for trusted in (False, True):
            with self.subTest(trusted=trusted):
                if trusted:
                    blocks = [json.loads(line) for line in blocks_path.read_text(encoding="utf-8").splitlines()]
                    keep = [b["block_id"] for b in blocks if b.get("figure_description")]
                    (self.build / "review-decisions.json").write_text(json.dumps({"keep_blocks": keep}), encoding="utf-8")
                built = self.pipeline("--tiers", "T1,T2")
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                page = next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8")
                self.assertEqual(page.count("图示：夹克轮廓"), 1)
                self.assertEqual(page.count("图示：袖口结构"), 1)
                self.assertNotIn("USD9.20", page)
                self.assertIn("SHERPA MINERU TRANSCRIPT", page)
                self.assertNotIn("SHERPA REDUCTO TRANSCRIPT", page)
                sections = {}
                for part in page.split("\n## ")[1:]:
                    heading, body = part.split("\n", 1)
                    sections[heading] = body
                self.assertIn("图示：夹克轮廓", sections["第 1 页"])
                self.assertIn('`figures["1"][0]`', sections["第 1 页"])
                self.assertIn('`figures["1"][2]`', sections["第 1 页"])
                self.assertIn("图示：口袋位置", sections["第 4 页"])
                self.assertIn("MACHINE WASHABLE", sections["第 4 页"])
                self.assertIn("图示：仅有图片说明的页面", sections["第 6 页"])
                for number in (1, 4, 6):
                    self.assertEqual(sections[f"第 {number} 页"].count("### ⚠ Reducto 图表描述（待核对）"), 1)
                    self.assertIn("不是已核实原文，也不是 OCR 转写", sections[f"第 {number} 页"])
                    self.assertIn("`reducto-units/a1.json`", sections[f"第 {number} 页"])
                for path, content in caches.items():
                    self.assertEqual(path.read_bytes(), content)
                blocks = [json.loads(line) for line in blocks_path.read_text(encoding="utf-8").splitlines()]
                figures = [b for b in blocks if b.get("figure_description") and b["status"] in {"kept", "flagged"}]
                self.assertEqual(len(figures), 4)
                self.assertTrue(all(not b["ocr"] and b["reader"] == "reducto" and
                                    any(f["type"] == "ai_read" for f in b["flags"]) for b in figures))
                public = (self.build / "exports" / "blocks-public.jsonl").read_text(encoding="utf-8")
                self.assertNotIn("图示：", public)
                checked = run("verify_kb.py", "--package", str(self.package), "--text-dir", str(self.text),
                              "--out", str(self.out), "--config", str(self.config))
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        blocks_path.write_text("".join(json.dumps(b) + "\n" for b in blocks if not b.get("figure_description")),
                               encoding="utf-8")
        checked = run("verify_kb.py", "--package", str(self.package), "--text-dir", str(self.text),
                      "--out", str(self.out), "--config", str(self.config))
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("source line lost", checked.stdout)

    def test_report_page_is_marked_not_dropped(self) -> None:
        self.clear_catalog_pending()
        ocr = self.build / "mineru-units"
        ocr.mkdir(parents=True)
        (ocr / "a1.json").write_text(json.dumps({"units": [{"locator": "page 1 ocr", "text": "Audit Report rating C"}]}), encoding="utf-8")
        self.pipeline("--tiers", "T1,T2")
        self.assertIn("检测或审核报告页", next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8"))

    def test_nothing_is_uploaded_without_an_approval_record(self) -> None:
        self.pipeline()
        result = run("mineru_ocr.py", "run", "--out", str(self.out), "--package", str(self.package), "--config", str(self.config))
        self.assertEqual(result.returncode, 2)
        self.assertIn("批准", result.stdout)
        self.assertTrue((self.build / "mineru-plan.md").is_file())

    def test_server_side_expiry_is_reported_as_expired(self) -> None:
        error = mineru_client.MinerUError("HTTP 401：user token expired", "api-request")
        self.assertEqual(mineru_client.classify_failure(error), "credential-missing-or-expired")

    def test_health_check_runs_end_to_end_against_a_fake_api(self) -> None:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("full.md", "MinerU API health check")

        class Fake:
            def submit(self, path, options):
                return "batch"

            def wait(self, batch, timeout):
                return {"full_zip_url": "https://example.invalid/r.zip"}

            def download(self, url):
                return data.getvalue()

        os.environ["MINERU_STATE_DIR"] = str(self.root / "state")
        self.assertTrue(mineru_client.health_check(force=True, api=Fake())["ok"])
        status = (self.root / "state" / "mineru-api-status.json").read_text(encoding="utf-8")
        self.assertNotIn("Bearer", status)


class Site(BaseHTTPRequestHandler):
    pages = {
        "/robots.txt": "User-agent: *\nDisallow: /private\n",
        "/": "<html><title>Home</title><body><nav>menu</nav><h1>Nordtex Garments</h1><p>We make sherpa jackets, fleece shirts and quilted vests for retail brands worldwide.</p>"
             "<a href='/about'>About</a><a href='/private/x'>x</a><a href='https://other.example/'>other</a><a href='/cases'>cases</a></body></html>",
        "/about": "<html><title>About</title><body><h1>About us</h1><p>Our factory produces fleece and sherpa garments every season, with in-house knitting and sewing lines.</p></body></html>",
        "/cases": "<html><title>Cases</title><body><h1>Client cases</h1><p>A retail client ordered our sherpa jackets for their winter collections across many stores.</p></body></html>",
        "/private/x": "<html><body><p>secret page that robots forbid us to read at all</p></body></html>",
    }

    def do_GET(self):  # noqa: N802
        body = self.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


class WebsiteTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.edit_config(websites=[{"url": f"http://127.0.0.1:{self.server.server_port}/", "role": "primary"}])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def crawl(self) -> list[dict]:
        result = run("fetch_website.py", "--out", str(self.out), "--config", str(self.config), "--delay", "0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads((self.build / "website" / "pages.json").read_text(encoding="utf-8"))

    def test_only_own_allowed_pages_are_read_and_cases_are_marked(self) -> None:
        from urllib.parse import urlparse

        pages = {urlparse(p["url"]).path.strip("/") or "home": p for p in self.crawl()}
        self.assertEqual(set(pages), {"home", "about", "cases"})
        self.assertEqual(pages["cases"]["kind"], "case")
        self.assertEqual(pages["about"]["module"], "10_公司概述")

    def test_website_pages_publish_as_official_and_cases_wait_for_the_client(self) -> None:
        self.crawl()
        self.assertEqual(self.pipeline("--tiers", "T1,T2").returncode, 0)
        text = self.published()
        self.assertIn("Our factory produces fleece", text)
        self.assertNotIn("menu", text)
        self.assertNotIn("Bigbuyer", text)
        self.assertIn("官网客户案例页", text)
        cus = (self.build / "vault/05_数据与审核/30_异常与待决定/10_待客户补充/01_待客户补充事项.md").read_text(encoding="utf-8")
        self.assertIn("Cases", cus)

    def test_unchanged_page_keeps_its_date_on_refresh(self) -> None:
        first = {p["url"]: p["fetched_at"] for p in self.crawl()}
        second = {p["url"]: p["fetched_at"] for p in self.crawl()}
        self.assertEqual(first, second)

    def test_browser_import_keeps_crawl_limitations_in_the_ledger(self) -> None:
        self.crawl()
        capture = self.root / "capture.json"
        capture.write_text(json.dumps({"url": f"http://127.0.0.1:{self.server.server_port}/",
                                      "title": "Home", "blocks": [{"tag": "p", "text": "Our factory makes sherpa garments and supplies retail brands worldwide."}]}), encoding="utf-8")
        args = ("--out", str(self.out), "--config", str(self.config), "--browser-capture", str(capture))
        for _ in range(2):
            result = run("fetch_website.py", *args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((self.build / "website/fetch-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["last_crawl"]["blocked_by_robots"], 1)
        self.assertEqual(self.pipeline().returncode, 0)
        mat = next((self.build / "vault").rglob("01_源资料处理台账.md")).read_text(encoding="utf-8")
        self.assertIn("网站自动抓取规则阻止读取 1 个网址", mat)


class CellReviewTests(Base):
    def test_judged_cells_publish_and_the_gate_holds_until_audited(self) -> None:
        self.pipeline()
        exported = json.loads(run("review_cells.py", "export", "--out", str(self.out)).stdout)
        self.assertGreater(exported["cells_to_judge"], 0)
        folder = self.build / "cell-review"
        cells = [json.loads(line) for path in folder.glob("batch-*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
        answers = []
        for cell in cells:
            if cell["text"] == "Sherpa jacket":
                answers.append({"cell_key": cell["cell_key"], "decision": "part", "keep_text": "Sherpa"})
            elif "价格" in cell["text"]:
                answers.append({"cell_key": cell["cell_key"], "decision": "drop"})
            else:
                answers.append({"cell_key": cell["cell_key"], "decision": "keep"})
        answers.append({"cell_key": cells[0]["cell_key"], "decision": "part", "keep_text": "not in the cell"})
        (folder / "answer-001.jsonl").write_text("\n".join(json.dumps(a, ensure_ascii=False) for a in answers), encoding="utf-8")
        collected = json.loads(run("review_cells.py", "collect", "--out", str(self.out)).stdout)
        self.assertEqual(collected["rejected"], 1)

        self.assertEqual(self.pipeline("--tiers", "T1,T2,T3").returncode, 0)
        self.assertIn('status: "待审"', next(self.clean.rglob("报价单*.md")).read_text(encoding="utf-8"))

        (folder / "t3-audit.json").write_text(json.dumps({"passed": True, "files_read": 20, "by": "test"}), encoding="utf-8")
        self.assertEqual(self.pipeline("--tiers", "T1,T2,T3").returncode, 0)
        text = self.published()
        self.assertIn('status: "有效"', next(self.clean.rglob("报价单*.md")).read_text(encoding="utf-8"))
        self.assertIn("| Sherpa |", text)
        self.assertNotIn("Sherpa jacket", text.split("## 表 1", 1)[-1].split("##")[0] if "## 表 1" in text else "")


class VaultTests(Base):
    def pipeline(self, *extra: str) -> subprocess.CompletedProcess:
        return super().pipeline("--tiers", "T1,T2,T3,T4", "--skip-t3-gate", *extra)

    def make_vault(self) -> Path:
        args = ["--vault-root", str(self.root / "vaults"), "--project-id", "P-999", "--name", "测试",
                "--website", "https://example.com", "--owner", "运营", "--source-root", str(self.clean)]
        self.assertEqual(run("kb_project.py", *args).returncode, 3)
        self.assertFalse((self.root / "vaults").exists())
        self.assertEqual(run("kb_project.py", *args, "--confirm").returncode, 0)
        return self.root / "vaults" / "P-999_测试知识库"

    def publish(self, vault: Path) -> subprocess.CompletedProcess:
        return run("publish_vault.py", "--out", str(self.out), "--vault", str(vault))

    def test_build_publish_index_and_search(self) -> None:
        self.assertEqual(self.pipeline().returncode, 0)
        vault = self.make_vault()
        result = self.publish(vault)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = json.loads(run("kb_index.py", "search", "--project", str(vault), "--query", "水洗面料").stdout)
        self.assertTrue(found)
        public = json.loads(run("kb_index.py", "search", "--project", str(vault), "--query", "水洗面料", "--visibility", "public").stdout)
        self.assertEqual(public, [])
        self.assertFalse(list(vault.rglob("问题单*")))
        self.assertIn("知识库总览", (vault / "01_工作台/00_知识库总览.md").read_text(encoding="utf-8"))
        self.assertTrue((vault / "02_源资料/源资料与可检索性台账.md").is_file())
        self.assertTrue((vault / "05_数据与审核/30_异常与待决定/30_源文与事实异常/01_源文与事实异常台账.md").is_file())

    def test_withdrawn_document_leaves_a_record_and_the_old_version_is_archived(self) -> None:
        self.pipeline()
        vault = self.make_vault()
        self.publish(vault)
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"] = [a for a in manifest["assets"] if a["item_id"] != "c1"]
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        self.assertEqual(self.publish(vault).returncode, 0)
        record = (vault / "02_源资料/10_净化原文/业务要点/业务要点-邮件摘录.md").read_text(encoding="utf-8")
        self.assertIn("已失效", record)
        self.assertTrue(list((vault / "05_数据与审核/60_版本归档/10_正式知识历史").rglob("业务要点-邮件摘录.md")))

    def test_hand_edited_file_stops_the_publish(self) -> None:
        self.pipeline()
        vault = self.make_vault()
        self.publish(vault)
        page = next(vault.rglob("*Catalog.md"))
        page.write_text(page.read_text(encoding="utf-8") + "\n人工改动\n", encoding="utf-8")
        self.pipeline()
        result = self.publish(vault)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("被人改过", result.stdout + result.stderr)

    def test_monthly_check_writes_a_dated_report(self) -> None:
        self.pipeline()
        vault = self.make_vault()
        self.publish(vault)
        self.assertEqual(run("kb_status.py", "--out", str(self.out), "--vault", str(vault), "--monthly").returncode, 0)
        self.assertTrue(list((vault / "05_数据与审核/40_月度与交接").glob("知识库体检_*.md")))


class AnswerTests(Base):
    def test_page_specific_question_describes_the_actual_source_conflict(self) -> None:
        self.edit_config(client_confirmation={"friendly": True, "questions": {
            "a1|page 1": "第一页的型号和产品说明不同，请确认正确对应关系。"}})
        self.assertEqual(self.pipeline().returncode, 0)
        checks = self.build / "page-check" / "results.json"
        checks.parent.mkdir(exist_ok=True)
        checks.write_text(json.dumps({"a1|page 1": {"item_id": "a1", "unit": "page", "page": 1,
                          "unclear": True, "structure": "表格", "pairing": "型号和说明不同"}}, ensure_ascii=False), encoding="utf-8")
        result = run("render_kb.py", "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with (self.build / "客户确认表.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        row = next(r for r in rows if r["在文件里的位置"] == "第 1 页")
        self.assertEqual(row["要确认的问题"], "第一页的型号和产品说明不同，请确认正确对应关系。")
        self.assertRegex(row["编号"], r"^问题\d+$")
        self.assertIn("原件内容的对应关系待确认", json.dumps(json.loads(
            (self.build / "governance.json").read_text(encoding="utf-8")), ensure_ascii=False))

    def test_friendly_confirmation_hides_internal_codes_and_reads_text_answers(self) -> None:
        self.edit_config(ask_client={"a1": "请确认产品归属"}, client_confirmation={"friendly": True,
                         "questions": {"a1": "这份资料应归属哪个产品型号？"},
                         "additional": [{"key": "warranty", "item_ids": ["a1"], "question": "保修期以哪个资料为准？"}]})
        self.assertEqual(self.pipeline().returncode, 0)
        csv_path = self.build / "客户确认表.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        row = next(r for r in rows if r["要确认的问题"] == "这份资料应归属哪个产品型号？")
        self.assertRegex(row["编号"], r"^问题\d+$")
        self.assertIn("在文件里的位置", row)
        self.assertTrue(any(r["要确认的问题"] == "保修期以哪个资料为准？" for r in rows))
        row["回答（保留 / 不用 / 文字）"] = "仅适用于指定型号，需要客户继续说明。"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        result = run("apply_answers.py", "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["notes_for_people"], 1)
        notes = (self.build / "client-notes.jsonl").read_text(encoding="utf-8")
        self.assertIn("仅适用于指定型号", notes)
        self.assertIn("a1", json.loads(self.config.read_text(encoding="utf-8"))["ask_client"])

    def test_client_yes_releases_the_held_page(self) -> None:
        self.pipeline()
        self.assertIn("🕒", next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8"))
        csv_path = self.build / "客户确认表.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            if "第 3 页" in row["要确认的问题"]:
                row["回答（保留 / 不用 / 文字）"] = "保留"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        result = run("apply_answers.py", "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(json.loads(result.stdout)["applied"], 1, result.stdout)
        self.pipeline()
        self.assertNotIn("🕒", next(self.clean.rglob("*Catalog.md")).read_text(encoding="utf-8"))

    def test_unscreened_folder_is_refused_with_a_pointer_to_screening(self) -> None:
        raw = self.root / "raw"
        raw.mkdir()
        result = run("run_pipeline.py", "--package", str(raw), "--text-dir", str(self.text), "--out", str(self.out))
        self.assertEqual(result.returncode, 2)
        self.assertIn("prepare-seo-knowledge-materials", result.stdout)


if __name__ == "__main__":
    unittest.main()
