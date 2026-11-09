#!/usr/bin/env python3
"""Pictures and videos the screening kept by folder: filed as material, asked about once per folder."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RENDERS = "建站资料/产品/SS50/渲染图"
PHOTOS = "建站资料/产品/SS50/实拍图"
VIDEOS = "建站资料/产品/SS50/渲染视频"


def media(item_id: str, relpath: str, kind: str, **extra) -> dict:
    label = {"image": "产品资料", "video": "营销宣传"}[kind]
    return {
        "item_id": item_id, "display_name": Path(relpath).name, "decision": "A",
        "resource_type": "product" if kind == "image" else "marketing", "asset_kind": kind,
        "source_hash": (item_id * 64)[:64], "path": f"A-原文保留/{label}/{relpath}", "source_relpath": relpath,
        "copy_status": "copied", "usable_as_fact": True, "pending_client_review": False, "pending_units": [],
        "byte_size": 4, "folder_rule": str(Path(relpath).parent.as_posix()), **extra,
    }


class MediaFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.package, self.text = self.root / "package", self.root / "text"
        (self.package / "审计记录").mkdir(parents=True)
        (self.package / "C-知识点").mkdir()
        (self.package / "C-知识点/knowledge-points.jsonl").write_text("", encoding="utf-8")
        self.text.mkdir()
        waiting = {"usable_as_fact": False, "pending_client_review": True, "review_group": PHOTOS,
                   "review_question": "这些照片可以放到官网上吗？"}
        assets = [
            media("r1", f"{RENDERS}/正视图.png", "image"),
            media("r2", f"{RENDERS}/侧视图.png", "image"),
            media("p1", f"{PHOTOS}/1.jpg", "image", **waiting),
            media("p2", f"{PHOTOS}/2.jpg", "image", **waiting),
            media("v1", f"{VIDEOS}/宣传片.mp4", "video"),
            {"item_id": "doc", "display_name": "公司简介.pdf", "decision": "A", "resource_type": "marketing",
             "asset_kind": "document", "source_hash": "d" * 64, "path": "A-原文保留/营销宣传/公司简介.pdf",
             "copy_status": "copied", "usable_as_fact": True, "pending_client_review": False, "pending_units": []},
        ]
        for entry in assets:
            target = self.package / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry["item_id"].encode() * 2)
        (self.package / "审计记录/package-manifest.json").write_text(json.dumps({"assets": assets}, ensure_ascii=False), encoding="utf-8")
        (self.package / "审计记录/triage-results.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        units = [{"locator": "page 1", "text": "测试公司专注数字标牌的研发和生产。"}]
        (self.text / "doc.json").write_text(json.dumps({"item_id": "doc", "units": units}, ensure_ascii=False), encoding="utf-8")
        self.config = self.root / "kb-config.json"
        self.config.write_text(json.dumps({"company": "测试公司", "own_terms": ["测试公司"], "own_domains": [], "own_contacts": [],
                                           "deny_terms": [], "sample_review": False}, ensure_ascii=False), encoding="utf-8")
        self.out = self.root / "run"
        self.build = self.out / "04_知识库构建"

    def run_script(self, name: str, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / name), *arguments], capture_output=True, text=True, encoding="utf-8")

    def pipeline(self) -> None:
        done = self.run_script("run_pipeline.py", "--package", str(self.package), "--text-dir", str(self.text), "--out", str(self.out),
                               "--config", str(self.config), "--images", "none", "--tiers", "T1,T2,T3,T4", "--skip-t3-gate")
        self.assertEqual(done.returncode, 0, done.stdout[-1500:] + done.stderr[-1500:])

    def pictures(self) -> dict:
        return json.loads((self.build / "pictures.json").read_text(encoding="utf-8"))

    def test_folder_media_is_filed_and_the_waiting_folder_is_one_question(self) -> None:
        self.pipeline()
        routes = json.loads((self.build / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual({i: routes[i]["reason"] for i in ("r1", "p1", "v1")},
                         {"r1": "media_kept", "p1": "media_awaiting_client", "v1": "media_kept"})
        files = sorted(entry["file"] for entry in self.pictures().values())
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.startswith("资料包素材/产品/SS50/渲染图/渲染图-") for f in files), files)

        index = (self.build / "vault/03_正式知识/90_素材/00_素材索引.md").read_text(encoding="utf-8")
        self.assertIn("## 留在初筛包里的素材", index)
        self.assertIn("| 建站资料/产品/SS50/渲染视频 | 视频 1 个 | 已保留，内容待转写 |", index)
        self.assertIn("| 建站资料/产品/SS50/实拍图 | 图片 2 个 | 待客户确认（整个文件夹） |", index)

        with (self.build / "客户确认表.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.DictReader(handle) if "实拍图" in row["原文件名"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["要确认的问题"], "这些照片可以放到官网上吗？")
        self.assertIn("2 个文件", rows[0]["原文件名"])

        name_map = (self.package / "审计记录/知识库文件对照.md").read_text(encoding="utf-8")
        self.assertIn("进素材库：90_素材/资料包素材/产品/SS50/渲染图/", name_map)
        self.assertIn("视频或音频，原件留在初筛包", name_map)

        verified = self.run_script("verify_kb.py", "--package", str(self.package), "--text-dir", str(self.text),
                                   "--out", str(self.out), "--config", str(self.config))
        self.assertNotIn("no route", verified.stdout + verified.stderr)

        # One answer from the client opens the whole folder.
        rows[0]["回答（保留 / 不用 / 文字）"] = "保留"
        answers = self.root / "answers.csv"
        with answers.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerow(rows[0])
        applied = self.run_script("apply_answers.py", "--out", str(self.out), "--config", str(self.config), "--answers", str(answers))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.pipeline()
        files = sorted(entry["file"] for entry in self.pictures().values())
        self.assertEqual(len(files), 4)
        self.assertEqual(sum("/实拍图/" in f for f in files), 2)


if __name__ == "__main__":
    unittest.main()
