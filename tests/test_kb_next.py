"""Tests for scripts/kb_next.py: Stage-1 smart routing, D-class rescue, unhandled inventory, and 5-stage checkpoints."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def make_pdf(path: Path, *, pages: int = 2, landscape: bool = False, text: str = "A" * 600) -> None:
    import pymupdf as fitz
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    width, height = (842, 595) if landscape else (595, 842)
    for idx in range(pages):
        page = doc.new_page(width=width, height=height)
        if text:
            page.insert_textbox(fitz.Rect(40, 40, width - 40, height - 40), f"Page {idx + 1}\n{text}")
    doc.save(str(path))
    doc.close()


def make_pptx(path: Path, slides: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(1, slides + 1):
            zf.writestr(f"ppt/slides/slide{i}.xml", "<p:sld/>")


def make_package(pkg: Path) -> Path:
    make_pdf(pkg / "规格书/产品规格书.pdf", pages=3, landscape=False, text="Standard specification text. " * 40)
    make_pdf(pkg / "产品画册/2026产品画册.pdf", pages=5, landscape=True, text="Short")
    make_pptx(pkg / "演示/公司介绍.pptx", slides=4)
    (pkg / "杂项").mkdir(parents=True, exist_ok=True)
    (pkg / "杂项/旧资料.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    make_pdf(pkg / "待确认/可能是产品手册.pdf", pages=2, landscape=False, text="Spec content " * 40)

    manifest = {
        "assets": [
            {"item_id": "item-spec", "path": "规格书/产品规格书.pdf", "display_name": "产品规格书.pdf", "decision": "A", "tier": "T1"},
            {"item_id": "item-cat", "path": "产品画册/2026产品画册.pdf", "display_name": "2026产品画册.pdf", "decision": "A", "tier": "T1"},
            {"item_id": "item-ppt", "path": "演示/公司介绍.pptx", "display_name": "公司介绍.pptx", "decision": "B", "tier": "T2"},
            {"item_id": "item-zip", "path": "杂项/旧资料.zip", "display_name": "旧资料.zip", "decision": "D", "tier": "T2"},
            {"item_id": "item-d-doc", "path": "待确认/可能是产品手册.pdf", "display_name": "可能是产品手册.pdf", "decision": "D", "tier": "T2"},
        ]
    }
    triage = {
        "items": [
            {"item_id": "item-d-doc", "summary": "初筛未确定归属", "required_follow_up": "确认是否为本公司产品手册"},
            {"item_id": "item-zip", "summary": "压缩包未展开"},
        ]
    }
    (pkg / "审计记录").mkdir(parents=True, exist_ok=True)
    (pkg / "审计记录/package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (pkg / "审计记录/triage-decisions.json").write_text(json.dumps(triage, ensure_ascii=False, indent=2), encoding="utf-8")
    return pkg


class KbNextTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "kb_next.py"), *args],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )

    def test_stage1_plan_auto_routing_d_class_and_unhandled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = make_package(root / "pkg")
            out = root / "run"
            cfg = root / "kb-config.json"
            cfg.write_text(json.dumps({"project_id": "P-001", "company": "甲公司"}, ensure_ascii=False), encoding="utf-8")

            res = self.run_cli("plan", "--package", str(pkg), "--out", str(out), "--config", str(cfg), "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)

            by_id = {f["item_id"]: f for f in data["routed_files"]}
            # Regular portrait Word-like PDF -> MinerU (3 pages)
            self.assertEqual(by_id["item-spec"]["method"], "mineru")
            self.assertEqual(by_id["item-spec"]["pages"], 3)
            # Catalog landscape PDF -> Reducto (5 pages)
            self.assertEqual(by_id["item-cat"]["method"], "reducto")
            self.assertEqual(by_id["item-cat"]["pages"], 5)
            # PPTX -> Reducto (4 slides)
            self.assertEqual(by_id["item-ppt"]["method"], "reducto")
            self.assertEqual(by_id["item-ppt"]["pages"], 4)

            self.assertEqual(data["summary"]["mineru_pages"], 3)
            self.assertEqual(data["summary"]["reducto_pages"], 9)
            self.assertEqual(data["summary"]["archives"], 1)
            self.assertEqual(data["summary"]["d_undecided"], 2)  # zip + d-doc
            self.assertTrue((out / "04_知识库构建/stage1-plan.md").is_file())

    def test_apply_plan_and_five_stage_checkpoint_progression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = make_package(root / "pkg")
            out = root / "run"
            vault = root / "vault"
            cfg = root / "kb-config.json"
            cfg.write_text(json.dumps({"project_id": "P-001", "company": "甲公司", "build_stage": "sources_only"}, ensure_ascii=False), encoding="utf-8")

            # Before plan/apply -> Stage 1
            st1 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st1["stage"], "stage1_plan")

            self.assertEqual(self.run_cli("plan", "--package", str(pkg), "--out", str(out), "--config", str(cfg)).returncode, 0)

            # Apply plan + rescue D-class doc + drop archive
            d_file = root / "d-dec.jsonl"
            d_file.write_text(
                json.dumps({"item_id": "item-d-doc", "decision": "keep"}) + "\n"
                + json.dumps({"item_id": "item-zip", "decision": "drop"}) + "\n",
                encoding="utf-8",
            )
            ap = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员", "--d-decisions", str(d_file))
            self.assertEqual(ap.returncode, 0, ap.stderr)
            ap_data = json.loads(ap.stdout)
            self.assertEqual(ap_data["d_applied"]["keep"], ["item-d-doc"])
            self.assertEqual(ap_data["d_applied"]["drop"], ["item-zip"])
            self.assertEqual(ap_data["d_still_undecided"], [])
            self.assertEqual(ap_data["build_stage"], "sources_only")
            # item-spec (3p) + rescued D-class item-d-doc (2p) = 5 MinerU pages
            self.assertEqual(ap_data["effective_pages"]["mineru_pages"], 5)
            self.assertEqual(ap_data["effective_pages"]["reducto_pages"], 9)
            self.assertEqual(ap_data["cloud_approvals"], ["mineru", "reducto"])

            updated_cfg = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(updated_cfg["build_stage"], "sources_only")
            self.assertEqual(updated_cfg["stage_authorization"]["approved_by"], "测试员")
            self.assertTrue(updated_cfg["mineru"]["approved"])
            self.assertTrue(updated_cfg["reducto"]["approved"])
            self.assertIn("item-d-doc", updated_cfg["d_keep"])
            self.assertIn("item-zip", updated_cfg["d_drop"])

            # After apply -> Stage 2
            st2 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st2["stage"], "stage2_extract")

            # Simulate docs/routes WITHOUT OCR outputs -> STILL Stage 2 (catches unfinished MinerU/Reducto!)
            build = out / "04_知识库构建"
            (build / "docs.jsonl").write_text("{}\n", encoding="utf-8")
            (build / "routes.json").write_text("{}", encoding="utf-8")
            st2_ocr = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st2_ocr["stage"], "stage2_extract")
            self.assertIn("item-spec", st2_ocr["ocr_pending"]["mineru_missing"])
            self.assertIn("item-cat", st2_ocr["ocr_pending"]["reducto_missing"])

            # Write OCR outputs + partial page-check progress -> Stage 3 resumes exact count
            (build / "mineru-units").mkdir(parents=True, exist_ok=True)
            (build / "reducto-units").mkdir(parents=True, exist_ok=True)
            for iid in ("item-spec", "item-d-doc"):
                (build / "mineru-units" / f"{iid}.json").write_text("{}", encoding="utf-8")
            for iid in ("item-cat", "item-ppt"):
                (build / "reducto-units" / f"{iid}.json").write_text("{}", encoding="utf-8")
            (build / "page-check").mkdir(parents=True, exist_ok=True)
            (build / "page-check/todo.jsonl").write_text(
                '{"key": "a|page 1"}\n{"key": "a|page 2"}\n{"key": "a|page 3"}\n', encoding="utf-8"
            )
            (build / "page-check/results.json").write_text('{"a|page 1": {"added": false}}', encoding="utf-8")
            st3 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st3["stage"], "stage3_page_check")
            self.assertEqual(st3["page_check"], {"total": 3, "done": 1, "remaining": 2})

            # Complete all page-check items -> Stage 4
            (build / "page-check/results.json").write_text(
                '{"a|page 1": {}, "a|page 2": {}, "a|page 3": {}}', encoding="utf-8"
            )
            st4 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st4["stage"], "stage4_governance")

            # Mark Stage 4 complete -> Stage 5
            self.assertEqual(self.run_cli("mark-stage4", "--out", str(out), "--internal-html", "a.html", "--customer-html", "b.html").returncode, 0)
            st5 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg), "--vault", str(vault)).stdout)
            self.assertEqual(st5["stage"], "stage5_curate_publish")

            # Complete Stage 5 -> Done
            updated_cfg["build_stage"] = "full"
            cfg.write_text(json.dumps(updated_cfg, ensure_ascii=False), encoding="utf-8")
            (build / "curation/company").mkdir(parents=True, exist_ok=True)
            (build / "curation/topics.json").write_text(json.dumps([{"slug": "company", "blocks": ["b1"]}]), encoding="utf-8")
            (build / "curation/company/page.md").write_text("# 公司\n", encoding="utf-8")
            (build / "校验结果.json").write_text('{"verified": true}', encoding="utf-8")
            (vault / "05_数据与审核/50_运行记录").mkdir(parents=True, exist_ok=True)
            (vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json").write_text('{"published_at": "2026-11-25"}', encoding="utf-8")
            st_done = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg), "--vault", str(vault)).stdout)
            self.assertEqual(st_done["stage"], "done")

    def test_fast_mvp_and_resume_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = make_package(root / "pkg")
            out = root / "run"
            cfg = root / "kb-config.json"
            cfg.write_text(json.dumps({"project_id": "P-001", "company": "甲公司"}, ensure_ascii=False), encoding="utf-8")

            self.assertEqual(self.run_cli("plan", "--package", str(pkg), "--out", str(out), "--config", str(cfg)).returncode, 0)
            ap = json.loads(self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员", "--fast").stdout)
            self.assertTrue(ap["fast_mvp"])
            self.assertEqual(ap["cloud_approvals"], ["mineru"])
            self.assertEqual(sorted(ap["deferred_items"]), ["item-cat", "item-ppt"])

            # Simulate MinerU + pipeline done -> jumps straight to Stage 5 (skips page-check & Stage 4)
            build = out / "04_知识库构建"
            (build / "docs.jsonl").write_text("{}\n", encoding="utf-8")
            (build / "routes.json").write_text("{}", encoding="utf-8")
            (build / "mineru-units").mkdir(parents=True, exist_ok=True)
            (build / "mineru-units/item-spec.json").write_text("{}", encoding="utf-8")
            st_mvp = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st_mvp["stage"], "stage5_curate_publish")

            # Later: resume-full restores deferred catalogs/PPTs and returns cleanly to Stage 2 for Reducto
            res = json.loads(self.run_cli("resume-full", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员").stdout)
            self.assertEqual(res["restored_count"], 2)
            self.assertEqual(res["cloud_approvals"], ["reducto"])
            st_back = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st_back["stage"], "stage2_extract")
            self.assertEqual(sorted(st_back["ocr_pending"]["reducto_missing"]), ["item-cat", "item-ppt"])


if __name__ == "__main__":
    unittest.main()
