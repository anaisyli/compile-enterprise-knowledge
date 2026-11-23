#!/usr/bin/env python3
"""Incremental rebuilds freeze confirmed extractions and PPT screenshots by source hash."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import kb_state  # noqa: E402
from kb_common import BUILD_DIRECTORY, write_json  # noqa: E402
from test_package_only import PackageOnlyTests  # noqa: E402


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


class RecoveredPageCacheTests(unittest.TestCase):
    def test_recovered_overlay_survives_save_restore_without_mutating_native_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, vault = root / 'run', root / 'vault'
            build = run / BUILD_DIRECTORY
            raw = {'items': {'ppt': {'source_hash': 'same', 'status': 'read_ok',
                                    'page_images_skipped': 'old timeout'}}}
            write_json(build / 'package-text/extract-status.json', raw)
            write_json(build / 'package-text/ppt.json', {'units': [{'locator': 'slide 1', 'text': 'Old reviewed words'}]})
            write_json(build / 'extract-status.json', {'items': {'ppt': {'source_hash': 'same',
                'page_images': {'1': 'img-recovered.png'}, 'page_unit': 'slide'}}})
            image = build / 'assets/img-recovered.png'
            image.parent.mkdir(parents=True)
            image.write_bytes(b'exact old screenshot')
            kb_state.save(run, vault)
            self.assertEqual(json.loads((build / 'package-text/extract-status.json').read_text(encoding='utf-8')), raw)
            frozen = json.loads((vault / kb_state.STATE / 'package-text/extract-status.json').read_text(encoding='utf-8'))
            self.assertEqual(frozen['items']['ppt']['page_images'], {'1': image.name})
            self.assertNotIn('page_images_skipped', frozen['items']['ppt'])
            fresh = root / 'fresh-run'
            kb_state.restore(fresh, vault)
            self.assertEqual((fresh / BUILD_DIRECTORY / 'assets' / image.name).read_bytes(), image.read_bytes())
            self.assertEqual(json.loads((fresh / BUILD_DIRECTORY / 'package-text/ppt.json').read_text(encoding='utf-8')),
                             {'units': [{'locator': 'slide 1', 'text': 'Old reviewed words'}]})

    def test_recovered_images_require_matching_source_and_present_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'frozen.json'
            original = {'items': {'ppt': {'source_hash': 'same', 'status': 'read_ok'}}}
            for source_hash, image_name in [('other', 'img.png'), ('same', 'missing.png'), ('same', '../outside.png')]:
                write_json(target, original)
                write_json(root / 'extract-status.json', {'items': {'ppt': {'source_hash': source_hash,
                    'page_images': {'1': image_name}}}})
                self.assertEqual(kb_state.freeze_recovered_page_images(root, target), 0)
                self.assertEqual(json.loads(target.read_text(encoding='utf-8')), original)


class IncrementalRebuildTests(PackageOnlyTests):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.folder.name)
        self.vault = self.root / "vaults/P-888_测试公司知识库"
        cfg = json.loads(self.config.read_text(encoding="utf-8"))
        cfg["project_id"] = "P-888"
        cfg["build_stage"] = "sources_only"
        self.config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_and_publish(self) -> None:
        res = run_script("run_pipeline.py", "--package", str(self.package), "--out", str(self.out),
                         "--config", str(self.config), "--images", "all", "--tiers", "T1,T2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        # Simulate a confirmed PPT slide screenshot stored in assets and recorded in extract-status.json.
        build = self.out / BUILD_DIRECTORY
        img = build / "assets/img-pptslide0001.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(b"\x89PNG\r\n\x1a\nfakeslide")
        st_path = build / "package-text/extract-status.json"
        st = json.loads(st_path.read_text(encoding="utf-8"))
        st["items"]["a1"]["page_images"] = {"slide 1": "img-pptslide0001.png"}
        st["items"]["a1"]["page_unit"] = "slide"
        st_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        proj = run_script("kb_project.py", "--vault-root", str(self.root / "vaults"), "--project-id", "P-888",
                          "--name", "测试公司", "--website", "https://example.com", "--owner", "运营",
                          "--source-root", str(self.package), "--confirm")
        self.assertEqual(proj.returncode, 0, proj.stdout + proj.stderr)
        pub = run_script("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault))
        self.assertEqual(pub.returncode, 0, pub.stdout + pub.stderr)

    def test_fresh_run_reuses_frozen_extraction_ppt_screenshots_and_remembers_flags(self) -> None:
        self.build_and_publish()
        self.assertTrue((self.vault / kb_state.STATE / "package-text/a1.json").is_file())
        self.assertTrue((self.vault / kb_state.STATE / "extracted-assets/img-pptslide0001.png").is_file())

        # Fresh run folder with --vault (no --images flag passed): inherits --images all and skips re-extraction.
        out2 = self.root / "run2"
        res2 = run_script("run_pipeline.py", "--out", str(out2), "--vault", str(self.vault))
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
        self.assertIn('"read_now": 0', res2.stdout)
        restored_img = out2 / BUILD_DIRECTORY / "assets/img-pptslide0001.png"
        self.assertTrue(restored_img.is_file())
        self.assertEqual(restored_img.read_bytes(), b"\x89PNG\r\n\x1a\nfakeslide")
        info = json.loads((out2 / BUILD_DIRECTORY / "run-info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["images"], "all")

        # --re-extract a1 forces re-extraction of a1, yet still preserves existing page_images for the same hash.
        res3 = run_script("run_pipeline.py", "--out", str(out2), "--vault", str(self.vault), "--re-extract", "a1")
        self.assertEqual(res3.returncode, 0, res3.stdout + res3.stderr)
        self.assertIn('"read_now": 1', res3.stdout)
        st3 = json.loads((out2 / BUILD_DIRECTORY / "package-text/extract-status.json").read_text(encoding="utf-8"))
        self.assertEqual(st3["items"]["a1"].get("page_images"), {"slide 1": "img-pptslide0001.png"})

    def test_from_guard_and_from_curate_skip_extraction(self) -> None:
        self.build_and_publish()
        out_guard = self.root / "run_guard"
        res_guard = run_script("run_pipeline.py", "--out", str(out_guard), "--vault", str(self.vault), "--from", "guard")
        self.assertEqual(res_guard.returncode, 0, res_guard.stdout + res_guard.stderr)
        self.assertNotIn("== extract_package_text.py", res_guard.stdout)
        self.assertNotIn("== extract_blocks.py", res_guard.stdout)
        self.assertIn("== guard_blocks.py", res_guard.stdout)

        out_curate = self.root / "run_curate"
        res_curate = run_script("run_pipeline.py", "--out", str(out_curate), "--vault", str(self.vault), "--from", "curate")
        self.assertEqual(res_curate.returncode, 0, res_curate.stdout + res_curate.stderr)
        self.assertNotIn("== extract_package_text.py", res_curate.stdout)
        self.assertNotIn("== guard_blocks.py", res_curate.stdout)
        self.assertIn("== verify_kb.py", res_curate.stdout)


if __name__ == "__main__":
    unittest.main()
