"""Dimension images are assets even when cached platform text exists."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from kb_common import preferred_page_units, suppress_drawing_dimensions


class DimensionPageTests(unittest.TestCase):
    def test_cached_platform_callouts_and_figures_are_excluded_but_table_and_body_remain(self):
        table = "| DN | L |\n| --- | --- |\n| 50 | 108 |"
        units = [
            {"locator": "page 1", "text": "NATIVE DUPLICATE"},
            {"locator": "page 1 ocr", "text": "Valve installation\n# L\nΦD\nDN≤50\n619.88\n" + table,
             "engine": "reducto"},
            {"locator": "page 1 figure 1", "text": "Four bolt holes", "figure_description": True},
            {"locator": "page 1 drawing", "text": "YFD71XA 的安装尺寸图", "dimension_drawing": True},
            {"locator": "page 2 ocr", "text": "619.88\nL", "engine": "mineru"},
        ]
        filtered = suppress_drawing_dimensions(preferred_page_units(units))
        by_locator = {u["locator"]: u["text"] for u in filtered}
        self.assertEqual(by_locator["page 1 ocr"], "Valve installation\n" + table)
        self.assertNotIn("page 1", by_locator)
        self.assertNotIn("page 1 figure 1", by_locator)
        self.assertEqual(by_locator["page 2 ocr"], "619.88\nL")
        self.assertIn("page 1 drawing", by_locator)

    def test_slide_and_mineru_without_engine_follow_the_same_rule_and_preserve_human_record(self):
        human = {"locator": "slide 2 review", "text": "Original human supplement 619.88",
                 "engine": "reviewed", "reviewed_supplement": True}
        units = [
            {"locator": "slide 2 ocr", "text": "Installation notes\n619.88\n□b×b"},
            {"locator": "slide 2 drawing", "text": "型号待确认 的安装尺寸图", "dimension_drawing": True},
            human,
        ]
        filtered = suppress_drawing_dimensions(units)
        self.assertEqual(filtered[0]["text"], "Installation notes")
        self.assertIn(human, filtered)

    def test_unreviewed_pages_are_unchanged(self):
        units = [{"locator": "page 1 ocr", "text": "619.88\nL"},
                 {"locator": "page 1 figure 1", "text": "Four holes", "figure_description": True}]
        self.assertEqual(suppress_drawing_dimensions(units), units)


if __name__ == "__main__":
    unittest.main()
