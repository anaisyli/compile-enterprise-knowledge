#!/usr/bin/env python3
"""After writing, the article's hard facts are looked up in its material pack."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_article  # noqa: E402

PACK = """# 写作资料包

## 一、客户专属事实（可以代表公司写）

### 产品系列-YFD71F · 参数 > 基本技术数据

| Size range | DN50-DN300 |
| Pressure range | PN 10-16/Class 150/JIS10K |
| Temperature | PTFE :-10°C to + 180°C |
| Design | API609 |
| Certificates | ISO 9001, CE, WRAS |

## 三、口径避坑（下面的说法有冲突或待核实）

- 保修期：官网 FAQ 写 12 months，Company Profile 写 24 months。

## 四、缺口

### 原文里搜到的（还没整理进知识页，用前核对）

- EPDM -20°C ~ 120°C
"""


class CheckTests(unittest.TestCase):
    def run_check(self, article: str, deny: list[str] | None = None) -> dict:
        return check_article.check(article, PACK, ["YFD"], deny or [], None)

    def test_facts_in_the_pack_pass_even_inside_ranges(self) -> None:
        result = self.run_check("YFD71F covers DN 100 to DN300, PN16 and Class 150, API 609. It runs from -10 ℃ to 180 °C. "
                                "Certified ISO 9001, CE and WRAS; flanges to JIS 10K.")
        self.assertEqual(result["missing"], [], result)
        self.assertGreaterEqual(result["in_pack"], 9)

    def test_invented_risky_and_uncurated_facts_are_named(self) -> None:
        result = self.run_check("YFD71XH is certified to API 607 and ATEX, founded in 2009. A 24-month warranty applies. "
                                "EPDM seats work up to 120 °C. Unlike Keystone valves.", deny=["Keystone"])
        missing = {i["text"] for i in result["missing"]}
        self.assertEqual(missing, {"YFD71XH", "API 607", "ATEX", "2009"})
        self.assertEqual([i["text"] for i in result["risky"]], ["24-month"])
        self.assertEqual([i["text"] for i in result["uncurated"]], ["120 °C"])
        self.assertEqual(result["competitors"], ["Keystone"])
        text = check_article.report(result)
        self.assertIn("## 资料包里没有", text)
        self.assertIn("口径避坑", text)
        self.assertIn("不挡交付", text)

    def test_a_clean_article_says_so(self) -> None:
        self.assertIn("没有发现问题", check_article.report(self.run_check("YFD71F, DN50, API609.")))

    def test_industry_rules_follow_the_vocabulary(self) -> None:
        apparel = check_article.load_rules({"vocabularies": ["apparel"]})
        display = check_article.load_rules({"vocabularies": ["display"]})
        text = "Made of 280 GSM fleece, 95% cotton, OEKO-TEX certified. A 55 inch 3840×2160 panel at 700 nits, IP65."
        found_apparel = {t for _, t in check_article.facts(text, [], apparel)}
        found_display = {t for _, t in check_article.facts(text, [], display)}
        self.assertTrue({"280 GSM", "OEKO-TEX", "95% cotton"} <= found_apparel, found_apparel)
        self.assertNotIn("700 nits", found_apparel)
        self.assertTrue({"3840×2160", "700 nits", "IP65", "55 inch"} <= found_display, found_display)
        # No industry named: every group is used.
        everything = {t for _, t in check_article.facts(text, [], check_article.load_rules({}))}
        self.assertTrue({"280 GSM", "700 nits"} <= everything)
        own = check_article.load_rules({"check": {"units": ["dtex"], "certificates": ["Woolmark"]}})
        self.assertEqual({t for _, t in check_article.facts("150 dtex yarn, Woolmark", [], own)}, {"150 dtex", "Woolmark"})

    def test_the_project_pages_teach_their_own_units_and_codes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            page = vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-X.md"
            page.parent.mkdir(parents=True)
            page.write_text("Body SS304, disc SS316, stem SS420. Yarn 30 tex, 40 tex, 50 tex. Since 2009 YIFA, 2010 YIFA, 2011 YIFA.",
                            encoding="utf-8")
            units, codes = check_article.harvest(vault, check_article.load_rules({}), {"own_terms": ["YIFA"]})
            self.assertEqual((units, codes), (["tex"], ["SS"]))
            rules = check_article.load_rules({"own_terms": ["YIFA"]}, vault)
            self.assertIn(("编号", "SS410"), check_article.facts("stem SS410", [], rules))

    def test_refetched_tables_count_as_part_of_the_pack(self) -> None:
        extra = "# 写作资料包\n\n## 一、客户专属事实（可以代表公司写）\n\n| DN80 | 45 Nm |\n"
        result = check_article.check("Torque at DN80 is 45 Nm.", [PACK, extra], ["YFD"], [], None)
        self.assertEqual(result["missing"], [])

    def test_a_docx_is_read_with_its_tables(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.docx"
            xml = ('<w:document><w:body><w:p><w:r><w:t>Size DN300</w:t></w:r></w:p>'
                   '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>API 607</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", xml)
            text = check_article.read_article(path)
        self.assertIn("API 607", text)
        self.assertEqual([i["text"] for i in self.run_check(text)["missing"]], ["API 607"])

    def test_industry_is_auto_detected_from_vault_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            page = vault / "03_正式知识/10_客户知识/20_产品介绍/产品系列-YFD.md"
            page.parent.mkdir(parents=True)
            # EN and IEC are in common (and also listed under display); they must not pull display into a valve vault.
            page.write_text("DN50-DN300, PN16, API 609, WRAS certified, EN 1092 flanges, IEC 60534 control.", encoding="utf-8")
            rules = check_article.load_rules({}, vault)
            found = {t for _, t in check_article.facts("DN100, 280 GSM, 700 nits, P2.", [], rules)}
            self.assertIn("DN100", found)
            self.assertNotIn("280 GSM", found)
            self.assertNotIn("700 nits", found)
            self.assertNotIn("P2", found)

    def test_chinese_pack_units_match_english_article_and_prose_is_not_flagged(self) -> None:
        cn_pack = ("# 写作资料包\n\n## 一、客户专属事实\n\n"
                   "- 口径 DN50 to DN300，最大起重 10 吨，阀体长度 2000 毫米，质保 12 个月，电流 10 A，公称 4\"。\n")
        article = ('Covers DN150 with 10 tons crane capacity, 2000 mm length, 12 months warranty, 10 A motor and 4" port. '
                   'Since the 2020s, 3 in 10 buyers check 5 a day for "150" rated parts.')
        result = check_article.check(article, cn_pack, [], [], None)
        missing = {i["text"] for i in result["missing"]}
        # 2020 is the year extracted from "2020s" (not "2020s" as seconds); "3 in", "5 a", '150"' are not flagged.
        self.assertEqual(missing, {"2020"}, result)

    def test_citations_verify_per_id_and_generate_human_trace(self) -> None:
        article = ("YFD71F covers DN50 to DN300 and API 609. Wrong rating PN40 here. "
                   "Our company PTFE seats resist acids up to 180 °C. Founded in 2009.")
        fact_index = {
            "K1": {"id": "K1", "text": "YFD71F size DN50-DN300, design API 609", "layer": "客户知识",
                   "page": "产品系列-YFD71F", "section": "参数 > 基本技术数据",
                   "sources": ["产品画册-YFD71F#第 1 页"], "risky": False},
            "K2": {"id": "K2", "text": "PTFE seats resist most acids up to 180 °C", "layer": "外部公共知识",
                   "page": "外部-阀座材料耐化学性", "section": "技术知识",
                   "sources": ["s1.example.org"], "risky": False},
        }
        citations_md = (
            "| 文章原文 | 引用 |\n| --- | --- |\n"
            "| YFD71F covers DN50 to DN300 and API 609 | K1 |\n"
            "| Wrong rating PN40 here | K1 |\n"
            "| Our company PTFE seats resist acids up to 180 °C | K2 |\n"
            "| Not in the article | K99 |\n"
        )
        res = check_article.check_citations(article, check_article.parse_citations(citations_md),
                                            fact_index, ["YFD"], ["YIFA"])
        self.assertEqual(res["failed_rows"], 2)
        self.assertEqual(res["warning_rows"], 1)
        self.assertTrue(res["rows"][0]["ok"])
        self.assertIn("PN40", " ".join(res["rows"][1]["issues"]))
        self.assertTrue(res["rows"][2]["warnings"])
        self.assertIn("文章里找不到这段原话", " ".join(res["rows"][3]["issues"]))
        self.assertIn("K99", " ".join(res["rows"][3]["issues"]))
        self.assertEqual([u["text"] for u in res["uncovered"]], ["2009"])
        trace = check_article.trace_report(res, "PTFE Valve Guide")
        self.assertIn("产品系列-YFD71F#参数 > 基本技术数据（客户知识）", trace)
        self.assertIn("产品画册-YFD71F#第 1 页", trace)
        self.assertIn("硬事实共", trace)
        self.assertIn("正文引用占比：", trace)
        self.assertGreater(res["cited_words"], 0)
        self.assertLess(res["cited_words"], res["total_words"])

    def test_body_word_citation_ratio_merges_overlaps_and_skips_failed_rows(self) -> None:
        article = "# Title\n\nAlpha beta gamma delta. Epsilon zeta eta theta iota kappa.\n"
        stats = check_article.body_word_citation_stats(
            article,
            ["Alpha beta gamma", "beta gamma delta", "Not in article"],
        )
        self.assertEqual(stats["total_words"], 10)
        self.assertEqual(stats["cited_words"], 4)
        self.assertEqual(stats["word_rate"], "40%")

    def test_project_learns_pure_letter_series_and_saves_to_vault_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            vault = Path(folder)
            (vault / "05_数据与审核/70_构建状态").mkdir(parents=True)
            page = vault / "03_正式知识/10_客户知识/20_产品介绍/产品大类-自助机.md"
            page.parent.mkdir(parents=True)
            page.write_text("# 产品大类-自助机\n\n## XMY 自助机\n\n### XMY 壁挂款\n\nXMY 支持壁挂安装。\n", encoding="utf-8")
            config = {"own_terms": ["Smartline"], "products": [{"name": "DS50系列", "aliases": ["DS50"]}]}
            rules = check_article.load_rules(config, vault)
            self.assertIn("XMY", rules["series"])
            self.assertIn("DS50", rules["series"])
            learned_file = vault / check_article.LEARNED_FILE
            self.assertTrue(learned_file.is_file())
            saved = learned_file.read_text(encoding="utf-8")
            self.assertIn("XMY", saved)
            found = {t for _, t in check_article.facts("Smartline XMY and DS50 support LED signage.", [], rules)}
            self.assertIn("XMY", found)
            self.assertIn("DS50", found)
            self.assertNotIn("LED", found)


if __name__ == "__main__":
    unittest.main()
