"""Tests for standalone (--vault --monthly) and batch (--vault-root) monthly audits in kb_status.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_vault(root: Path, name: str, *, pid: str, company: str, owner: str = "小王",
               unhandled: int = 0, broken_link: bool = False, with_index: bool = True,
               website_date: str | None = "2026-11-10", articles: list[str] | None = None,
               cus_pending: bool = False, ext_spot_check: bool = False) -> Path:
    vault = root / name
    write(vault / "01_工作台/10_项目基础信息.md",
          f"# 项目基础信息\n\n- 项目ID：{pid}\n- 项目名称：{company}\n- 内容运营负责人：{owner}\n")
    write(vault / "01_工作台/00_知识库总览.md", "# 知识库总览\n")
    write(vault / "01_工作台/写作目录.md", "# 写作目录\n")
    write(vault / "03_正式知识/10_客户知识/10_公司概述/公司概况.md",
          "# 公司概况\n\n正常正文。" + ("\n[坏链](file:///Z:/not-exist-xyz/file.pdf)\n" if broken_link else "\n"))
    write(vault / "02_源资料/10_净化原文/画册/产品画册.md", "# 产品画册\n")
    if with_index:
        write(vault / "02_源资料/knowledge-index.sqlite", "placeholder")
    if website_date:
        write(vault / "02_源资料/30_官网快照/units/home.json",
              json.dumps({"url": "https://example.com", "fetched_at": f"{website_date}T08:00:00"}, ensure_ascii=False))
    write(vault / "05_数据与审核/20_知识库覆盖与缺口/01_知识库覆盖与缺口.md",
          f"# 知识库覆盖与缺口\n\n小板块状态：有 5，缺 1。\n\n"
          f"## 整理覆盖账本\n\n可用原文 20 段要在整理页里有去处：写进 {20 - unhandled}"
          + (f"，未处理 {unhandled}" if unhandled else "") + "。\n")
    if cus_pending:
        write(vault / "05_数据与审核/30_异常与待决定/10_待客户补充/01_待客户补充事项.md",
              f"# 待客户补充事项\n\n| CUS ID | 问题 | 当前阶段 |\n| --- | --- | --- |\n| {pid}-CUS-001 | 确认交期 | 待处理 |\n")
    if ext_spot_check:
        write(vault / "03_正式知识/20_外部公共知识/50_行业知识与洞察/PTFE耐腐蚀原理.md",
              '---\norigin: external\ntitle: "PTFE耐腐蚀原理"\nstatus: "待抽检"\nreview_due: "2026-01-01"\n---\n\n正文。\n')
    if articles is not None:
        rows = "\n".join(articles)
        write(vault / "04_文章任务/00_文章台账.md",
              "# 文章写作台账\n\n| 任务编号 | 日期 | 标题 | 写作 Skill | 状态 | 引用覆盖率 | 记录目录 |\n"
              "| --- | --- | --- | --- | --- | --- | --- |\n" + rows + "\n")
    return vault


class MonthlyAuditTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "kb_status.py"), *args],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )

    def test_standalone_vault_monthly_without_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = make_vault(
                root, "P-001_示例知识库", pid="P-001", company="示例科技",
                website_date="2026-09-01", cus_pending=True, ext_spot_check=True,
                articles=[
                    "| 20261110-valve | 2026-11-10 | Valve Guide | seo-writer | 已交付 | 硬事实 8/10（80%）；正文 300/1000词（30%） | - |",
                    "| 20261118-pump | 2026-11-18 | Pump Guide | seo-writer | 已核对 | 硬事实 10/10（100%）；正文 400/1000词（40%） | - |",
                    "| 20261005-old | 2026-10-05 | Old Post | seo-writer | 已交付 | 硬事实 5/5（100%） | - |",
                ],
            )
            res = self.run_cli("--vault", str(vault), "--monthly", "--month", "2026-11")
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["articles"]["total"], 3)
            self.assertEqual(data["articles"]["month_count"], 2)
            self.assertEqual(data["articles"]["avg_fact_rate"], 90.0)
            self.assertEqual(data["articles"]["avg_word_rate"], 35.0)
            self.assertEqual(data["ledger_counts"]["CUS"]["pending"], 1)
            self.assertTrue(data["website_stale"])
            report_file = vault / "05_数据与审核/40_月度与交接/知识库体检_2026-11.md"
            self.assertTrue(report_file.is_file())
            text = report_file.read_text(encoding="utf-8")
            self.assertIn("本月（2026-11）产出 2 篇", text)
            self.assertIn("90.0%", text)
            self.assertIn("PTFE耐腐蚀原理", text)
            self.assertIn("2026-09-01", text)

    def test_batch_vault_root_monthly_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_vault(root, "P-001_正常项目知识库", pid="P-001", company="正常科技", website_date="2026-11-20")
            make_vault(root, "P-002_红灯项目知识库", pid="P-002", company="红灯工业",
                       unhandled=3, broken_link=True, with_index=False, website_date="2026-08-01")
            # Non-KB folder and hidden folder should be skipped cleanly without failing.
            (root / "其他非知识库文件夹").mkdir()
            (root / ".obsidian").mkdir()

            res = self.run_cli("--vault-root", str(root), "--month", "2026-11")
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["checked"], 2)
            self.assertEqual(data["green"], 1)
            self.assertEqual(data["red"], 1)
            self.assertEqual(data["skipped"], ["其他非知识库文件夹"])
            self.assertEqual(data["errors"], [])

            summary_file = root / "月度体检总览_2026-11.md"
            self.assertTrue(summary_file.is_file())
            summary_text = summary_file.read_text(encoding="utf-8")
            self.assertIn("P-001_正常项目知识库", summary_text)
            self.assertIn("P-002_红灯项目知识库", summary_text)
            self.assertIn("整理未处理段 3", summary_text)
            self.assertIn("缺少检索索引", summary_text)
            self.assertIn("其他非知识库文件夹", summary_text)
            self.assertTrue((root / "P-001_正常项目知识库/05_数据与审核/40_月度与交接/知识库体检_2026-11.md").is_file())
            self.assertTrue((root / "P-002_红灯项目知识库/05_数据与审核/40_月度与交接/知识库体检_2026-11.md").is_file())

    def test_batch_vault_root_with_project_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_vault(root, "P-001_洛阳一阀知识库", pid="P-001", company="洛阳一阀")
            make_vault(root, "P-002_恒塔知识库", pid="P-002", company="恒塔")
            make_vault(root, "P-003_未选中知识库", pid="P-003", company="未选中")

            res = self.run_cli("--vault-root", str(root), "--projects", "P-001,恒塔,P-999", "--month", "2026-11")
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["checked"], 2)
            self.assertEqual([p["vault_name"] for p in data["projects"]], ["P-001_洛阳一阀知识库", "P-002_恒塔知识库"])
            self.assertEqual(data["unmatched"], ["P-999"])
            partial_summary = root / "月度体检总览_2026-11_部分项目.md"
            self.assertTrue(partial_summary.is_file())
            self.assertFalse((root / "月度体检总览_2026-11.md").exists())
            self.assertFalse((root / "P-003_未选中知识库/05_数据与审核/40_月度与交接/知识库体检_2026-11.md").exists())
            self.assertIn("P-999", partial_summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
