#!/usr/bin/env python3
"""Lay a worksheet out the way a person reads it: the lines above the table, the table, the lines below.

A sheet like a quotation template opens with the company name, address and "Quotation", has the
real header (Item no. / Model no. / …) in the middle, and ends with remarks and terms. Put into one
Markdown table, the company name becomes the header and the real header a row in the middle.

Only the arrangement changes; every word stays:
- rows above the header row and below the last table row become plain lines;
- the header row is the first row with three or more filled cells, at least 60% as many as the fullest row,
  that more rows of two or more follow;
- a column empty below its header is left out of the table and named in a line under it;
- a cell merged down several rows (one category for eight models) is repeated on each row;
- line breaks inside a cell stay, as <br>;
- an Excel date number next to a "date" label is written as the date.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

DATE_LABEL = re.compile(r"(?i)date|日期|时间")
CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    return number


def excel_date(value: str) -> str | None:
    """46148 → 2026-05-05, for numbers in the range Excel uses for dates of these decades."""
    if not re.fullmatch(r"\d{5}(?:\.0+)?", value.strip()):
        return None
    serial = int(float(value))
    if not 30000 <= serial <= 60000:
        return None
    return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()


def fill_merges(rows: dict[int, dict[str, str]], merges: list[str], column_of) -> None:
    """A value merged down a column belongs to every row it covers: repeat it there."""
    for merge in merges:
        if ":" not in merge:
            continue
        start, end = merge.split(":", 1)
        first, last = CELL_REF.match(start), CELL_REF.match(end)
        if not first or not last or first.group(1) != last.group(1):
            continue  # Only merges down one column: a title merged across stays once.
        column, top, bottom = first.group(1), int(first.group(2)), int(last.group(2))
        value = rows.get(top, {}).get(column)
        if not value:
            continue
        for number in range(top + 1, bottom + 1):
            if not rows.get(number, {}).get(column):
                rows.setdefault(number, {})[column] = value


def lay_out(rows: dict[int, dict[str, str]], merges: list[str] | None = None) -> dict[str, Any] | None:
    """{"above": [...], "header": [...], "body": [[...]], "empty": [...], "below": [...]}, or None when the
    sheet has no header row to speak of; then it is shown as the plain table it is."""
    numbers = sorted(n for n in rows if any(v.strip() for v in rows[n].values()))
    if not numbers:
        return None
    filled = {n: sum(1 for v in rows[n].values() if v.strip()) for n in numbers}
    # The header is about as wide as the table: a "Company Name: … Quoted date …" line above it is not.
    widest = max(filled.values())
    header = next((n for i, n in enumerate(numbers) if filled[n] >= max(3, 0.6 * widest)
                   and sum(1 for m in numbers[i + 1:] if filled[m] >= 2) >= 1), None)
    if header is None or header == numbers[0] and all(filled[n] >= 2 for n in numbers):
        return None  # A plain list from the first row: nothing to rearrange.
    fill_merges(rows, merges or [], column_number)
    last = max(n for n in numbers if n >= header and filled[n] >= 2)
    body_numbers = [n for n in numbers if header < n <= last]
    columns = sorted({c for n in [header, *body_numbers] for c in rows[n] if rows[n][c].strip()}, key=column_number)
    empty = [c for c in columns if not any(rows[n].get(c, "").strip() for n in body_numbers)]
    kept = [c for c in columns if c not in empty]

    def cell(value: str) -> str:
        return "<br>".join(part.strip() for part in value.replace("\r\n", "\n").split("\n") if part.strip())

    def line(number: int) -> str:
        cells = [rows[number][c].strip() for c in sorted(rows[number], key=column_number) if rows[number][c].strip()]
        out = []
        for index, value in enumerate(cells):
            as_date = excel_date(value) if index and DATE_LABEL.search(cells[index - 1]) else None
            out.append(as_date or value.replace("\n", " "))
        return "  ".join(out)

    return {
        "above": [line(n) for n in numbers if n < header],
        "header": [cell(rows[header].get(c, "")) for c in kept],
        "body": [[cell(rows[n].get(c, "")) for c in kept] for n in body_numbers],
        "empty": [" ".join(rows[header].get(c, "").split()) for c in empty if rows[header].get(c, "").strip()],
        "below": [line(n) for n in numbers if n > last],
    }
