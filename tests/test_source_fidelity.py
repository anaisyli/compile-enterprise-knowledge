from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_fidelity import literal_dimensions, merged_sheet_table, reviewed_units  # noqa: E402
from verify_kb import squeeze  # noqa: E402
from extract_blocks import configured_route  # noqa: E402


class SourceFidelityTests(unittest.TestCase):
    def test_literal_star_is_equal_to_markdown_escaped_star_during_source_check(self) -> None:
        self.assertEqual(squeeze(r'590*550*1750MM'), squeeze('590*550*1750MM'))
        self.assertNotEqual(squeeze('590*550*1750MM'), squeeze('5905501750MM'))

    def test_reviewed_transcript_requires_matching_source_and_image(self) -> None:
        record = {'source_sha256': 'source-hash', 'image_sha256': 'image-hash',
                  'reviewed_by': 'Agent', 'image_file': 'page.png',
                  'locator': 'page 2', 'text': '1.8GHz'}
        native = [{'locator': 'page 1', 'text': 'cover'}, {'locator': 'page 2 col 1', 'text': '18GHz .'}]
        with patch('source_fidelity.load_json', return_value={'sample': [record]}), \
             patch.object(Path, 'is_file', return_value=True), \
             patch('source_fidelity.sha256_file', return_value='image-hash'):
            result = reviewed_units(Path('build'), 'sample', 'source-hash', native)
            self.assertEqual([u['text'] for u in result], ['cover', '1.8GHz'])
            with self.assertRaisesRegex(ValueError, 'Stale reviewed transcript'):
                reviewed_units(Path('build'), 'sample', 'changed-source', native)
        with patch('source_fidelity.load_json', return_value={'sample': [record]}), \
             patch.object(Path, 'is_file', return_value=True), \
             patch('source_fidelity.sha256_file', return_value='changed-image'):
            with self.assertRaisesRegex(ValueError, 'Missing or changed reviewed image'):
                reviewed_units(Path('build'), 'sample', 'source-hash', native)

    def test_explicit_no_ocr_keeps_image_as_material_and_exclusions_win(self) -> None:
        asset = {"item_id": "image1", "decision": "A", "asset_kind": "image",
                 "display_name": "screen.png", "usable_as_fact": True}
        config = {"no_ocr_items": {"image1": "user says keep as material"}}
        self.assertEqual(configured_route(asset, config), {"route": "media", "reason": "media_kept"})
        config["excluded_items"] = {"image1": "do not process"}
        self.assertEqual(configured_route(asset, config), {"route": "skip", "reason": "user_excluded"})

    def test_dimension_asterisks_are_preserved_and_escaped_once(self) -> None:
        source = "590*550*1750MM and 1706.2 * 986.6 * 97.5"
        escaped = r"590\*550\*1750MM and 1706.2 \* 986.6 \* 97.5"
        self.assertEqual(literal_dimensions(source), escaped)
        self.assertEqual(literal_dimensions(escaped), escaped)

    def test_merged_sheet_headers_keep_the_original_geometry(self) -> None:
        source = "| 产品款式 |  |  | 尺寸 |  | 型号 |\n| --- | --- | --- | --- | --- | --- |\n|  |  |  | 屏幕 | 立柱 | RR-21.5XPZ01 |\n|  |  |  | 590*550*1750MM | / | RR-27XPZ01 |\n|  |  |  | 670*550*1850MM | / | RR-32XPZ01 |"
        layout = {
            "rows": [1, 2, 3, 4],
            "columns": [1, 2, 3, 4, 5, 6],
            "merges": ["A1:C1", "D1:E1", "A2:A4"],
            "header_rows": 3,
        }

        result = merged_sheet_table(source, layout)

        self.assertIn('<th colspan="3">产品款式</th>', result)
        self.assertIn('<th colspan="2">尺寸</th>', result)
        self.assertIn('<th rowspan="3">', result)
        self.assertIn("590*550*1750MM", result)
        self.assertNotIn(r"590\*550\*1750MM", result)


if __name__ == "__main__":
    unittest.main()
