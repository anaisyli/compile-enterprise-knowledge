#!/usr/bin/env python3
"""A vault and its package copied to another computer: the links follow the package."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from publish_vault import normalized_hash  # noqa: E402


class RelinkTests(unittest.TestCase):
    def test_links_aliases_and_records_follow_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "电脑A" / "02_初筛包"
            new = root / "电脑B" / "收到的" / "02_初筛包"
            vault = root / "电脑B" / "P-001_测试知识库"
            (new / "审计记录").mkdir(parents=True)
            (new / "审计记录/package-manifest.json").write_text('{"assets": []}', encoding="utf-8")
            (new / "A-原文保留").mkdir()
            (new / "A-原文保留/给某买家的画册.pdf").write_bytes(b"%PDF")
            with (new / "审计记录/知识库文件对照.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["知识库名字", "知识库位置", "初筛包里的位置（原名）", "初筛类别", "去向", "资料编号"])
                writer.writerow(["产品画册-春季", "02_源资料/10_净化原文/产品画册/产品画册-春季.md", "A-原文保留/给某买家的画册.pdf", "A", "已入库，有效", "a1"])
            page = vault / "02_源资料/10_净化原文/产品画册/产品画册-春季.md"
            page.parent.mkdir(parents=True)
            old_link = (old / "审计记录/按知识库名字/产品画册-春季.pdf").as_uri()
            outside = "file:///C:/Windows/notepad.exe"
            page.write_text(f"> - 原文件：[打开原件]({old_link})；别处的链接 [x]({outside})\n", encoding="utf-8")
            state = vault / "05_数据与审核/70_构建状态/run-info.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"packages": [str(old)]}), encoding="utf-8")
            info = vault / "01_工作台/10_项目基础信息.md"
            info.parent.mkdir(parents=True)
            info.write_text(f"- 客户原始资料路径：{old}\n", encoding="utf-8")
            receipt = vault / "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
            receipt.parent.mkdir(parents=True)
            key = page.relative_to(vault).as_posix()
            receipt.write_text(json.dumps({"files": {key: normalized_hash(page)}}), encoding="utf-8")

            result = subprocess.run([sys.executable, str(ROOT / "scripts/kb_relink.py"), "--vault", str(vault), "--package", str(new)],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            text = page.read_text(encoding="utf-8")
            self.assertIn((new / "审计记录/按知识库名字/产品画册-春季.pdf").resolve().as_uri(), text)
            self.assertIn(outside, text)  # Links into anywhere else are left alone.
            alias = new / "审计记录/按知识库名字/产品画册-春季.pdf"
            self.assertTrue(alias.is_file())
            self.assertTrue(alias.samefile(new / "A-原文保留/给某买家的画册.pdf"))
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["packages"], [str(new.resolve())])
            self.assertIn(str(new.resolve()), info.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["files"][key], normalized_hash(page))

    def test_links_on_mac_and_linux_keep_their_leading_slash(self) -> None:
        from kb_relink import link_path, moved_link
        self.assertEqual(link_path("file:///Users/xx/%E5%8C%85/a.pdf"), "/Users/xx/包/a.pdf")
        self.assertEqual(link_path("file:///C:/old/%E5%8C%85/a.pdf"), "C:/old/包/a.pdf")
        moved = moved_link("file:///Users/xx/02_%E5%88%9D%E7%AD%9B%E5%8C%85/a.pdf", {"/Users/xx/02_初筛包": Path("/Volumes/new/02_初筛包")})
        self.assertIsNotNone(moved)  # Before, a Mac link never matched its package and stayed pointing at the old place.
        self.assertTrue(moved.endswith("/a.pdf"))
        self.assertIsNone(moved_link("file:///Users/xx/elsewhere/a.pdf", {"/Users/xx/02_初筛包": Path("/Volumes/new/02_初筛包")}))

    def test_check_finds_links_that_do_not_open_here(self) -> None:
        from kb_relink import check_links
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            (vault / "a.md").write_text(f"[打开原件]({(vault / 'gone.pdf').as_uri()}) [[b]] [[不存在]] ![[pic.png|80]]\n", encoding="utf-8")
            (vault / "b.md").write_text("x", encoding="utf-8")
            (vault / "pic.png").write_bytes(b"x")
            report = check_links(vault)
        self.assertEqual((report["file_links"], report["broken"]), (1, 1))
        self.assertEqual((report["wiki_links"], report["wiki_broken"]), (3, 1))


if __name__ == "__main__":
    unittest.main()
