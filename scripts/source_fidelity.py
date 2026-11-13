"""Presentation-only protection and source-bound, human/Agent-reviewed page transcripts."""
from __future__ import annotations

import html
import re
from pathlib import Path

from kb_common import load_json, sha256_file


def literal_dimensions(text: str) -> str:
    """A multiplication asterisk between digits is source data, not Markdown emphasis."""
    return re.sub(r'(?<=\d)([ \t]*)\*(?=[ \t]*\d)', r'\1\\*', text)


def reviewed_units(build: Path, item_id: str, source_hash: str, units: list[dict]) -> list[dict]:
    path = build / 'reviewed-pages.json'
    records = load_json(path).get(item_id, []) if path.is_file() else []
    for record in records:
        if record['source_sha256'] != source_hash:
            raise ValueError(f'Stale reviewed transcript: {item_id}')
        if not record.get('reviewed_by') or not record.get('image_sha256') or not record.get('text', '').strip():
            raise ValueError(f'Incomplete page review: {item_id}')
        image = build / record.get('image_file', '')
        if not image.is_file() or sha256_file(image) != record['image_sha256']:
            raise ValueError(f'Missing or changed reviewed image: {item_id}')
        locator = record['locator']
        if not re.fullmatch(r'(?:page|slide|image) [1-9]\d*', locator):
            raise ValueError(f'Invalid reviewed locator: {locator}')
        # Raw text and MinerU results remain in their caches; the reviewed transcription
        # passes through the same content guard as any other input. No bypass at render time.
        units = [u for u in units if u['locator'] != locator and not u['locator'].startswith(locator + ' ')]
        units.append({'locator': locator, 'text': record['text'], 'engine': 'reviewed',
                      'reviewed_by': record['reviewed_by'], 'review_note': record.get('note', '')})
    return sorted(units, key=lambda u: (int(re.search(r'\d+', u['locator']).group()) if re.search(r'\d+', u['locator']) else 0, u['locator'])) if records else units


def merged_sheet_table(text: str, layout: dict) -> str:
    """Render only GUARDED cell text, with merge geometry read from the original workbook.

    If filtering changed rows/columns, fail closed to the cleaned Markdown table.
    HTML values are escaped; source-provided markup never becomes active HTML.
    """
    if not layout or not layout.get('merges'):
        return literal_dimensions(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != len(layout['rows']) + 1 or not all(s.startswith('|') and s.endswith('|') for s in lines):
        return literal_dimensions(text)
    rows = [[c.strip().replace('\\*', '*').replace('\\|', '|') for c in re.split(r'(?<!\\)\|', line)[1:-1]]
            for i, line in enumerate(lines) if i != 1]
    if any(len(row) != len(layout['columns']) for row in rows):
        return literal_dimensions(text)
    coordinates = {(r, c): rows[ri][ci] for ri, r in enumerate(layout['rows']) for ci, c in enumerate(layout['columns'])}
    spans, covered = {}, set()
    for merge in layout['merges']:
        a, b = merge.split(':')
        ca, ra = re.fullmatch(r'([A-Z]+)(\d+)', a).groups()
        cb, rb = re.fullmatch(r'([A-Z]+)(\d+)', b).groups()
        def col(s):
            n = 0
            for letter in s:
                n = n * 26 + ord(letter) - 64
            return n
        members = {(r, c) for r in range(int(ra), int(rb) + 1) for c in range(col(ca), col(cb) + 1)}
        anchor = (int(ra), col(ca))
        if not members <= coordinates.keys() or any(coordinates[m] for m in members - {anchor}):
            return literal_dimensions(text)
        spans[anchor] = (int(rb) - int(ra) + 1, col(cb) - col(ca) + 1)
        covered.update(members - {anchor})
    output = ['<table>']
    for ri, r in enumerate(layout['rows']):
        cells = []
        for c in layout['columns']:
            if (r, c) in covered:
                continue
            rs, cs = spans.get((r, c), (1, 1))
            attrs = (f' rowspan="{rs}"' if rs > 1 else '') + (f' colspan="{cs}"' if cs > 1 else '')
            tag = 'th' if ri < layout.get('header_rows', 1) else 'td'
            cells.append(f'<{tag}{attrs}>{html.escape(coordinates[r, c])}</{tag}>')
        output.append('<tr>' + ''.join(cells) + '</tr>')
    return '\n'.join(output + ['</table>'])
