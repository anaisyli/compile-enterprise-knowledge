#!/usr/bin/env python3
"""External research works on the vault alone, keeps to public knowledge, and survives publishing."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import kb_lookup  # noqa: E402
import research  # noqa: E402
from kb_common import VAULT_LOCK, vault_lock  # noqa: E402
from test_extensions import Base, run  # noqa: E402

COVERAGE = """# 知识库覆盖与缺口

## 10_公司概述

| 小板块 | 原文 | 来源 | 状态 | 怎么补 |
| --- | ---: | ---: | --- | --- |
| 公司能力 | 5 段 | 1 份 | 薄弱 | 向客户要产能、员工、厂房面积 |

## 50_行业知识与洞察

| 小板块 | 原文 | 来源 | 状态 | 怎么补 |
| --- | ---: | ---: | --- | --- |
| 技术知识 | 0 段 | 0 份 | 缺 | 补技术资料 |
| 市场趋势 | 40 段 | 5 份 | 有 |  |

## 整理覆盖账本

| 主题 | 写进 | 未处理 |
| --- | ---: | ---: |
| 公司概况（`company`） | 3 | 1 |
"""


def source(sid: str, publisher_type: str = "standard", **extra) -> dict:
    return {"id": sid, "url": f"https://{sid.lower()}.example.org/seat", "title": f"Seat guide {sid}", "publisher": f"Body {sid}",
            "publisher_type": publisher_type, "fetched_at": "2026-11-20",
            "excerpts": ["PTFE seats resist most acids and alkalis.", "PTFE seats work from -20 °C to 180 °C."], **extra}


def answer(**page) -> dict:
    markdown = ("## 一句话说明\n\n- PTFE 阀座耐受大多数酸碱。{{ext:S1-1}}\n\n## 技术知识\n\n"
                "- PTFE 阀座的工作温度可到 180 °C。{{ext:S1-2}}{{ext:S2-2}}\n")
    return {"gap_key": "GAP-industry-技术知识", "status": "done", "searches": ["ptfe seat chemical resistance"],
            "sources": [source("S1"), source("S2", "association")],
            "page": {"title": "阀座材料的耐化学性", "module": "industry", "aliases": ["seat material chemical resistance"],
                     "markdown": markdown, **page}}


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "P-002_测试"
        (self.vault / "01_工作台").mkdir(parents=True)
        coverage = self.vault / research.COVERAGE
        coverage.parent.mkdir(parents=True)
        coverage.write_text(COVERAGE, encoding="utf-8")
        config = self.vault / "05_数据与审核/70_构建状态/kb-config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"own_terms": ["YIFA", "一阀"], "own_domains": ["yifavalve.com"], "deny_terms": ["RivalCo"]}),
                          encoding="utf-8")
        self.rules = kb_lookup.load_rules(self.vault)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def answer_file(self, *answers: dict) -> None:
        path = self.vault / research.STATE / "answer-1.jsonl"
        path.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in answers), encoding="utf-8")

    def planned(self) -> None:
        research.plan_from_gaps(self.vault, self.rules)
        research.approve(self.vault, self.rules, [], everything=True)

    def test_gaps_plan_keeps_public_knowledge_and_waits_for_a_tick(self) -> None:
        result = research.plan_from_gaps(self.vault, self.rules)
        self.assertEqual((result["public"], result["client_only"]), (1, 1))
        page = (self.vault / research.STATE / "plan.md").read_text(encoding="utf-8")
        self.assertIn("| GAP-industry-技术知识 | 50_行业知识与洞察 | 技术知识 | 缺 |", page)
        self.assertIn("| 10_公司概述 |  | 公司能力 | 薄弱 | 向客户要产能", page)
        self.assertNotIn("市场趋势", page)
        self.assertEqual(research.approve(self.vault, self.rules, [], False)["approved"], [])
        (self.vault / research.STATE / "plan.md").write_text(
            page.replace("| 技术知识（50_行业知识与洞察） |  |", "| 蝶阀阀座材料的耐化学性 | 是 |"), encoding="utf-8")
        self.assertEqual(research.approve(self.vault, self.rules, [], False)["approved"], ["GAP-industry-技术知识"])
        todo = research.todo_of(self.vault)
        self.assertEqual(todo[0]["question"], "蝶阀阀座材料的耐化学性")
        self.assertEqual(todo[0]["budget"], {"searches": 3, "pages": 5})
        self.assertIn("不查客户自己的事", (self.vault / research.STATE / "README.md").read_text(encoding="utf-8"))

    def test_outline_gaps_are_sorted_capped_and_reused(self) -> None:
        result = research.plan_from_outline(self.vault, self.rules, [
            "What certifications does the factory hold", "PTFE seat chemical resistance", "Valve torque basics",
            "Flange standards explained"], "How to choose")
        self.assertEqual(result["client_only"], ["What certifications does the factory hold"])
        self.assertEqual([t["section"] for t in result["todo"]], ["PTFE seat chemical resistance", "Valve torque basics"])
        self.assertEqual(result["over_budget"], ["Flange standards explained"])
        again = research.plan_from_outline(self.vault, self.rules, ["PTFE seat chemical resistance"], None)
        self.assertEqual(again["already"][0]["status"], "todo")

    def test_a_good_answer_becomes_evidence_and_a_page_writers_can_take(self) -> None:
        self.planned()
        self.answer_file(answer())
        result = research.collect(self.vault, self.rules)
        self.assertEqual(result["failed"], {})
        page = self.vault / result["done"][0]["page"]
        self.assertEqual(page.parent.name, "50_行业知识与洞察")
        text = page.read_text(encoding="utf-8")
        self.assertIn('status: "待抽检"', text)
        self.assertIn("#摘录 2|〔2〕]]", text)
        self.assertIn("https://s1.example.org/seat", text)
        evidence = list((self.vault / research.EVIDENCE).rglob("*.md"))
        self.assertEqual(len(evidence), 2)
        self.assertIn("## 摘录 2", evidence[0].read_text(encoding="utf-8"))
        self.assertEqual(research.state_of(self.vault)["gaps"]["GAP-industry-技术知识"]["status"], "done")
        self.assertEqual(research.todo_of(self.vault), [])
        self.assertIn("外部-阀座材料的耐化学性", (self.vault / kb_lookup.CATALOG).read_text(encoding="utf-8"))
        pack = kb_lookup.pack(self.vault, [], ["PTFE seat chemical resistance"], None, False, False)
        self.assertTrue(pack["gaps"][0]["reused"])
        self.assertIn("## 二、行业公共知识", pack["text"])
        self.assertEqual(research.status(self.vault)["to_spot_check"][0]["status"], "待抽检")
        research.mark(self.vault, page.stem, "有效")
        self.assertIn('status: "有效"', page.read_text(encoding="utf-8"))
        # The same outline section later is reused, not researched again.
        self.assertTrue(research.plan_from_outline(self.vault, self.rules, ["PTFE seat chemical resistance"], None)["reused"])
        # Invalidating the page updates state.json (so the gap can be researched again) and drops it from the index.
        research.mark(self.vault, page.stem, "已失效")
        self.assertEqual(research.state_of(self.vault)["gaps"]["GAP-industry-技术知识"]["status"], "invalidated")
        self.assertEqual(research.plan_from_gaps(self.vault, self.rules)["public"], 1)
        import kb_index
        self.assertEqual(kb_index.search(self.vault, "PTFE", 5, None, None, ["外部公共知识"]), [])

    def test_multi_number_lines_check_each_number_across_excerpts(self) -> None:
        self.planned()
        multi = answer(markdown="## 一句话说明\n\n- API 609 蝶阀 PTFE 阀座可到 180 °C。{{ext:S1-1}}{{ext:S1-2}}{{ext:S2-1}}{{ext:S2-2}}\n")
        multi["sources"] = [
            source("S1", excerpts=["API 609 covers butterfly valves.", "PTFE seats work up to 180 °C."]),
            source("S2", "association", excerpts=["Standard API 609 applies to industrial butterfly valves.", "Max PTFE temperature is 180 °C."]),
        ]
        self.answer_file(multi)
        self.assertEqual(research.collect(self.vault, self.rules)["failed"], {})

    def test_bad_answers_are_refused_with_reasons_and_kept_for_fixing(self) -> None:
        self.planned()
        one_source = answer(markdown="## 一句话说明\n\n- PTFE 阀座可到 180 °C。{{ext:S1-2}}\n")
        self.answer_file(one_source)
        errors = " ".join(research.collect(self.vault, self.rules)["failed"]["GAP-industry-技术知识"])
        self.assertIn("数字只有 1 个来源支撑", errors)
        self.assertTrue((self.vault / research.STATE / "answer-1.jsonl").is_file())
        bad = answer(markdown="## 一句话说明\n\n- YIFA 的阀座耐酸碱。{{ext:S1-1}}\n- 没有出处的一句。\n")
        bad["sources"] = [source("S1", "manufacturer"), source("S2", excerpts=["RivalCo seats are best."]),
                          source("S3", url="https://www.yifavalve.com/x")]
        bad["searches"] = ["a", "b", "c", "d"]
        self.answer_file(bad)
        errors = " ".join(research.collect(self.vault, self.rules)["failed"]["GAP-industry-技术知识"])
        for reason in ("超过上限 3 次", "manufacturer 类来源不能用", "竞品或第三方名称 rivalco", "客户自己的网站", "写到了客户自己", "没有标出处"):
            self.assertIn(reason, errors)
        self.assertFalse((self.vault / research.PAGES).exists())

    def test_a_lock_from_another_computer_is_honoured_until_it_is_stale(self) -> None:
        lock = self.vault / VAULT_LOCK
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"1\tother-host\t{datetime.now().isoformat(timespec='seconds')}\tpublish_vault.py\n", encoding="utf-8")
        self.assertFalse(vault_lock(self.vault, "research.py collect"))
        lock.write_text("1\tother-host\t2020-01-01T00:00:00\tpublish_vault.py\n", encoding="utf-8")
        self.assertTrue(vault_lock(self.vault, "research.py collect"))
        self.assertEqual(lock.read_text(encoding="utf-8").split("\t")[:2], [str(os.getpid()), socket.gethostname()])
        lock.unlink()


class PublishKeepsResearchTests(Base):
    def test_publishing_again_leaves_research_alone(self) -> None:
        self.clear_catalog_pending()
        self.edit_config(project_id="P-999")
        (self.text / "a1.json").write_text(json.dumps({"item_id": "a1", "units": [
            {"locator": "page 1", "text": "SHERPA SERIES\nNTX S-001 SHERPA JACKET"}]}), encoding="utf-8")
        self.assertEqual(self.pipeline().returncode, 0)
        args = ["--vault-root", str(self.root / "vaults"), "--project-id", "P-999", "--name", "测试", "--website",
                "https://example.com", "--owner", "运营", "--source-root", str(self.package), "--confirm"]
        self.assertEqual(run("kb_project.py", *args).returncode, 0)
        vault = self.root / "vaults" / "P-999_测试知识库"
        self.assertEqual(run("publish_vault.py", "--out", str(self.out), "--vault", str(vault)).returncode, 0)
        rules = kb_lookup.load_rules(vault)
        research.plan_from_outline(vault, rules, ["Fleece fabric care"], None)
        key = research.todo_of(vault)[0]["gap_key"]
        good = answer()
        good["gap_key"] = key
        (vault / research.STATE / "answer-1.jsonl").write_text(json.dumps(good, ensure_ascii=False) + "\n", encoding="utf-8")
        page = vault / research.collect(vault, rules)["done"][0]["page"]
        result = run("publish_vault.py", "--out", str(self.out), "--vault", str(vault))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(page.is_file())
        self.assertEqual(len(list((vault / research.EVIDENCE).rglob("*.md"))), 2)
        self.assertIn(page.stem, (vault / kb_lookup.CATALOG).read_text(encoding="utf-8"))
        self.assertIn("外部公共知识：1 页", (vault / "01_工作台/00_知识库总览.md").read_text(encoding="utf-8"))
        self.assertFalse((vault / VAULT_LOCK).exists())


if __name__ == "__main__":
    unittest.main()
