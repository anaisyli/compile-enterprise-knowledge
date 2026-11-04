#!/usr/bin/env python3
"""Put the text boxes of a page or slide back in reading order.

Catalog pages and slides place several products side by side. Read in the order the file
stores them, a style code ends up beside another product's features. Here boxes are grouped
into columns by where they sit: a code above a column stays with that column. Nothing is
reworded; only the order of whole boxes changes.
"""

from __future__ import annotations

from typing import Any

# A box wider than this share of the page is a title or a band across columns.
SPANNING_SHARE = 0.6
# Two boxes are in one column when they overlap by this share of the narrower one.
OVERLAP_SHARE = 0.3


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def columns(boxes: list[dict[str, Any]], width: float) -> list[list[dict[str, Any]]]:
    """Return groups in reading order: bands across the page, and the columns between them."""
    boxes = [b for b in boxes if b["text"].strip()]
    if not boxes:
        return []
    spanning = [b for b in boxes if width and (b["x1"] - b["x0"]) > width * SPANNING_SHARE]
    # Narrow boxes mark out the columns; a wider one (a code with its product name) joins the
    # column it overlaps most and does not widen it, or two products would merge into one.
    rest = sorted((b for b in boxes if b not in spanning), key=lambda b: (b["x1"] - b["x0"], b["x0"]))
    cols: list[dict[str, Any]] = []
    for box in rest:
        span = (box["x0"], box["x1"])
        width_box = max(span[1] - span[0], 0.01)
        fits = [(overlap(c["span"], span), c) for c in cols]
        fits = [(o, c) for o, c in fits if o > OVERLAP_SHARE * min(width_box, c["span"][1] - c["span"][0])]
        home = max(fits, key=lambda pair: pair[0])[1] if fits else None
        if home is None:
            home = {"span": span, "boxes": []}
            cols.append(home)
        home["boxes"].append(box)
    groups = [{"top": min(b["y0"] for b in c["boxes"]), "x": c["span"][0], "boxes": sorted(c["boxes"], key=lambda b: (b["y0"], b["x0"]))}
              for c in cols]
    top = min((g["top"] for g in groups), default=0.0)
    bottom = max((max(b["y1"] for b in g["boxes"]) for g in groups), default=0.0)
    above = sorted((b for b in spanning if b["y0"] <= top), key=lambda b: b["y0"])
    below = sorted((b for b in spanning if b["y0"] > top), key=lambda b: b["y0"])
    ordered = [[b] for b in above] + [g["boxes"] for g in sorted(groups, key=lambda g: g["x"])]
    # A band in the middle of the page still reads after the columns above it end.
    ordered += [[b] for b in below if b["y0"] >= bottom] + [[b] for b in below if b["y0"] < bottom]
    return ordered


def ordered_text(boxes: list[dict[str, Any]], width: float) -> list[str]:
    """One string per column or band, each box's own text unchanged."""
    return ["\n".join(b["text"].strip() for b in group) for group in columns(boxes, width)]
