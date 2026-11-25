"""Tests for scripts/kb_next.py: Stage-1 smart routing, D-class rescue, unhandled inventory, and 5-stage checkpoints."""

from __future__ import annotations

import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_progress import (PIPELINE_CHECKPOINT, RECEIPT_PATH, VERIFY_REPORT, batch_id,
                            curation_inputs, extracted_inputs, file_digest, package_digests, routing_inputs)
from kb_common import load_config, load_json, write_json


def record_pipeline(build: Path, config_path: Path, pkg: Path) -> None:
    config = load_config(config_path)
    write_json(build / PIPELINE_CHECKPOINT, {
        "build_id": "test-build", "batch_id": batch_id(config),
        "build_stage": config["build_stage"], "packages": package_digests([pkg]),
        "routing": routing_inputs(build, config), "source_inputs": extracted_inputs(build),
        "curation": curation_inputs(build), "report": file_digest(build / VERIFY_REPORT),
        "config": file_digest(config_path),
    })


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
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / script), *args], capture_output=True, text=True,
            encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}, check=False,
        )

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script("kb_next.py", *args)

    def test_guided_build_publishes_only_the_completed_batch(self) -> None:
        from tests.test_package_only import make_docx, sha

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, out, vault = root / "pkg", root / "run", root / "vault"
            out.mkdir()
            (vault / "01_工作台").mkdir(parents=True)
            cfg = out / "kb-config.json"
            write_json(cfg, {"company": "示例公司", "package_read_only": True, "curation": {"ledger": "warn"}})
            pkg.mkdir()
            source = pkg / "公司简介.docx"
            make_docx(source, ["公司专注户外服装与摇粒绒夹克开发。"])
            write_json(pkg / "审计记录/package-manifest.json", {"assets": [
                {"item_id": "intro", "display_name": source.name, "decision": "A", "resource_type": "marketing",
                 "asset_kind": "document", "path": source.name, "copy_status": "copied", "source_hash": sha(source),
                 "usable_as_fact": True},
            ]})
            write_json(pkg / "审计记录/triage-results.json", {"items": []})
            scope = ("--out", str(out), "--package", str(pkg), "--config", str(cfg))
            publish = ("--out", str(out), "--vault", str(vault))
            build = out / "04_知识库构建"

            def success(result: subprocess.CompletedProcess[str]) -> None:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            success(self.run_cli("plan", *scope))
            success(self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg),
                                 "--approved-by", "测试员", "--evidence", "确认建库"))
            success(self.run_script("run_pipeline.py", *scope, "--images", "none"))
            self.assertEqual(json.loads(self.run_cli("status", *scope).stdout)["stage"], "stage4_governance")
            self.assertNotEqual(self.run_script("publish_vault.py", *publish).returncode, 0)
            self.assertFalse((vault / RECEIPT_PATH).exists())
            success(self.run_cli("mark-stage4", "--out", str(out)))

            write_json(cfg, {**load_json(cfg), "build_stage": "full"})
            success(self.run_script("run_pipeline.py", *scope, "--from", "curate", "--images", "none"))
            topics = load_json(build / "curation/topics.json")
            self.assertTrue(any(topic.get("blocks") for topic in topics))
            write_json(cfg, {**load_json(cfg), "skip_topics": {topic["slug"]: "此摘录只用于验证发布" for topic in topics}})
            success(self.run_script("run_pipeline.py", *scope, "--from", "curate", "--images", "none"))
            success(self.run_script("publish_vault.py", *publish))
            self.assertEqual(json.loads(self.run_cli("status", *scope, "--vault", str(vault)).stdout)["stage"], "done")
            self.assertEqual(load_json(vault / RECEIPT_PATH)["build_id"], load_json(build / PIPELINE_CHECKPOINT)["build_id"])

    def test_mineru_runs_approved_files_even_when_other_files_have_empty_pages(self) -> None:
        import mineru_ocr
        from tests.test_package_only import sha

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg, out = root / "pkg", root / "run"
            pkg.mkdir()
            cfg = root / "kb-config.json"
            build = out / "04_知识库构建"
            files = {key: pkg / f"{key}.pdf" for key in ("approved", "other")}
            for path in files.values():
                make_pdf(path)
            write_json(pkg / "审计记录/package-manifest.json",
                       {"assets": [{"item_id": key, "path": path.name} for key, path in files.items()]})
            write_json(build / "routes.json", {
                key: {"route": "kb", "display_name": path.name, "source_hash": sha(path), "ocr_pages": [1]}
                for key, path in files.items()
            })
            write_json(build / "sample-decisions.json", {"文件:approved": {"method": "mineru"}, "文件:other": {"method": "text"}})
            write_json(cfg, {"mineru": {"approved": True, "approved_by": "测试员", "approved_at": "2026-11-25",
                                       "batch_scoped": True, "approved_item_ids": ["approved"],
                                       "approved_source_hashes": {"approved": sha(files["approved"])}}})
            argv = ["mineru_ocr.py", "run", "--out", str(out), "--package", str(pkg), "--config", str(cfg)]
            with (mock.patch.object(sys, "argv", argv), mock.patch.object(mineru_ocr, "run_lock", return_value=True),
                  mock.patch.object(mineru_ocr, "client_from_env"), mock.patch.object(mineru_ocr, "extract") as extract,
                  mock.patch.object(mineru_ocr, "page_units", return_value=[{"locator": "page 1 ocr", "text": "content"}]),
                  contextlib.redirect_stdout(io.StringIO()) as stdout):
                self.assertEqual(mineru_ocr.main(), 0)
            extract.assert_called_once()
            self.assertEqual(extract.call_args.args[1], files["approved"])
            self.assertEqual(json.loads(stdout.getvalue())["outside_approval"], ["other"])

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

            # --allow-undecided-d is rejected in standard (non-fast) mode
            bad_allow = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员", "--evidence", "同意", "--allow-undecided-d")
            self.assertNotEqual(bad_allow.returncode, 0)
            self.assertIn("仅限 --fast", bad_allow.stderr)

            # Without --evidence, formal apply-plan refuses to fabricate verbatim approval text
            no_ev = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员")
            self.assertNotEqual(no_ev.returncode, 0)
            self.assertIn("--evidence", no_ev.stderr)

            # Without --d-decisions, apply-plan refuses to silently drop undecided D-class files
            blocked = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员", "--evidence", "同意")
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("尚有 2 份 D 类文件未初判", blocked.stderr)

            # Conflicting D-class decisions (same item_id both keep and drop) are rejected
            bad_d = root / "bad-d.jsonl"
            bad_d.write_text(
                json.dumps({"item_id": "item-d-doc", "decision": "keep"}) + "\n"
                + json.dumps({"item_id": "item-d-doc", "decision": "drop"}) + "\n",
                encoding="utf-8",
            )
            conflict = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--d-decisions", str(bad_d), "--dry-run")
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("D 类决定冲突", conflict.stderr)

            # Dry-run works WITHOUT --approved-by or --evidence and previews effective_pages without writing anything
            d_file = root / "d-dec.jsonl"
            d_file.write_text(
                json.dumps({"item_id": "item-d-doc", "decision": "keep"}) + "\n"
                + json.dumps({"item_id": "item-zip", "decision": "drop"}) + "\n",
                encoding="utf-8",
            )
            dry_res = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--d-decisions", str(d_file), "--dry-run")
            self.assertEqual(dry_res.returncode, 0, dry_res.stderr)
            dry = json.loads(dry_res.stdout)
            self.assertFalse(dry["applied"])
            self.assertTrue(dry["dry_run"])
            self.assertEqual(dry["effective_pages"]["mineru_pages"], 5)
            self.assertFalse((out / "04_知识库构建/sample-decisions.json").exists())

            # Real apply-plan writes decisions + verbatim evidence
            ap = self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员", "--evidence", "同意，按推荐开跑", "--d-decisions", str(d_file))
            self.assertEqual(ap.returncode, 0, ap.stderr)
            ap_data = json.loads(ap.stdout)
            self.assertEqual(ap_data["d_applied"]["keep"], ["item-d-doc"])
            self.assertEqual(ap_data["d_applied"]["drop"], ["item-zip"])
            self.assertEqual(ap_data["d_still_undecided"], [])
            self.assertEqual(ap_data["build_stage"], "sources_only")
            # item-spec (3p) + rescued D-class item-d-doc (2p) = 5 MinerU pages
            self.assertEqual(ap_data["effective_pages"]["mineru_pages"], 5)
            self.assertEqual(ap_data["effective_pages"]["mineru_unknown_pages"], 0)
            self.assertEqual(ap_data["effective_pages"]["reducto_pages"], 9)
            self.assertEqual(ap_data["effective_pages"]["reducto_unknown_pages"], 0)
            self.assertEqual(ap_data["cloud_approvals"], ["mineru", "reducto"])

            updated_cfg = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(updated_cfg["build_stage"], "sources_only")
            self.assertEqual(updated_cfg["stage_authorization"]["approved_by"], "测试员")
            self.assertEqual(updated_cfg["stage_authorization"]["evidence"], "同意，按推荐开跑")
            self.assertTrue(updated_cfg["mineru"]["approved"])
            self.assertEqual(updated_cfg["mineru"]["approved_item_ids"], ["item-d-doc", "item-spec"])
            self.assertEqual(updated_cfg["reducto"]["approved_item_ids"], ["item-cat", "item-ppt"])

            # resume-full is rejected on a standard (non-fast) project
            bad_resume = self.run_cli("resume-full", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员")
            self.assertNotEqual(bad_resume.returncode, 0)
            self.assertIn("不是快车道极简版", bad_resume.stderr)
            self.assertTrue(updated_cfg["reducto"]["approved"])
            self.assertIn("item-d-doc", updated_cfg["d_keep"])
            self.assertIn("item-zip", updated_cfg["d_drop"])

            # After apply -> Stage 2 (routes not ready yet: must run run_pipeline.py before OCR)
            st2 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st2["stage"], "stage2_extract")
            self.assertFalse(st2["routes_ready"])
            self.assertIn("先运行 run_pipeline.py", st2["instruction"])

            # Simulate docs/routes WITHOUT OCR outputs -> STILL Stage 2 (catches unfinished MinerU/Reducto!)
            build = out / "04_知识库构建"
            (build / "docs.jsonl").write_text("{}\n", encoding="utf-8")
            (build / "routes.json").write_text("{}", encoding="utf-8")
            st2_ocr = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st2_ocr["stage"], "stage2_extract")
            self.assertIn("item-spec", st2_ocr["ocr_pending"]["mineru_missing"])
            self.assertIn("item-cat", st2_ocr["ocr_pending"]["reducto_missing"])

            # Write OCR outputs + page-triage.json + refresh docs.jsonl -> Stage 3 resumes exact count
            (build / "mineru-units").mkdir(parents=True, exist_ok=True)
            (build / "reducto-units").mkdir(parents=True, exist_ok=True)
            write_json(build / "routes.json", {iid: {"route": "kb", "source_hash": iid}
                                              for iid in ("item-spec", "item-d-doc", "item-cat", "item-ppt")})
            for iid in ("item-spec", "item-d-doc"):
                write_json(build / "mineru-units" / f"{iid}.json",
                           {"source": "mineru", "source_hash": iid, "units": [{"locator": "page 1 ocr", "text": "data"}]})
            for iid in ("item-cat", "item-ppt"):
                write_json(build / "reducto-units" / f"{iid}.json",
                           {"source": "reducto", "source_hash": "stale", "pages_read": [1]})
            write_json(build / "page-triage.json", {"files": {iid: {"pages": {"1": {}, "2": {}}}
                                                            for iid in ("item-cat", "item-ppt")}})
            (build / "docs.jsonl").write_text("{}\n", encoding="utf-8")
            record_pipeline(build, cfg, pkg)
            read_status = lambda: json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg),
                                                          "--config", str(cfg)).stdout)
            self.assertEqual(read_status()["ocr_pending"]["reducto_missing"], ["item-cat", "item-ppt"])
            for iid in ("item-cat", "item-ppt"):
                write_json(build / "reducto-units" / f"{iid}.json",
                           {"source": "reducto", "source_hash": iid, "pages_read": [1]})
            self.assertEqual(read_status()["ocr_pending"]["reducto_missing"], ["item-cat", "item-ppt"])
            for iid in ("item-cat", "item-ppt"):
                write_json(build / "reducto-units" / f"{iid}.json",
                           {"source": "reducto", "source_hash": iid, "pages_read": [1, 2]})
            self.assertTrue(read_status()["needs_ocr_merge"])
            record_pipeline(build, cfg, pkg)
            (build / "page-check").mkdir(parents=True, exist_ok=True)
            (build / "page-check/todo.jsonl").write_text(
                '{"key": "a|page 1"}\n{"key": "a|page 2"}\n{"key": "a|page 3"}\n', encoding="utf-8"
            )
            (build / "page-check/results.json").write_text('{"a|page 1": {"added": false}}', encoding="utf-8")
            st3 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st3["stage"], "stage3_page_check")
            self.assertEqual(st3["page_check"]["done"], 1)
            self.assertEqual(st3["page_check"]["remaining"], 2)

            # Complete all page-check items and refresh docs.jsonl -> Stage 4
            (build / "page-check/results.json").write_text(
                '{"a|page 1": {}, "a|page 2": {}, "a|page 3": {}}', encoding="utf-8"
            )
            self.assertTrue(read_status()["page_check"]["needs_refresh"])
            record_pipeline(build, cfg, pkg)
            st4 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st4["stage"], "stage4_governance")

            # mark-stage4 rejects nonexistent HTML paths
            bad_m4 = self.run_cli("mark-stage4", "--out", str(out), "--internal-html", str(root / "missing.html"))
            self.assertNotEqual(bad_m4.returncode, 0)
            self.assertIn("找不到", bad_m4.stderr)

            # Mark Stage 4 complete with real HTML files -> Stage 5
            html_a, html_b = root / "a.html", root / "b.html"
            html_a.write_text("<html/>", encoding="utf-8")
            html_b.write_text("<html/>", encoding="utf-8")
            self.assertEqual(self.run_cli("mark-stage4", "--out", str(out), "--internal-html", str(html_a), "--customer-html", str(html_b)).returncode, 0)
            st5 = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg), "--vault", str(vault)).stdout)
            self.assertEqual(st5["stage"], "stage5_curate_publish")

            # Complete Stage 5 -> Done (only when --vault has receipt; without --vault never reports published=True)
            updated_cfg["build_stage"] = "full"
            cfg.write_text(json.dumps(updated_cfg, ensure_ascii=False), encoding="utf-8")
            (build / "curation/company").mkdir(parents=True, exist_ok=True)
            (build / "curation/topics.json").write_text(json.dumps([{"slug": "company", "blocks": ["b1"]}]), encoding="utf-8")
            (build / "curation/company/page.md").write_text("# 公司\n", encoding="utf-8")
            (build / "校验结果.json").write_text('{"verified": true}', encoding="utf-8")
            record_pipeline(build, cfg, pkg)
            st_no_vault = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st_no_vault["stage"], "stage5_curate_publish")
            self.assertFalse(st_no_vault["curation"]["published"])

            write_json(vault / RECEIPT_PATH, {"build_stage": "full", "build_id": "test-build"})
            st_done = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg), "--vault", str(vault)).stdout)
            self.assertEqual(st_done["stage"], "done")
            extra = root / "extra"
            write_json(extra / "审计记录/package-manifest.json", {"assets": []})
            self.assertEqual(self.run_cli("plan", "--out", str(out), "--config", str(cfg),
                                          "--package", str(pkg), "--package", str(extra)).returncode, 0)
            pending = json.loads(self.run_cli("status", "--out", str(out), "--config", str(cfg),
                                               "--package", str(pkg), "--package", str(extra), "--vault", str(vault)).stdout)
            self.assertEqual(pending["stage"], "stage1_plan")
            self.assertTrue(pending["plan_generated"])

    def test_fast_mvp_and_resume_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = make_package(root / "pkg")
            out = root / "run"
            cfg = root / "kb-config.json"
            cfg.write_text(json.dumps({"project_id": "P-001", "company": "甲公司"}, ensure_ascii=False), encoding="utf-8")

            self.assertEqual(self.run_cli("plan", "--package", str(pkg), "--out", str(out), "--config", str(cfg)).returncode, 0)
            # Even with --fallback-mineru, --fast must defer heavy Reducto catalogs/PPTs first
            ap = json.loads(self.run_cli("apply-plan", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员", "--evidence", "先出极简版", "--fast", "--fallback-mineru", "--allow-undecided-d").stdout)
            self.assertTrue(ap["fast_mvp"])
            self.assertEqual(ap["cloud_approvals"], ["mineru"])
            self.assertEqual(sorted(ap["deferred_items"]), ["item-cat", "item-ppt"])

            # Simulate MinerU + pipeline done -> jumps straight to Stage 5 (skips page-check & Stage 4)
            build = out / "04_知识库构建"
            write_json(build / "routes.json", {"item-spec": {"route": "kb", "source_hash": "spec"}})
            (build / "page-triage.json").write_text("{}", encoding="utf-8")
            (build / "mineru-units").mkdir(parents=True, exist_ok=True)
            write_json(build / "mineru-units/item-spec.json",
                       {"source": "mineru", "source_hash": "spec", "units": [{"locator": "page 1 ocr", "text": "data"}]})
            (build / "docs.jsonl").write_text("{}\n", encoding="utf-8")
            record_pipeline(build, cfg, pkg)
            st_mvp = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st_mvp["stage"], "stage5_curate_publish")

            # Later: resume-full restores deferred catalogs/PPTs and returns cleanly to Stage 2 for Reducto
            res = json.loads(self.run_cli("resume-full", "--out", str(out), "--config", str(cfg), "--approved-by", "测试员").stdout)
            self.assertEqual(res["restored_count"], 2)
            self.assertEqual(res["cloud_approvals"], ["reducto"])
            st_back = json.loads(self.run_cli("status", "--out", str(out), "--package", str(pkg), "--config", str(cfg)).stdout)
            self.assertEqual(st_back["stage"], "stage2_extract")
            self.assertEqual(sorted(st_back["ocr_pending"]["reducto_missing"]), ["item-cat", "item-ppt"])

    def test_resume_fast_vault_that_only_skipped_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            out = root / "fresh-run"
            cfg = out / "kb-config.json"
            write_json(vault / "05_数据与审核/70_构建状态/kb-config.json",
                       {"build_stage": "full", "deferred_items": {},
                        "page_check": {"skip": "快车道极简版暂跳过，二期补全"}})
            result = self.run_cli("resume-full", "--out", str(out), "--config", str(cfg),
                                  "--approved-by", "测试员", "--vault", str(vault))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["restored_count"], 0)
            self.assertEqual(load_json(cfg)["build_stage"], "sources_only")
            self.assertNotIn("page_check", load_json(cfg))


if __name__ == "__main__":
    unittest.main()
