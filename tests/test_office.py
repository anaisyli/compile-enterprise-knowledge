#!/usr/bin/env python3
"""Old Office files: a LibreOffice that cannot work here is found out quickly, not waited on file by file."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import office  # noqa: E402


class OfficeTests(unittest.TestCase):
    def setUp(self) -> None:
        importlib.reload(office)
        # A "LibreOffice" that exits at once and writes nothing, like one a sandbox forbids to write.
        self.saved = os.environ.get("LIBREOFFICE_PATH")
        os.environ["LIBREOFFICE_PATH"] = sys.executable
        office.RETRY_WAIT = 0
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self) -> None:
        if self.saved is None:
            os.environ.pop("LIBREOFFICE_PATH", None)
        else:
            os.environ["LIBREOFFICE_PATH"] = self.saved
        self.folder.cleanup()
        importlib.reload(office)

    def test_after_a_few_failures_the_rest_are_not_tried(self) -> None:
        source = self.root / "a.xls"
        source.write_bytes(b"old workbook")
        before = set(Path(tempfile.gettempdir()).glob("cek-lo-*"))
        for index in range(office.GIVE_UP_AFTER):
            with self.assertRaises(office.ConversionError):
                office.convert_office(source, "xlsx", self.root / "out", f"k{index}", 5)
        self.assertTrue(office.disabled())
        with self.assertRaises(LookupError):
            office.convert_office(source, "xlsx", self.root / "out", "k9", 5)
        # Each attempt's temporary profile and output folder are gone afterwards.
        self.assertLessEqual(set(Path(tempfile.gettempdir()).glob("cek-lo-*")), before)

    def test_the_self_test_reports_a_converter_that_produces_nothing(self) -> None:
        self.assertTrue(office.self_test(self.root / "check"))


if __name__ == "__main__":
    unittest.main()
