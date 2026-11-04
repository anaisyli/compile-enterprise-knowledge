#!/usr/bin/env python3
"""The sub-sections of each module, and which of them a passage belongs to.

Classification is by keyword, done by the script: it costs no tokens, and a person can
correct it by editing the keyword lists, never passage by passage.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kb_common import load_json


DEFAULTS = Path(__file__).resolve().parents[1] / "assets" / "sections-default.json"
UNSORTED = "未分类"


def load_sections(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Module key -> sections. A section the config names replaces the default one of that name."""
    table = {key: [dict(s) for s in value] for key, value in load_json(DEFAULTS).items() if not key.startswith("_")}
    for module, overrides in config.get("sections", {}).items():
        current = table.setdefault(module, [])
        for override in overrides:
            names = [s["name"] for s in current]
            if override.get("remove"):
                table[module] = current = [s for s in current if s["name"] != override["name"]]
            elif override["name"] in names:
                current[names.index(override["name"])] = override
            else:
                current.append(override)
    for sections in table.values():
        for section in sections:
            section["pattern"] = keyword_pattern(section.get("keywords", []))
    return table


def keyword_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    # Latin keywords match from a word start, so "test" does not hit "latest".
    parts = [(r"(?<![a-z])" if re.match(r"[a-z]", k.casefold()) else "") + re.escape(k.casefold()) for k in keywords if k]
    return re.compile("|".join(parts)) if parts else None


def classify(text: str, sections: list[dict[str, Any]]) -> list[str]:
    folded = text.casefold()
    return [s["name"] for s in sections if s["pattern"] and s["pattern"].search(folded)]
