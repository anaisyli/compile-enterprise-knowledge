"""Presentation-only protection and source-bound, human/Agent-reviewed page transcripts."""
from __future__ import annotations

import html
import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from kb_common import BUILD_DIRECTORY, load_json, sha256_file, write_json


EXCLUDE_KINDS = {'no_source_text', 'product_label', 'demonstration_content'}


def validate_review(build: Path, item_id: str, source_hash: str, record: dict) -> Path:
    if record.get('source_sha256') != source_hash:
        raise ValueError(f'Stale reviewed transcript: {item_id}')
    excluded = record.get('exclude_kind')
    text = record.get('text', '')
    if not isinstance(text, str) or not record.get('reviewed_by') or not record.get('image_sha256'):
        raise ValueError(f'Incomplete page review: {item_id}')
    if excluded:
        if excluded not in EXCLUDE_KINDS or text.strip() or not record.get('excluded_reason', '').strip():
            raise ValueError(f'Invalid reviewed exclusion: {item_id}')
    elif not text.strip():
        raise ValueError(f'Incomplete page review: {item_id}')
    if not re.fullmatch(r'(?:page|slide|image) [1-9]\d*', record.get('locator', '')):
        raise ValueError(f"Invalid reviewed locator: {record.get('locator')}")
    if not record.get('image_file'):
        raise ValueError(f'Missing reviewed image: {item_id}')
    image = build / record['image_file']
    if not image.is_file() or sha256_file(image) != record['image_sha256']:
        raise ValueError(f'Missing or changed reviewed image: {item_id}')
    return image


def literal_dimensions(text: str) -> str:
    """A multiplication asterisk between digits is source data, not Markdown emphasis."""
    return re.sub(r'(?<=\d)([ \t]*)\*(?=[ \t]*\d)', r'\1\\*', text)


def reviewed_units(build: Path, item_id: str, source_hash: str, units: list[dict]) -> list[dict]:
    path = build / 'reviewed-pages.json'
    records = load_json(path).get(item_id, []) if path.is_file() else []
    for record in records:
        validate_review(build, item_id, source_hash, record)
        locator = record['locator']
        platform_page = any(u['locator'] == locator + ' ocr'
                            and u.get('engine') in {None, '', 'mineru', 'reducto'}
                            and u['text'].strip() for u in units)
        if platform_page and not record.get('exclude_kind'):
            # A previous full-page human transcript is evidence to retain, not
            # permission to replace the platform page selected for this build.
            units.append({'locator': locator + ' review', 'text': record['text'], 'engine': 'reviewed',
                          'reviewed_by': record['reviewed_by'], 'review_note': record.get('note', ''),
                          'reviewed_supplement': True})
            continue
        # Raw text and MinerU results remain in their caches; the reviewed transcription
        # passes through the same content guard as any other input. No bypass at render time.
        units = [u for u in units if u['locator'] != locator and not u['locator'].startswith(locator + ' ')]
        units.append({'locator': locator, 'text': record['text'], 'engine': 'reviewed',
                      'reviewed_by': record['reviewed_by'], 'review_note': record.get('note', ''),
                      'review_exclusion': record.get('exclude_kind', ''),
                      'review_excluded_reason': record.get('excluded_reason', '')})
    return sorted(units, key=lambda u: (int(re.search(r'\d+', u['locator']).group()) if re.search(r'\d+', u['locator']) else 0, u['locator'])) if records else units


