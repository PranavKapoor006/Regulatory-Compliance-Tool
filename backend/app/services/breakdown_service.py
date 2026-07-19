from __future__ import annotations

import re
from typing import List, Dict

# Clause marker must appear at the start of a logical line. This prevents splitting numbers
# inside dates, amounts, percentages, addresses, or normal sentences.
CLAUSE_PATTERN = re.compile(r"(?m)^\s*((?:\d+\.)+\d*)\s+(?=\S)")
PAGE_MARKER_PATTERN = re.compile(r"--- Page (\d+) ---")


def normalize_text(text: str) -> str:
    lines = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        lines.append(re.sub(r"\s+", " ", line))
    return "\n".join(lines)


def _page_for_position(text: str, pos: int) -> str:
    page = "Unknown"
    for match in PAGE_MARKER_PATTERN.finditer(text[:pos]):
        page = match.group(1)
    return page


def breakdown_regulatory_text(raw_text: str) -> List[Dict[str, str]]:
    """Break directive text into Introduction + numbered clauses.

    Logic:
    - Splits only when a digit-dot marker starts a new logical line, e.g. 1, 1., 2.1, 2.2.1.
    - Preserves hierarchical references by treating the full marker as Section.
    - Does not split numeric values in dates/amounts/sentences because those are not line-start markers.
    - Text before the first numbered clause is stored as Introduction.
    """
    text = normalize_text(raw_text)
    matches = list(CLAUSE_PATTERN.finditer(text))
    rows: List[Dict[str, str]] = []
    sequence = 1

    if not matches:
        return [{
            "Sequence": 1,
            "Section": "Introduction",
            "Language from Directive": text.strip(),
            "Page": "Unknown",
        }]

    intro = text[: matches[0].start()].strip()
    if intro:
        rows.append({
            "Sequence": sequence,
            "Section": "Introduction",
            "Language from Directive": intro,
            "Page": _page_for_position(text, 0),
        })
        sequence += 1

    for idx, match in enumerate(matches):
        section = match.group(1).rstrip(".")
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        wording = text[start:end].strip()
        if not wording:
            continue
        rows.append({
            "Sequence": sequence,
            "Section": section,
            "Language from Directive": wording,
            "Page": _page_for_position(text, match.start()),
        })
        sequence += 1
    return rows
