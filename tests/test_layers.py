#!/usr/bin/env python3
"""Source layer titles and cards, ledgers, and the knowledge layer's checker."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_extensions import Base, run  # noqa: E402


class LayerTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.clear_catalog_pending()
        self.write_units("a1", [
            {"locator": "page 1", "text": "SHERPA SERIES\nNTX S-001 SHERPA JACKET\nFABRIC: 350GSM SHERPA FLEECE"},
            {"locator": "page 2", "text": "Established in 2004, we run our own factory."},
            {"locator": "page 3", "text": "公司成立于2012年，拥有自有工厂。"},
        ])
        self.edit_config(project_id="P-999", style_prefixes=["NTX"])

    def write_units(self, item_id: str, units: list) -> None:
        (self.text / f"{item_id}.json").write_text(json.dumps({"item_id": item_id, "units": units}, ensure_ascii=False), encoding="utf-8")

    def ledger(self, name: str) -> str:
        return next((self.build / "vault/05_数据与审核").rglob(name)).read_text(encoding="utf-8")

    def test_titles_say_what_a_file_is_and_cards_say_where_it_came_from(self) -> None:
        self.assertEqual(self.pipeline().returncode, 0)
        page = next(self.clean.rglob("产品画册-Catalog.md")).read_text(encoding="utf-8")
        # The vault never prints original names; the package's map pairs the two.
        self.assertIn("原名和位置见初筛包的 `审计记录/知识库文件对照.md`", page)
        self.assertNotIn("Catalog.pdf", page)
        self.assertIn("资料编号：`a1`", page)
        name_map = (self.package / "审计记录/知识库文件对照.md").read_text(encoding="utf-8")
        self.assertIn("| 产品画册-Catalog | 02_源资料/10_净化原文/产品画册/产品画册-Catalog.md | A/Catalog.pdf | A | 已入库，有效 |", name_map)
        self.assertIn("只说明来源，不等于每句都已核实", page)

    def test_conflicting_founding_years_are_recorded_for_the_client(self) -> None:
        self.pipeline()
        cus = self.ledger("01_待客户补充事项.md")
        self.assertIn("成立年份以哪个为准", cus)
        self.assertIn("2004", cus)
        self.assertIn("2012", cus)
        self.assertNotIn("成立年份", self.ledger("01_源文与事实异常台账.md"))

    def test_ids_stay_the_same_on_a_rerun(self) -> None:
        self.pipeline()
        first = self.ledger("01_源文与事实异常台账.md")
        self.pipeline()
        self.assertEqual(first, self.ledger("01_源文与事实异常台账.md"))

    def curate(self, page: str) -> subprocess.CompletedProcess:
        prepared = json.loads(run("curate.py", "prepare", "--out", str(self.out), "--config", str(self.config)).stdout)
        self.assertTrue(prepared["topics"])
        folder = self.build / "curation" / "company"
        (folder / "page.md").write_text(page, encoding="utf-8")
        return run("curate.py", "check", "--out", str(self.out))

    def block_id(self, text: str) -> str:
        topic = json.loads((self.build / "curation/company/topic.json").read_text(encoding="utf-8"))
        blocks = [json.loads(line) for line in (self.build / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
        return next(b["block_id"] for b in blocks if text in b["text"] and b["block_id"] in topic["blocks"])

    def test_checker_refuses_uncited_lines_and_numbers_not_in_the_source(self) -> None:
        self.pipeline()
        run("curate.py", "prepare", "--out", str(self.out), "--config", str(self.config))
        source = self.block_id("Established in 2004")
        result = self.curate(f"# 公司概况\n\n## 一句话说明\n\n- 公司成立于 2005 年。{{{{src:{source}}}}}\n- 拥有自有工厂。\n")
        self.assertEqual(result.returncode, 1)
        report = json.loads((self.build / "curation-report.json").read_text(encoding="utf-8"))["company"]["errors"]
        self.assertTrue(any("2005" in e for e in report))
        self.assertTrue(any("没有标出处" in e for e in report))

    def test_checked_page_becomes_a_linked_knowledge_page(self) -> None:
        self.pipeline()
        run("curate.py", "prepare", "--out", str(self.out), "--config", str(self.config))
        a, b = self.block_id("Established in 2004"), self.block_id("成立于2012")
        page = (f"# 公司概况\n\n## 一句话说明\n\n- 有自有工厂。{{{{src:{a}}}}}\n\n## 需核实\n\n"
                f"- 成立年份说法不一：2004 年{{{{src:{a}}}}}，2012 年{{{{src:{b}}}}}，需核实。\n")
        self.assertEqual(self.curate(page).returncode, 0)
        # One page written and the rest left open is not a finished knowledge layer.
        result = self.pipeline()
        self.assertEqual(result.returncode, 1)
        self.assertIn("整理页没写完", result.stdout)
        topics = json.loads((self.build / "curation/topics.json").read_text(encoding="utf-8"))
        self.edit_config(skip_topics={t["slug"]: "测试里不写" for t in topics if t["slug"] != "company"})
        self.assertEqual(self.pipeline().returncode, 0)
        built = next((self.build / "vault/03_正式知识").rglob("公司-概况.md")).read_text(encoding="utf-8")
        self.assertIn("〔1〕]]", built)
        self.assertIn("产品画册-Catalog#第 2 页", built)
        self.assertIn("整理页：由 AI 按原文归纳", built)

    def test_first_publish_to_a_new_vault_has_no_withdrawn_records(self) -> None:
        self.pipeline()
        self.pipeline()
        root = self.root / "vaults"
        args = ["--vault-root", str(root), "--project-id", "P-999", "--name", "测试", "--website", "https://example.com",
                "--owner", "运营", "--source-root", str(self.clean), "--confirm"]
        self.assertEqual(run("kb_project.py", *args).returncode, 0)
        vault = root / "P-999_测试知识库"
        result = run("publish_vault.py", "--out", str(self.out), "--vault", str(vault))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"withdrawn": 0', result.stdout)
        overview = (vault / "01_工作台/00_知识库总览.md").read_text(encoding="utf-8")
        for part in ("整理层", "来源层", "待处理", "还缺什么"):
            self.assertIn(part, overview)


if __name__ == "__main__":
    unittest.main()