def collect_reviews(out: Path, patches: list[Path], *, apply: bool = False,
                    replace_reviewed: bool = False) -> dict:
    """Validate all evidence before collecting page replacements; never manufacture text.

    An empty transcript is accepted only as an explicit, reasoned exclusion of a
    photo, product label or demonstration screen. Its original image remains.
    """
    out = out.resolve()
    build = out / BUILD_DIRECTORY
    routes = load_json(build / 'routes.json')
    target = build / 'reviewed-pages.json'
    existing = load_json(target) if target.is_file() else {}
    incoming: dict[tuple[str, str], tuple[dict, Path]] = {}
    source_checks: dict[str, str] = {}
    for patch in patches:
        for item_id, records in load_json(patch).items():
            if not re.fullmatch(r'[A-Za-z0-9_-]+', item_id) or item_id not in routes:
                raise ValueError(f'Unknown reviewed item: {item_id}')
            route = routes[item_id]
            if item_id not in source_checks:
                package = Path(route['package']).resolve()
                source = (package / route['path']).resolve()
                if not source.is_relative_to(package) or not source.is_file():
                    raise ValueError(f'Missing reviewed source: {item_id}')
                source_checks[item_id] = sha256_file(source)
                if source_checks[item_id] != route['source_hash']:
                    raise ValueError(f'Changed reviewed source: {item_id}')
            for record in records:
                image = validate_review(build, item_id, source_checks[item_id], record).resolve()
                if not image.is_relative_to(out):
                    raise ValueError(f'Review image must be inside run directory: {item_id}')
                normalized = {k: record[k] for k in ('locator', 'text', 'reviewed_by', 'note',
                              'source_sha256', 'image_sha256', 'exclude_kind', 'excluded_reason') if k in record}
                normalized['image_file'] = (Path('reviewed-page-images') / item_id /
                    (record['image_sha256'] + image.suffix.lower())).as_posix()
                key = (item_id, record['locator'])
                if key in incoming and incoming[key][0] != normalized:
                    raise ValueError(f'Conflicting review patches: {key}')
                incoming[key] = (normalized, image)
    added = replaced = unchanged = excluded = 0
    merged = {item: list(records) for item, records in existing.items()}
    for (item_id, locator), (record, image) in incoming.items():
        records = merged.setdefault(item_id, [])
        prior = [r for r in records if r['locator'] == locator]
        if len(prior) > 1:
            raise ValueError(f'Duplicate existing page reviews: {item_id}|{locator}')
        if prior:
            # Image paths may differ before this collector made them portable.
            same = all(prior[0].get(k, '') == record.get(k, '') for k in
                       ('text', 'source_sha256', 'image_sha256', 'exclude_kind', 'excluded_reason'))
            if same:
                unchanged += 1
            elif not replace_reviewed:
                raise ValueError(f'Already reviewed page differs; inspect then use --replace-reviewed: {item_id}|{locator}')
            else:
                replaced += 1
            records[:] = [r for r in records if r['locator'] != locator]
        else:
            added += 1
        excluded += bool(record.get('exclude_kind'))
        records.append(record)
    summary = {'apply': apply, 'pages': len(incoming), 'added': added, 'replaced': replaced,
               'unchanged': unchanged, 'excluded': excluded}
    if apply:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        if target.is_file():
            shutil.copy2(target, target.with_name(f'reviewed-pages.before-collect-{stamp}.json.bak'))
        for record, image in incoming.values():
            destination = build / record['image_file']
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file():
                shutil.copy2(image, destination)
            if sha256_file(destination) != record['image_sha256']:
                raise ValueError(f'Changed collected review image: {destination}')
        temporary = target.with_suffix('.json.tmp')
        write_json(temporary, merged)
        temporary.replace(target)
        write_json(build / 'reviewed-pages-collect.json', {**summary, 'patches': [str(p) for p in patches]})
    return summary


def merged_sheet_table(text: str, layout: dict) -> str:
    """Render only GUARDED cell text, with merge geometry read from the original workbook.

    If filtering changed rows/columns, fail closed to the cleaned Markdown table.
    HTML values are escaped; source-provided markup never becomes active HTML.
    """
    if not layout or (not layout.get('merges') and layout.get('header_rows', 1) != 0):
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Collect source-bound, independently reviewed page transcripts')
    parser.add_argument('command', choices=['collect'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--patch', type=Path, action='append', required=True)
    parser.add_argument('--apply', action='store_true', help='Without this option, only validate and preview')
    parser.add_argument('--replace-reviewed', action='store_true', help='Allow reviewed text corrections after inspecting the patch')
    args = parser.parse_args()
    print(json.dumps(collect_reviews(args.out, args.patch, apply=args.apply,
                                    replace_reviewed=args.replace_reviewed), ensure_ascii=False))
