#!/usr/bin/env python3
"""Incremental rebuilds freeze confirmed extractions and PPT screenshots by source hash."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import kb_state  # noqa: E402
from kb_common import BUILD_DIRECTORY  # noqa: E402
from test_package_only import PackageOnlyTests  # noqa: E402


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


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
