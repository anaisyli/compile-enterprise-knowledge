#!/usr/bin/env python3
"""A writer takes picked sections from the pages, slimmed, in zones, with the gaps sorted."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import kb_index  # noqa: E402
import kb_lookup  # noqa: E402

LINK = "[[P-002_测试/02_源资料/10_净化原文/产品画册/产品画册-YFD71F#第 1 页|〔1〕]]"
PRODUCT = f"""---
doc_id: "KNOW-line-YFD71F"
title: "产品系列-YFD71F"
layer: "整理层"
status: "有效"
aliases: ["PTFE lined butterfly valve", "衬氟蝶阀"]
---

# 产品系列-YFD71F

> 整理页：由 AI 按原文归纳。

## 一句话说明

YFD71F 是 PTFE 衬里蝶阀。{LINK}

![[P-002_测试/03_正式知识/90_素材/页面图/x.png|360]]

## 参数

### 基本技术数据

| 项目 | 画册数据 | 出处 |
| --- | --- | --- |
| Size range | DN50-DN300 | {LINK.replace('|', chr(92) + '|')} |

### 扭矩

| DN | Nm |
| --- | --- |
""" + "".join(f"| DN{50 + i} | {10 + i} |\n" for i in range(30)) + f"""
- 完整法兰孔位表 [[P-002_测试/02_源资料/10_净化原文/产品画册/产品画册-YFD71F#第 9 页|见原文〔2〕]]

## 需核实

- 温度上限有两种说法，需核实。{LINK}

## 来源

- [[产品画册-YFD71F]]（T2）
"""
EXTERNAL = """---
doc_id: "EXT-seat-materials"
title: "外部-阀座材料耐化学性"
status: "待抽检"
origin: "external"
review_due: "2027-05-01"
---

# 阀座材料耐化学性

## 技术知识

PTFE 阀座耐受大多数酸碱介质。[[EV-1#摘录 1|〔1〕]]
"""


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "P-002_测试"
        product = self.vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-YFD71F.md"
        product.parent.mkdir(parents=True)
        product.write_text(PRODUCT, encoding="utf-8")
        external = self.vault / "03_正式知识/20_外部公共知识/50_行业知识与洞察/外部-阀座材料耐化学性.md"
        external.parent.mkdir(parents=True)
        external.write_text(EXTERNAL, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_the_catalog_lists_pages_aliases_and_sections(self) -> None:
        text = kb_lookup.catalog(self.vault)
        self.assertIn("### 产品系列-YFD71F（产品系列-YFD71F；约", text)
        self.assertIn("别名：PTFE lined butterfly valve、衬氟蝶阀", text)
        self.assertIn("- 参数（", text)
        self.assertIn("基本技术数据（", text)
        self.assertNotIn("来源（", text)
        self.assertIn("## 外部公共知识", text)
        self.assertEqual(kb_lookup.write_catalog(self.vault), self.vault / kb_lookup.CATALOG)
        # A page a person wrote in its place is left alone.
        (self.vault / kb_lookup.CATALOG).write_text("我的目录", encoding="utf-8")
        self.assertIsNone(kb_lookup.write_catalog(self.vault))

    def test_a_pack_is_slim_and_keeps_its_zones(self) -> None:
        result = kb_lookup.pack(self.vault, ["衬氟蝶阀#参数", "产品系列-YFD71F#一句话说明"], [], None, False, False)
        text = result["text"]
        self.assertIn("## 一、客户专属事实", text)
        self.assertIn("YFD71F 是 PTFE 衬里蝶阀。", text)
        self.assertNotIn("[[", text)
        self.assertNotIn("90_素材", text)
        self.assertNotIn("整理页：", text)
        self.assertIn("| Size range | DN50-DN300 |", text)  # The emptied 出处 column is gone.
        self.assertNotIn("| 出处 |", text)
        self.assertIn("（表格共 30 行，已折叠，这里只列表头。要写表里的具体数值时，先补读整张表：", text)
        self.assertIn('--sections "产品系列-YFD71F#参数 > 扭矩" --full-tables --no-brief', text)
        self.assertIn("（见原文：产品画册-YFD71F#第 9 页）", text)
        self.assertIn("## 三、口径避坑", text)
        self.assertIn("温度上限有两种说法", text)
        full = kb_lookup.pack(self.vault, ["产品系列-YFD71F#参数 > 扭矩"], [], None, True, True)["text"]
        self.assertIn("| DN79 | 39 |", full)
        self.assertNotIn("基本技术数据", full)

    def test_a_budget_folds_and_drops_leads_but_never_facts(self) -> None:
        source = self.vault / "02_源资料/10_净化原文/产品画册/产品画册-YFD71F.md"
        source.parent.mkdir(parents=True)
        source.write_text('---\ndoc_id: "DOC-1"\ntitle: "产品画册-YFD71F"\nstatus: "有效"\n---\n\n## 第 3 页\n\n' + "PTFE seat note. " * 80,
                          encoding="utf-8")
        kb_index.build(self.vault)
        product = self.vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-YFD71F.md"
        text = product.read_text(encoding="utf-8").replace("### 扭矩", "### 选型\n\n| DN | Nm |\n| --- | --- |\n"
                                                             + "".join(f"| DN{i} | {i} |\n" for i in range(12)) + "\n### 扭矩")
        product.write_text(text, encoding="utf-8")
        spec = ["产品系列-YFD71F#参数", "产品系列-YFD71F#一句话说明"]
        full = kb_lookup.pack(self.vault, spec, ["PTFE seat note"], None, False, False)
        self.assertIn("| DN11 | 11 |", full["text"])
        self.assertIn("原文里搜到的", full["text"])
        tight = kb_lookup.pack(self.vault, spec, ["PTFE seat note"], None, False, False, budget=200)
        self.assertNotIn("| DN11 | 11 |", tight["text"])
        self.assertIn("（表格共 12 行，已折叠", tight["text"])
        self.assertNotIn("PTFE seat note. PTFE", tight["text"])
        self.assertIn("YFD71F 是 PTFE 衬里蝶阀。", tight["text"])
        self.assertIn("温度上限有两种说法", tight["text"])
        self.assertIn("一条没删", tight["text"])
        self.assertIn("超过 200 字", tight["text"])  # Still over: said so, nothing cut.

    def test_sources_can_be_kept_on_request(self) -> None:
        text = kb_lookup.pack(self.vault, ["产品系列-YFD71F#一句话说明"], [], None, True, False)["text"]
        self.assertIn("YFD71F 是 PTFE 衬里蝶阀。〔1〕", text)
        self.assertIn("〔1〕产品画册-YFD71F#第 1 页", text)

    def test_gaps_are_sorted_and_external_knowledge_is_reused(self) -> None:
        result = kb_lookup.pack(self.vault, [], ["PTFE 阀座 耐受 酸碱", "酸碱介质", "What certifications does the factory hold",
                                                 "Valve torque basics"], None, False, False)
        kinds = {g["gap"]: (g["kind"], bool(g["reused"])) for g in result["gaps"]}
        self.assertEqual(kinds["What certifications does the factory hold"], ("客户专属", False))
        self.assertEqual(kinds["Valve torque basics"], ("公共知识", False))
        self.assertTrue(kinds["PTFE 阀座 耐受 酸碱"][1])
        # Terms appearing only in the body (not the heading or aliases) still reuse external knowledge.
        self.assertTrue(kinds["酸碱介质"][1])
        self.assertIn("## 二、行业公共知识", result["text"])
        self.assertIn("待抽检；复查日期 2027-05-01", result["text"])
        self.assertIn("research.py plan --vault", result["text"])

    def test_unknown_names_are_reported(self) -> None:
        problems = kb_lookup.pack(self.vault, ["没有的页", "产品系列-YFD71F#没有的节"], [], None, False, False)["problems"]
        self.assertEqual(len(problems), 2)

    def test_the_index_ranks_instead_of_requiring_every_word(self) -> None:
        source = self.vault / "02_源资料/10_净化原文/产品画册/产品画册-YFD71F.md"
        source.parent.mkdir(parents=True)
        source.write_text('---\ndoc_id: "DOC-1"\ntitle: "产品画册-YFD71F"\nstatus: "有效"\n---\n\n## 第 1 页\n\nPTFE seat resists acids.\n',
                          encoding="utf-8")
        kb_index.build(self.vault)
        hits = kb_index.search(self.vault, "PTFE seat chemical resistance guide", 5, None, None, ["原文"])
        self.assertEqual([h["path"] for h in hits], ["02_源资料/10_净化原文/产品画册/产品画册-YFD71F.md"])
        layers = {h["layer"] for h in kb_index.search(self.vault, "PTFE", 10, None, None)}
        self.assertEqual(layers, {"客户知识", "外部公共知识", "原文"})
        chunk = kb_index.search(self.vault, "扭矩", 5, None, None, ["客户知识"])[0]
        self.assertEqual(chunk["section"], "参数 > 扭矩")


if __name__ == "__main__":
    unittest.main()
