#!/usr/bin/env python3
"""Every source passage gets a place on the pages, and the checker can show it."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import curate  # noqa: E402
from kb_common import BUILD_DIRECTORY  # noqa: E402


def jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def bid(i: int) -> str:
    return f"{i:012x}"


TABLE = "| Size | Pressure |\n| --- | --- |\n" + "".join(f"| DN{50 + i} | PN16 |\n" for i in range(3))
LONG = "| Size | Torque |\n| --- | --- |\n" + "".join(f"| DN{50 + i} | {10 + i} Nm |\n" for i in range(30))


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.build = self.out / BUILD_DIRECTORY
        self.docs = [{"doc_id": "c1", "title": "公司介绍-简介", "tier": "T2", "module": "10_公司概述", "folder": "公司介绍",
                      "file_name": "公司介绍-简介.md"}]
        texts = [f"The company runs line {i} for valves with careful testing." for i in range(70)]
        texts[68] = "The company runs line 68 for DN100 valves with careful testing."
        texts += [TABLE, LONG, "- PTFE", "Related Posts", "Related Posts", "Related Posts",
                  "The company runs line 0 for valves with careful testing!"]
        self.blocks = [{"block_id": bid(i), "doc_id": "c1", "status": "kept", "text": t, "group": f"第 {i + 1} 页"}
                       for i, t in enumerate(texts)]
        jsonl(self.build / "docs.jsonl", self.docs)
        jsonl(self.build / "blocks.jsonl", self.blocks)
        (self.build / "statuses.json").write_text(json.dumps({"c1": "有效"}), encoding="utf-8")
        (self.build / "published.json").write_text(json.dumps({"c1": "公司介绍/公司介绍-简介.md"}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, **config) -> dict:
        (self.build / "kb-config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return curate.prepare(self.out, {"skip_topics": {}, **config})

    def folder(self) -> Path:
        topics = json.loads((self.build / "curation/topics.json").read_text(encoding="utf-8"))
        company = next(t for t in topics if t["module"] == "company" and t["home"])
        return self.build / "curation" / company["slug"]

    def write(self, page: str, ledger: list | None = None) -> None:
        folder = self.folder()
        (folder / "page.md").write_text(page, encoding="utf-8")
        if ledger is not None:
            jsonl(folder / "ledger.jsonl", ledger)

    def full_page(self, upto: int = 70) -> str:
        lines = [f"- 第 {i} 条产线。{{{{src:{bid(i)}}}}}" for i in range(upto)]
        return ("# 公司概况\n别名：YIFA、一阀\n\n## 一句话说明\n\n" + "\n".join(lines) + "\n\n## 公司介绍\n\n"
                f"压力等级表：{{{{src:{bid(70)}}}}}\n\n{{{{表:{bid(70)}}}}}\n\n"
                f"- 扭矩表很长，按口径列出。{{{{见原文:{bid(71)}}}}}\n- 阀座材料可选 PTFE。{{{{src:{bid(72)}}}}}\n")

    def test_every_home_passage_goes_to_the_writer_in_batches(self) -> None:
        report = self.prepare()
        company = next(t for t in report["topics"] if t["slug"] == self.folder().name)
        # 70 sentences, two tables and the short list item; the repeated header and the copy are settled.
        self.assertEqual(company["home"], 73)
        self.assertEqual(company["batches"], 2)
        self.assertTrue((self.folder() / "input-02.md").is_file())
        self.assertEqual(report["auto"], {"noise": 3, "repeats": 1})
        self.assertIn("还剩 73 段", (self.folder() / "input.md").read_text(encoding="utf-8"))

    def test_a_passage_without_a_place_fails_the_check_and_is_named(self) -> None:
        self.prepare()
        self.write(self.full_page(upto=69))
        result = curate.check(self.out)[self.folder().name]
        self.assertFalse(result["ok"])
        self.assertIn("还有 1 段原文没有去处", " ".join(result["errors"]))
        self.assertIn(bid(69), " ".join(result["errors"]))
        ledger = json.loads((self.build / curate.LEDGER_REPORT).read_text(encoding="utf-8"))
        self.assertEqual(ledger["total"]["未处理"], 1)
        self.assertEqual(ledger["total"]["指针"], 1)

    def test_warn_mode_only_reminds(self) -> None:
        self.prepare(curation={"ledger": "warn"})
        self.write(self.full_page(upto=69))
        result = curate.check(self.out)[self.folder().name]
        self.assertTrue(result["ok"], result["errors"])
        self.assertIn("还有 1 段原文没有去处", result["warnings"][0])

    def test_ledger_entries_are_held_to_their_rules(self) -> None:
        self.prepare()
        self.write(self.full_page(upto=67), [
            {"block": bid(67), "action": "重复", "same_as": bid(0)},
            {"block": bid(68), "action": "不需要写", "reason": "目录"},
            {"block": bid(69), "action": "重复", "same_as": bid(69)},
        ])
        errors = " ".join(curate.check(self.out)[self.folder().name]["errors"])
        self.assertIn("第 3 行：same_as 指的段", errors)
        # Line 68 names a size (DN100): it has to be written or pointed to.
        self.assertIn(f"段 {bid(68)} 含型号、数字", errors)
        self.write(self.full_page(upto=69), [{"block": bid(69), "action": "重复", "same_as": bid(0)}])
        self.assertTrue(curate.check(self.out)[self.folder().name]["ok"])

    def test_tables_come_from_the_source_and_long_ones_fold(self) -> None:
        self.prepare()
        page = self.full_page()
        self.write(page.replace(f"{{{{表:{bid(70)}}}}}", f"表：{{{{表:{bid(70)}}}}}"))
        self.assertIn("必须单独一行", " ".join(curate.check(self.out)[self.folder().name]["errors"]))
        self.write(page.replace(f"- 阀座材料可选 PTFE。{{{{src:{bid(72)}}}}}", f"{{{{表:{bid(72)}}}}}"))
        self.assertIn("里没有表格", " ".join(curate.check(self.out)[self.folder().name]["errors"]))
        self.write(page)
        built = curate.build_pages(self.out)
        self.assertFalse(built["failed"], built["failed"])
        text = next((self.build / curate.KNOWLEDGE).rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("| DN52 | PN16 |", text)
        self.assertIn("表格出处：[[公司介绍-简介#第 71 页|", text)
        self.assertIn("见原文〔", text)
        self.assertNotIn("别名：", text)
        self.assertIn('aliases: ["YIFA", "一阀"]', text)
        self.assertIn('uses: ["article", "crm", "agent"]', text)
        self.assertIn("未另外注明的内容都是客户资料自己的说法", text)
        self.assertEqual(curate.render_table(LONG, "L").count("|"), 0)
        self.assertIn("表格较长（30 行；列：Size / Torque）", curate.render_table(LONG, "L"))

    def test_a_skipped_topic_answers_for_nothing(self) -> None:
        self.prepare()
        self.write("# 公司概况\n\n## 一句话说明\n\n- 产线。{{src:" + bid(0) + "}}\n")
        slug = self.folder().name
        (self.build / "kb-config.json").write_text(json.dumps({"skip_topics": {slug: "测试"}}), encoding="utf-8")
        self.assertTrue(curate.check(self.out)[slug]["ok"])
        ledger = json.loads((self.build / curate.LEDGER_REPORT).read_text(encoding="utf-8"))
        self.assertEqual(ledger["total"]["决定不写"], 72)

    def test_the_brief_is_short_and_built_from_written_pages(self) -> None:
        self.prepare()
        self.write(self.full_page())
        report = self.prepare()
        brief = next(t for t in report["topics"] if t["slug"] == "brief")
        self.assertTrue(brief["summary"])
        self.assertEqual(brief["blocks"], 72)  # What the company page cites, at most BRIEF_MAX.
        folder = self.build / "curation/brief"
        (folder / "page.md").write_text("# 公司速查卡\n\n## 一句话说明\n\n" + f"- {'很长的说明' * 600}{{{{src:{bid(0)}}}}}\n", encoding="utf-8")
        self.assertIn("超过 2500 字", " ".join(curate.check(self.out)["brief"]["errors"]))

    def test_noise_is_only_what_repeats_or_says_nothing(self) -> None:
        blocks = [{"block_id": bid(i), "text": t} for i, t in enumerate(
            ["Share this article |", "Share this article |", "Share this article |", "- PTFE", "Heavy Solid Cast Body",
             "www.example.com", "DN50 PN16", "DN50 PN16", "DN50 PN16"])]
        noise, repeats = curate.auto_settled(blocks)
        self.assertEqual(noise, {bid(0), bid(1), bid(2), bid(5)})
        self.assertEqual(repeats, {bid(7): bid(6), bid(8): bid(6)})


if __name__ == "__main__":
    unittest.main()
