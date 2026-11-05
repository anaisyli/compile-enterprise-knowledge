#!/usr/bin/env python3
"""One set of ledgers: another tool's empty templates give way, its filled ledgers do not."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_extensions import Base, run  # noqa: E402


LEDGER = "05_数据与审核/30_异常与待决定/10_待客户补充/01_待客户补充事项.md"
BESIDE = "05_数据与审核/30_异常与待决定/10_待客户补充/02_知识库构建_待客户补充事项.md"
TEMPLATE = "# 待客户补充事项\n\n## 当前事项\n\n| CUS ID | 想确认什么 |\n|---|---|\n"


class LedgerNameTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.vault = self.root / "vaults" / "P-999_测试知识库"
        args = ["--vault-root", str(self.root / "vaults"), "--project-id", "P-999", "--name", "测试", "--website",
                "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm"]
        run("kb_project.py", *args)
        self.pipeline()

    def publish(self):
        return run("publish_vault.py", "--out", str(self.out), "--vault", str(self.vault))

    def test_an_empty_template_from_another_tool_gives_way(self) -> None:
        (self.vault / LEDGER).parent.mkdir(parents=True, exist_ok=True)
        (self.vault / LEDGER).write_text(TEMPLATE, encoding="utf-8")
        self.assertEqual(self.publish().returncode, 0)
        self.assertIn("P-000-CUS-", (self.vault / LEDGER).read_text(encoding="utf-8"))
        self.assertFalse((self.vault / BESIDE).exists())

    def test_a_ledger_written_beside_a_template_moves_back_with_its_notes(self) -> None:
        (self.vault / LEDGER).parent.mkdir(parents=True, exist_ok=True)
        (self.vault / LEDGER).write_text(TEMPLATE + "| CUS-001 | 别人的事项 |\n", encoding="utf-8")
        self.assertEqual(self.publish().returncode, 0)
        self.assertTrue((self.vault / BESIDE).exists())  # A filled ledger of another tool is kept as it is.
        (self.vault / LEDGER).unlink()
        beside = self.vault / BESIDE
        beside.write_text(beside.read_text(encoding="utf-8") + "\n#### 处理记录\n\n- 人工备注\n", encoding="utf-8")
        self.pipeline()
        self.assertEqual(self.publish().returncode, 0)
        self.assertFalse(beside.exists())
        self.assertIn("人工备注", (self.vault / LEDGER).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
