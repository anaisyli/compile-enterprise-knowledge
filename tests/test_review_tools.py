#!/usr/bin/env python3
"""D files kept as material, questions we put to the client, guessed names, and the tools around them."""

from __future__ import annotations

import csv
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_extensions import Base, run  # noqa: E402
from test_pipeline import asset  # noqa: E402


class ReviewToolTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.edit_config(project_id="P-998")

    def add_assets(self, *added: dict) -> None:
        path = self.package / "审计记录/package-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["assets"].extend(added)
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False),
                                                  encoding="utf-8")

    def config_value(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def routes(self) -> dict:
        return json.loads((self.build / "routes.json").read_text(encoding="utf-8"))

    def ledger(self, relative: str) -> str:
        return (self.build / "vault/05_数据与审核/30_异常与待决定" / relative).read_text(encoding="utf-8")

    def built(self, *extra: str):
        result = self.pipeline(*extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def pictures(self) -> None:
        self.add_assets(asset("p1", "主图.psd", "D", "unknown", path="D/产品/广告机/主图.psd", source_relpath="产品/广告机/主图.psd"),
                        asset("p2", "白底图.jpg", "D", "unknown", path="D/产品/广告机/白底图.jpg", source_relpath="产品/广告机/白底图.jpg"),
                        asset("v1", "宣传片.mp4", "D", "unknown", path="D/产品/广告机/宣传片.mp4", source_relpath="产品/广告机/宣传片.mp4"))

    def test_a_kept_d_picture_or_video_is_material_not_a_document(self) -> None:
        self.pictures()
        self.edit_config(d_keep=["p1", "p2", "v1"])
        self.built()
        routes = self.routes()
        for item in ("p1", "p2", "v1"):
            self.assertEqual((routes[item]["route"], routes[item]["reason"]), ("media", "media_kept"), item)
        docs = [json.loads(line)["item_id"] for line in (self.build / "docs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse({"p1", "p2", "v1"} & set(docs))
        media = {m["item_id"]: m for m in json.loads((self.build / "media.json").read_text(encoding="utf-8"))}
        self.assertEqual(media["p1"]["kind"], "image")  # A .psd is a picture to the material index.
        self.assertEqual(media["v1"]["kind"], "video")

    def test_a_picture_named_only_jpg_is_still_a_picture(self) -> None:
        self.add_assets(asset("p9", ".jpg", "D", "unknown", path="D/产品/LED/.jpg", source_relpath="产品/LED/.jpg"))
        self.edit_config(d_keep=["p9"], sample_review=True)
        self.built()
        self.assertEqual(self.routes()["p9"]["reason"], "media_kept")
        gate = json.loads((self.build / "sample-gate.json").read_text(encoding="utf-8"))
        self.assertNotIn("other", gate.get("kinds", {}))

    def test_a_document_we_ask_about_waits_in_cus_until_the_client_answers(self) -> None:
        self.write_units("d1", [{"locator": "page 1", "text": "DS50 壁挂广告机说明书，含安装步骤。"}])
        self.edit_config(client_answers={"d1": "keep"}, ask_client={"d1": "参考整理稿，请公司确认是不是自己的资料"})
        self.built()
        self.assertEqual(self.routes()["d1"]["reason"], "awaiting_client")
        cus = self.ledger("10_待客户补充/01_待客户补充事项.md")
        self.assertIn("参考整理稿，请公司确认是不是自己的资料", cus)
        table = self.build / "客户确认表.csv"
        with table.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
            fields = list(rows[0].keys())
        answer_column = next(f for f in fields if f.startswith("回答"))
        asked = [r for r in rows if "说明书" in json.dumps(r, ensure_ascii=False) or "scan" in json.dumps(r, ensure_ascii=False)]
        self.assertTrue(asked, rows)
        for row in asked:
            row[answer_column] = "保留"
        with table.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        applied = run("apply_answers.py", "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        config = self.config_value()
        self.assertNotIn("d1", config.get("ask_client", {}))
        self.assertEqual(config["client_answers"]["d1"], "keep")
        self.built()
        self.assertEqual(self.routes()["d1"]["reason"], "resolved_by_answer")

    def test_pictures_we_ask_about_are_one_question_per_folder(self) -> None:
        self.pictures()
        self.edit_config(ask_client={"p1": "现场实拍，可能露出客户标志", "p2": "现场实拍，可能露出客户标志"})
        self.built()
        routes = self.routes()
        self.assertEqual(routes["p1"]["reason"], "media_awaiting_client")
        cus = self.ledger("10_待客户补充/01_待客户补充事项.md")
        self.assertIn("2 个素材能否公开使用", cus)
        self.assertIn("现场实拍，可能露出客户标志", cus)

    def test_a_guessed_name_found_in_many_documents_removes_nothing(self) -> None:
        self.write_units("b1", [{"locator": "sheet 术语 (visible)",
                                 "text": "A1=钢化玻璃\nA3=ITEM NO; B3=DESCRIPTION; C3=FABRIC\nA4=AB-100; B4=Sherpa jacket; C4=100% POLYESTER"}])
        for i in range(6):
            self.add_assets(asset(f"s{i}", f"规格书{i}.txt", "A", "product", path=f"A/规格书{i}.txt"))
            self.write_units(f"s{i}", [{"locator": "file", "text": f"第 {i} 款：钢化玻璃面板，亮度 500 尼特。"}])
        self.built()
        blocks = [json.loads(line) for line in (self.build / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
        spec = [b for b in blocks if b["doc_id"].startswith("DOC-") and "钢化玻璃面板" in b["text"]]
        self.assertTrue(spec)
        self.assertFalse([b for b in spec if any(r.startswith("customer_name") for r in b.get("hold_reasons", []))])
        queue = (self.build / "review-queue.md").read_text(encoding="utf-8")
        self.assertIn("- 钢化玻璃（出现在 ", queue)
        terms = json.loads((self.build / "guard-terms.json").read_text(encoding="utf-8"))
        self.assertNotIn("钢化玻璃", terms["soft"])

    def test_a_guessed_name_in_one_document_is_still_removed(self) -> None:
        self.built()
        self.assertNotIn("Acme Fashion Traders", self.published())

    def test_settings_only_the_vault_has_come_into_the_run_config(self) -> None:
        self.built()
        vault = self.root / "vaults" / "P-998_测试知识库"
        made = run("kb_project.py", "--vault-root", str(self.root / "vaults"), "--project-id", "P-998", "--name", "测试",
                   "--website", "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.assertEqual(run("publish_vault.py", "--out", str(self.out), "--vault", str(vault)).returncode, 0)
        kept = vault / "05_数据与审核/70_构建状态/kb-config.json"
        state = json.loads(kept.read_text(encoding="utf-8"))
        state["products"] = [{"name": "DS50系列", "category": "广告机", "aliases": ["DS50"]}]
        kept.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        result = self.built("--vault", str(vault))
        self.assertIn("已补进", result.stdout)
        self.assertEqual(self.config_value()["products"][0]["name"], "DS50系列")
        self.assertTrue(list(self.config.parent.glob("kb-config.*.bak.json")))

    def test_a_dry_run_publish_writes_nothing(self) -> None:
        self.built()
        vault = self.root / "vaults" / "P-998_测试知识库"
        run("kb_project.py", "--vault-root", str(self.root / "vaults"), "--project-id", "P-998", "--name", "测试",
            "--website", "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm")
        before = sorted(str(p.relative_to(vault)) for p in vault.rglob("*"))
        result = run("publish_vault.py", "--out", str(self.out), "--vault", str(vault), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["dry_run"])
        self.assertGreater(report["added"], 0)
        self.assertEqual(before, sorted(str(p.relative_to(vault)) for p in vault.rglob("*")))

    def test_d_review_lists_both_piles_and_writes_the_answers_back(self) -> None:
        self.write_units("d1", [{"locator": "page 1", "text": "规格书：亮度 500 尼特。Buy One Get One Free"}])
        self.add_assets(asset("z1", "旧资料.zip", "D", "unknown", path="D/旧资料/旧资料.zip", source_relpath="旧资料/旧资料.zip"))
        self.edit_config(d_keep=["d1"])
        self.built()
        exported = run("d_review.py", "export", "--out", str(self.out))
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        folder = self.build / "d-review"
        approve = [json.loads(line) for line in (folder / "todo-approve.jsonl").read_text(encoding="utf-8").splitlines()]
        decide = [json.loads(line) for line in (folder / "todo-decide.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([a["item_id"] for a in approve], ["d1"])
        self.assertTrue(approve[0]["watch"], approve[0])  # The promotion is pointed out.
        self.assertEqual([d["item_id"] for d in decide], ["z1"])
        promo = approve[0]["watch"][0]["block_id"]
        (folder / "answer-1.jsonl").write_text(
            json.dumps({"doc_id": approve[0]["doc_id"], "decision": "approve_after_drop", "drop_blocks": [promo]}) + "\n"
            + json.dumps({"folder": decide[0]["folder"], "decision": "drop", "reason": "压缩包"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        collected = run("d_review.py", "collect", "--out", str(self.out), "--config", str(self.config))
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        decisions = json.loads((self.build / "review-decisions.json").read_text(encoding="utf-8"))
        self.assertIn(approve[0]["doc_id"], decisions["approve_docs"])
        self.assertIn(promo, decisions["drop_blocks"])
        self.assertIn("z1", self.config_value()["d_drop"])
        result = self.built()
        self.assertNotIn("还没决定", result.stdout)

    def test_d_review_refuses_answers_it_cannot_place(self) -> None:
        self.built()
        run("d_review.py", "export", "--out", str(self.out))
        (self.build / "d-review/answer-1.jsonl").write_text(json.dumps({"doc_id": "DOC-nothing", "decision": "approve"}) + "\n",
                                                            encoding="utf-8")
        collected = run("d_review.py", "collect", "--out", str(self.out), "--config", str(self.config))
        self.assertNotEqual(collected.returncode, 0)
        self.assertIn("什么都没写", collected.stdout + collected.stderr)

    def test_a_pdf_in_a_sibling_pdf_folder_is_the_word_file_exported(self) -> None:
        self.add_assets(asset("w1", "X规格书.docx", "A", "product", path="A/规格书/中文版/X规格书.docx",
                              source_relpath="规格书/中文版/X规格书.docx"),
                        asset("w2", "X规格书.pdf", "A", "product", path="A/规格书/PDF/X规格书.pdf",
                              source_relpath="规格书/PDF/X规格书.pdf"))
        self.write_units("w1", [{"locator": "para 1", "text": "X 型号，亮度 500 尼特。"}])
        self.write_units("w2", [{"locator": "page 1", "text": "X 型号，亮度 500 尼特。"}])
        self.built()
        routes = self.routes()
        self.assertEqual((routes["w2"]["reason"], routes["w2"]["same_as"]), ("other_format", "w1"))

    def test_a_second_build_in_the_same_run_folder_stops(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / ".run_pipeline.lock").write_text(f"{os.getpid()} test\n", encoding="utf-8")
        result = self.pipeline()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("已经有一次建库在跑", result.stdout)
        (self.out / ".run_pipeline.lock").unlink()
        self.built()
        self.assertFalse((self.out / ".run_pipeline.lock").exists())


if __name__ == "__main__":
    unittest.main()
