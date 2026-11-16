#!/usr/bin/env python3
"""The one-time move of published pictures to the current layout: names, links, the old-to-new list."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from test_page_pictures import BUILD, make_pdf_package, run  # noqa: E402
from publish_vault import normalized_hash  # noqa: E402

ASSETS = "03_正式知识/90_素材"
RECEIPT = "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.package, self.out, self.config = self.root / "02_初筛包", self.root / "run", self.root / "kb-config.json"
        make_pdf_package(self.package, self.config)
        self.build = self.out / BUILD
        self.vault = self.root / "vaults" / "P-995_测试知识库"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def pipeline(self) -> None:
        result = run("run_pipeline.py", "--package", str(self.package), "--out", str(self.out), "--config", str(self.config),
                     "--images", "none")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def publish(self, *extra: str):
        result = run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault), *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def old_project(self) -> dict[str, str]:
        """A vault published under the old names: page pictures as 页面图/页面图-<资料>-第N页, plus a
        picture no build makes any more, and a note a person wrote linking both."""
        self.pipeline()
        named = json.loads((self.build / "pictures.json").read_text(encoding="utf-8"))
        old = {i: f"页面图/页面图-规格书-DS50 Specification-{Path(e['file']).stem.split('-')[0]}-{i[4:12]}.png"
               for i, e in named.items()}
        (self.build / "picture-names.json").write_text(json.dumps(
            {i: {"file": f, "named_by": "原文"} for i, f in old.items()}, ensure_ascii=False), encoding="utf-8")
        (self.build / "picture-layout.json").write_text(json.dumps({"layout": 1}), encoding="utf-8")
        self.pipeline()
        made = run("kb_project.py", "--vault-root", str(self.root / "vaults"), "--project-id", "P-995", "--name", "测试",
                   "--website", "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm")
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.publish()
        for file in old.values():
            self.assertTrue((self.vault / ASSETS / file).is_file(), file)
        # A picture an earlier build published and this one no longer makes.
        gone = self.vault / ASSETS / "页面图" / "页面图-旧画册-第9页-99999999.png"
        gone.write_bytes(b"old picture")
        receipt = json.loads((self.vault / RECEIPT).read_text(encoding="utf-8"))
        receipt["files"][f"{ASSETS}/页面图/页面图-旧画册-第9页-99999999.png"] = normalized_hash(gone)
        (self.vault / RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        first = Path(next(iter(old.values()))).name
        note = self.vault / "04_文章任务" / "草稿.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"配图 ![[{first}|300]] 和 ![[页面图-旧画册-第9页-99999999.png]]\n", encoding="utf-8")
        return old

    def test_relayout_moves_published_pictures_and_keeps_every_link(self) -> None:
        old = self.old_project()
        switched = run("pictures.py", "relayout", "--out", str(self.out))
        self.assertEqual(switched.returncode, 0, switched.stdout + switched.stderr)
        self.pipeline()
        preview = json.loads(self.publish("--dry-run").stdout)
        self.assertEqual((preview["pictures_moved"], preview["pictures_to_old_folder"], preview["picture_layout_migration"]),
                         (2, 1, True))
        self.assertTrue((self.vault / ASSETS / next(iter(old.values()))).is_file())  # The preview moved nothing.
        self.publish()
        named = json.loads((self.build / "pictures.json").read_text(encoding="utf-8"))
        for picture_id, entry in named.items():
            self.assertTrue(entry["file"].startswith("页面图/DS50 Specification/第"), entry["file"])
            self.assertTrue((self.vault / ASSETS / entry["file"]).is_file())
            self.assertFalse((self.vault / ASSETS / old[picture_id]).exists())
        self.assertTrue((self.vault / ASSETS / "旧版未归类" / "页面图-旧画册-第9页-99999999.png").is_file())
        note = (self.vault / "04_文章任务" / "草稿.md").read_text(encoding="utf-8")
        new_first = Path(named[next(iter(old))]["file"]).name
        self.assertIn(f"![[{new_first}|300]]", note)
        self.assertIn("页面图-旧画册-第9页-99999999.png", note)  # Same name in 旧版未归类: the link still finds it.
        with (self.vault / ASSETS / "00_旧名新名对照.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(sorted(r["状态"] for r in rows), ["按新规则改名", "按新规则改名", "本次未出现，移到旧版未归类"])
        self.assertTrue(all((self.vault / r["新路径"]).is_file() for r in rows))
        layout = json.loads((self.vault / "05_数据与审核/70_构建状态/picture-layout.json").read_text(encoding="utf-8"))
        self.assertTrue(layout["applied"])
        again = json.loads(self.publish("--dry-run").stdout)
        self.assertEqual((again["pictures_moved"], again["picture_layout_migration"]), (0, False))

    def test_a_new_project_starts_on_the_current_layout(self) -> None:
        self.pipeline()
        layout = json.loads((self.build / "picture-layout.json").read_text(encoding="utf-8"))
        self.assertEqual(layout["layout"], 2)
        self.assertTrue(layout["applied"])
        relayout = json.loads(run("pictures.py", "relayout", "--out", str(self.out)).stdout)
        self.assertFalse(relayout["changed"])


if __name__ == "__main__":
    unittest.main()
