#!/usr/bin/env python3
"""Regression tests for legacy page-check preservation/migration, apply_answers --dry-run & deny cleanup, and fast SMB state comparison."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kb_state  # noqa: E402
import page_check  # noqa: E402
from kb_common import BUILD_DIRECTORY, sha256_file  # noqa: E402


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / name), *args],
                          capture_output=True, text=True, encoding="utf-8")


class LegacyPageCheckMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / "pkg"
        self.out = self.root / "run"
        self.build = self.out / BUILD_DIRECTORY
        (self.pkg / "审计记录").mkdir(parents=True)
        (self.pkg / "A").mkdir(parents=True)
        (self.build / "package-text").mkdir(parents=True)
        (self.build / "mineru-units").mkdir(parents=True)
        (self.build / "page-check").mkdir(parents=True)

        import pymupdf
        pdf = self.pkg / "A" / "catalog.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Model X100 specification")
        doc.save(str(pdf))
        doc.close()

        self.source_hash = sha256_file(pdf)
        (self.pkg / "审计记录/package-manifest.json").write_text(json.dumps({
            "assets": [{
                "item_id": "a1", "display_name": "catalog.pdf", "decision": "A",
                "resource_type": "product", "asset_kind": "document",
                "source_hash": self.source_hash, "path": "A/catalog.pdf", "source_relpath": "catalog.pdf",
                "copy_status": "copied", "usable_as_fact": True, "pending_client_review": False,
            }]
        }, ensure_ascii=False), encoding="utf-8")
        (self.build / "routes.json").write_text(json.dumps({
            "a1": {
                "route": "kb", "tier": "T2", "display_name": "catalog.pdf",
                "package": str(self.pkg), "path": "A/catalog.pdf", "source_hash": self.source_hash,
            }
        }, ensure_ascii=False), encoding="utf-8")
        (self.build / "docs.jsonl").write_text(json.dumps({
            "doc_id": "D1", "item_id": "a1", "title": "catalog", "pages": 1, "tier": "T2", "folder": "产品画册",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.build / "package-text/a1.json").write_text(json.dumps({
            "item_id": "a1", "units": [{"locator": "page 1", "text": "Model X100 specification"}],
        }, ensure_ascii=False), encoding="utf-8")
        (self.build / "mineru-units/a1.json").write_text(json.dumps({
            "item_id": "a1", "source": "ai", "units": [
                {"locator": "page 1 check", "text": "AI checked note", "engine": "ai", "checked": True}
            ],
        }, ensure_ascii=False), encoding="utf-8")
        # Legacy page-check result: no source_hash and no review_digest.
        (self.build / "page-check/results.json").write_text(json.dumps({
            "a1|page 1": {
                "item_id": "a1", "unit": "page", "page": 1,
                "reasons": ["抽查"], "added": True, "draft": ["文字层"],
                "text_errors": "", "pairing": "", "unclear": False,
            }
        }, ensure_ascii=False), encoding="utf-8")

    def test_plan_preserves_legacy_reviews_and_ai_units(self) -> None:
        report = page_check.plan(self.build, [str(self.pkg)], {"sample_review": False}, [self.build / "package-text"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["to_check"], 0)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["legacy_preserved"], 1)
        results = json.loads((self.build / "page-check/results.json").read_text(encoding="utf-8"))
        self.assertIn("a1|page 1", results)
        ai_units = json.loads((self.build / "mineru-units/a1.json").read_text(encoding="utf-8"))["units"]
        self.assertEqual(len(ai_units), 1)
        self.assertEqual(ai_units[0]["text"], "AI checked note")

    def test_migrate_dry_run_and_apply_backfill_digest_without_deleting_unverified(self) -> None:
        # Add an unverified legacy record for a missing item; migrate must keep it untouched and report it.
        results_path = self.build / "page-check/results.json"
        data = json.loads(results_path.read_text(encoding="utf-8"))
        data["missing|page 1"] = {"item_id": "missing", "unit": "page", "page": 1, "added": False}
        results_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        preview = page_check.migrate(self.build, [str(self.pkg)], [self.build / "package-text"], dry_run=True)
        self.assertEqual(preview["migrated"], 1)
        self.assertEqual(len(preview["unmigrated"]), 1)
        # Dry run did not modify disk.
        self.assertNotIn("review_digest", json.loads(results_path.read_text(encoding="utf-8"))["a1|page 1"])

        applied = page_check.migrate(self.build, [str(self.pkg)], [self.build / "package-text"], dry_run=False)
        self.assertEqual(applied["migrated"], 1)
        self.assertEqual(len(applied["unmigrated"]), 1)
        after = json.loads(results_path.read_text(encoding="utf-8"))
        self.assertEqual(after["a1|page 1"]["source_hash"], self.source_hash)
        self.assertTrue(after["a1|page 1"]["review_digest"])
        # Unmigrated record is kept, never silently deleted.
        self.assertIn("missing|page 1", after)


class ApplyAnswersPreflightAndCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "run"
        self.build = self.out / BUILD_DIRECTORY
        (self.build / "mineru-units").mkdir(parents=True)
        (self.build / "reducto-units").mkdir(parents=True)
        (self.build / "page-check/pages").mkdir(parents=True)
        self.config_path = self.root / "kb-config.json"
        self.config_path.write_text(json.dumps({"project_id": "P-099", "client_answers": {}}, ensure_ascii=False), encoding="utf-8")
        (self.build / "governance.json").write_text(json.dumps({
            "CUS": [{
                "id": "P-099-CUS-001",
                "items": [{
                    "sub_id": "P-099-CUS-001.1", "client_id": "ans-deny-1",
                    "item_id": "doc_deny", "location": "", "question": "能否纳入知识库？", "answer_type": "binary",
                }],
            }]
        }, ensure_ascii=False), encoding="utf-8")
        (self.build / "mineru-units/doc_deny.json").write_text('{"units":[]}', encoding="utf-8")
        (self.build / "reducto-units/doc_deny.json").write_text('{"units":[]}', encoding="utf-8")
        (self.build / "page-check/pages/doc_deny-aaaa-p1.png").write_bytes(b"png")
        (self.build / "page-check/results.json").write_text(json.dumps({
            "doc_deny|page 1": {"item_id": "doc_deny", "unit": "page", "page": 1},
            "other|page 1": {"item_id": "other", "unit": "page", "page": 1},
        }), encoding="utf-8")
        self.feedback = self.root / "feedback.json"
        self.feedback.write_text(json.dumps({
            "answers": [
                {"answerId": "ans-deny-1", "cusId": "CUS-001", "decision": "deny", "sourceLocation": "", "question": "能否纳入知识库？"},
                {"answerId": "ans-old-999", "cusId": "CUS-999", "decision": "allow", "sourceLocation": "", "question": "旧问题"},
            ]
        }, ensure_ascii=False), encoding="utf-8")

    def test_dry_run_previews_and_real_run_cleans_denied_caches(self) -> None:
        dry = run_script("apply_answers.py", "--out", str(self.out), "--config", str(self.config_path),
                         "--answers", str(self.feedback), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        preview = json.loads(dry.stdout)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["recommended_action"], "full_pipeline")
        self.assertEqual(len(preview["unmatched_ids"]), 1)
        self.assertEqual(preview["denied_cache_cleanup"]["items"], ["doc_deny"])
        self.assertEqual(preview["denied_cache_cleanup"]["removed_page_checks"], 1)
        # Nothing touched on disk during --dry-run.
        self.assertTrue((self.build / "mineru-units/doc_deny.json").is_file())
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["client_answers"], {})

        real = run_script("apply_answers.py", "--out", str(self.out), "--config", str(self.config_path),
                          "--answers", str(self.feedback))
        self.assertEqual(real.returncode, 0, real.stdout + real.stderr)
        result = json.loads(real.stdout)
        self.assertFalse(result["dry_run"])
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["client_answers"]["doc_deny"], "drop")
        self.assertFalse((self.build / "mineru-units/doc_deny.json").exists())
        self.assertFalse((self.build / "reducto-units/doc_deny.json").exists())
        self.assertFalse((self.build / "page-check/pages/doc_deny-aaaa-p1.png").exists())
        remaining_results = json.loads((self.build / "page-check/results.json").read_text(encoding="utf-8"))
        self.assertNotIn("doc_deny|page 1", remaining_results)
        self.assertIn("other|page 1", remaining_results)


class FastStateCompareTests(unittest.TestCase):
    def test_same_uses_size_and_mtime_for_binary_cache_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = root / "a.png", root / "b.png"
            a.write_bytes(b"123456")
            b.write_bytes(b"123456")
            self.assertTrue(kb_state.same(a, b))
            b.write_bytes(b"1234567")
            self.assertFalse(kb_state.same(a, b))

    def test_run_pipeline_fails_fast_when_explicit_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            done = run_script("run_pipeline.py", "--out", str(root / "run"), "--vault", str(root / "vault"),
                              "--config", str(root / "missing-config.json"))
            self.assertEqual(done.returncode, 2)
            self.assertIn("找不到指定的 --config 文件", done.stdout)
            self.assertIn("请省略 --config", done.stdout)


if __name__ == "__main__":
    unittest.main()
