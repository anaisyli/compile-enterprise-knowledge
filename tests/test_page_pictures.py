#!/usr/bin/env python3
"""Page pictures survive text read elsewhere (--text-dir), and the check counts them on its own."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BUILD = "04_知识库构建"


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, encoding="utf-8")


def make_pdf_package(package: Path, config: Path) -> None:
    """A package with one real two-page PDF, so pages get real page pictures."""
    import pymupdf  # type: ignore[import-not-found]

    relative = "A-原文保留/营销宣传/DS50 Specification.pdf"
    pdf_path = package / relative
    pdf_path.parent.mkdir(parents=True)
    (package / "审计记录").mkdir()
    (package / "C-知识点").mkdir()
    document = pymupdf.open()
    for number in (1, 2):
        page = document.new_page()
        page.insert_text((72, 72), f"DS50 wall mounted digital signage, page {number}.", fontsize=12)
        page.insert_text((72, 100), "Brightness 500 nits, resolution 3840 x 2160, tempered glass front.", fontsize=12)
        page.insert_text((72, 128), "Wall bracket included; operating temperature 0 to 40 degrees.", fontsize=12)
    document.save(str(pdf_path))
    document.close()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    asset = {"item_id": "p1", "display_name": pdf_path.name, "decision": "A", "resource_type": "marketing",
             "asset_kind": "document", "path": relative, "copy_status": "copied", "source_hash": digest,
             "usable_as_fact": True, "pending_client_review": False, "pending_units": []}
    (package / "审计记录/package-manifest.json").write_text(json.dumps({"assets": [asset]}), encoding="utf-8")
    (package / "审计记录/triage-results.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    (package / "C-知识点/knowledge-points.jsonl").write_text("", encoding="utf-8")
    config.write_text(json.dumps({"own_terms": ["DS50"], "sample_review": False, "project_id": "P-995"}), encoding="utf-8")


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
class PagePictureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.package, self.work, self.out = root / "02_初筛包", root / "screening-work", root / "run"
        self.config = root / "kb-config.json"
        make_pdf_package(self.package, self.config)
        # The text is read in another folder first, as a screening work folder would have it.
        read = run("extract_package_text.py", "--package", str(self.package), "--out", str(self.work))
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        self.text_dir = self.work / BUILD / "package-text"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def args(self) -> list[str]:
        return ["--package", str(self.package), "--out", str(self.out), "--config", str(self.config),
                "--text-dir", str(self.text_dir), "--extract-status", str(self.text_dir / "extract-status.json")]

    def verify(self) -> subprocess.CompletedProcess:
        # The check reads the build's own merged record, never the folder the text came from.
        return run("verify_kb.py", "--package", str(self.package), "--out", str(self.out), "--config", str(self.config),
                   "--text-dir", str(self.text_dir))

    def page(self) -> str:
        return next((self.out / "03_净化md").rglob("*DS50*.md")).read_text(encoding="utf-8")

    def test_text_read_elsewhere_still_brings_its_page_pictures(self) -> None:
        built = run("run_pipeline.py", *self.args(), "--images", "none")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.page().count("|360]]"), 2)
        merged = json.loads((self.out / BUILD / "extract-status.json").read_text(encoding="utf-8"))
        names = merged["items"]["p1"]["page_images"].values()
        self.assertTrue(all((self.out / BUILD / "assets" / n).is_file() for n in names))

    def test_the_check_finds_a_missing_page_picture(self) -> None:
        self.assertEqual(run("run_pipeline.py", *self.args(), "--images", "none").returncode, 0)
        merged = json.loads((self.out / BUILD / "extract-status.json").read_text(encoding="utf-8"))
        (self.out / BUILD / "assets" / merged["items"]["p1"]["page_images"]["1"]).unlink()
        checked = self.verify()
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("原页图文件不见了", checked.stdout + checked.stderr)

    def test_project_prefixed_pictures_remain_verifiable_without_rewriting_sources(self) -> None:
        self.assertEqual(run("run_pipeline.py", *self.args(), "--images", "none").returncode, 0)
        source = next((self.out / "03_净化md").rglob("*DS50*.md"))
        source.write_text(source.read_text(encoding="utf-8").replace(
            "![[03_正式知识/90_素材/", "![[客户项目/P-995/03_正式知识/90_素材/"), encoding="utf-8")
        frozen = source.read_bytes()
        checked = self.verify()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(source.read_bytes(), frozen)
        merged = json.loads((self.out / BUILD / "extract-status.json").read_text(encoding="utf-8"))
        (self.out / BUILD / "assets" / merged["items"]["p1"]["page_images"]["1"]).unlink()
        checked = self.verify()
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("原页图文件不见了", checked.stdout + checked.stderr)

    def test_the_check_finds_a_page_with_no_pictures_at_all(self) -> None:
        self.assertEqual(run("run_pipeline.py", *self.args(), "--images", "none").returncode, 0)
        source = next((self.out / "03_净化md").rglob("*DS50*.md"))
        source.write_text("\n".join(l for l in source.read_text(encoding="utf-8").splitlines() if "|360]]" not in l),
                          encoding="utf-8")
        checked = self.verify()
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("一张原页图都没有", checked.stdout + checked.stderr)


if __name__ == "__main__":
    unittest.main()
