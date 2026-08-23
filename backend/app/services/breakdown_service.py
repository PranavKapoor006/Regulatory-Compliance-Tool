from __future__ import annotations

import re
from typing import Dict, List, Tuple

PAGE_MARKER_PATTERN = re.compile(r"--- Page\s+(\d+)\s*\|[^-]*---", flags=re.I)
SECTION_PATTERN = re.compile(
    r"^\s*(?P<section>\d+(?:\.\d+)*(?:\.|\))?)\s+(?P<body>\S[\s\S]*)$"
)
ANNEXURE_PATTERN = re.compile(r"^\s*(ANNEXURE\s+[A-Z0-9]+)\s*(.*)$", flags=re.I)
ROMAN_OR_ALPHA_PATTERN = re.compile(r"^\s*(\([a-z]\)|\([ivxlcdm]+\))\s+(.*)$", flags=re.I)

NOISE_PATTERNS = [
    r"^\s*page\s+\d+\s+of\s+\d+\s*$",
    r"^\s*page\s+\d+\s*$",
    r"^\s*(?:ref:\s*)?directive\s+\d{1,4}\.[a-z]\.[a-z].*$",
    r"^\s*financial services board\s*$",
    r"^\s*republic of south africa\s*$",
    r"^\s*fsb\s*$",
    r"^\s*directive\s*$",
    r"^\s*long-term insurance act.*$",
    r"^\s*short-term insurance act.*$",
    r"^\s*(?:issue date|effective date|status|withdrawal date|edition)\s*$",
    r"^\s*directive status.*$",
    r"^\s*withdrawal date\s*$",
    r"^\s*file:\s*.*$",
]

HEADING_FIXES = {
    "purpose": "PURPOSE",
    "background": "BACKGROUND",
    "application and scope of directive": "APPLICATION AND SCOPE OF DIRECTIVE",
    "legislative framework": "LEGISLATIVE FRAMEWORK",
    "clarification and application of terminology used in directive": "CLARIFICATION AND APPLICATION OF TERMINOLOGY USED IN DIRECTIVE",
    "outsourcing policy": "OUTSOURCING POLICY",
    "outsourcing agreements": "OUTSOURCING AGREEMENTS",
    "information sharing": "INFORMATION SHARING",
    "notification to the registrar": "NOTIFICATION TO THE REGISTRAR",
    "commencement": "COMMENCEMENT",
}


def _clean_line(line: str) -> str:
    line = line.replace("\u00ad", "")
    line = line.replace("￾", "")
    line = line.replace("|", " ")
    line = re.sub(r"[ \t]+", " ", line)
    line = re.sub(r"\s+([,.;:])", r"\1", line)
    line = line.strip()
    # A rotated scan can render the leading 9 in a clause marker as ``$``.
    # Repair only the unambiguous start-of-line marker shape; currency inside
    # clause wording is left untouched.
    line = re.sub(r"^\$\.(\d+)\s+", r"9.\1 ", line)
    return line


def _is_noise(line: str) -> bool:
    if not line:
        return True
    if len(line) <= 2 and not re.match(r"\d+(?:\.\d+)*", line):
        return True
    for pattern in NOISE_PATTERNS:
        if re.match(pattern, line, flags=re.I):
            return True
    # FSB/OCR table fragments from first page.
    if re.match(r"^\s*(addressee|edition|issued|in force|withdrawal|subject:)\b", line, flags=re.I):
        return True
    if re.fullmatch(r"(?:19|20)\d{2}|\d+(?:st|nd|rd|th)|-", line, flags=re.I):
        return True
    # Avoid standalone OCR fragments made mostly of punctuation/numbers.
    alpha_count = sum(ch.isalpha() for ch in line)
    if alpha_count < 3 and not re.match(r"\d+(?:\.\d+)*", line):
        return True
    return False


def _normalise_ocr_common(text: str) -> str:
    replacements = {
        "Lang-term": "Long-term",
        "lang-term": "long-term",
        "Short-tarm": "Short-term",
        "insurars": "insurers",
        "insurars": "insurers",
        "insurence": "insurance",
        "Diractive": "Directive",
        "diractive": "directive",
        "Registar": "Registrar",
        "Ragistrar": "Registrar",
        "prior te": "prior to",
        "relating ta": "relating to",
        "persen te": "person to",
        "govemance": "governance",
        "iiability": "liability",
        "seoured": "secured",
        "complyance": "compliance",
        "outsourci ": "outsourci",
        "out sourc": "outsourc",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def normalize_text(text: str) -> str:
    text = _normalise_ocr_common(text or "")
    lines = []
    current_page = "Unknown"
    for raw in text.replace("\r", "\n").split("\n"):
        marker = PAGE_MARKER_PATTERN.search(raw)
        if marker:
            current_page = marker.group(1)
            lines.append(f"[[PAGE:{current_page}]]")
            continue
        line = _clean_line(raw)
        if _is_noise(line):
            continue
        # Preserve an observed trailing dot because it distinguishes a real
        # digit-dot clause marker from a page number or footnote marker.
        line = re.sub(r"^\s*(\d+)\.\s*([A-Z][A-Z /&,-]{2,})\s*$", r"\1. \2", line)
        lines.append(line)
    return "\n".join(lines)


def _is_false_positive_section(section: str, body: str) -> bool:
    clean = section.rstrip(".")
    body_l = body.lower().strip()
    if not body_l:
        return True
    # Dates and decimals should not become sections.
    if re.match(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$", clean):
        return True
    if re.match(r"^\d{4}$", clean):
        return True
    if clean.startswith("0") and clean != "0":
        return True
    if re.match(
        r"^(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
        body_l,
    ):
        return True
    if len(clean.split(".")) > 5:
        return True
    try:
        if int(clean.split(".")[0]) > 50:
            return True
    except ValueError:
        pass
    if body_l.startswith(("of ", "and ", "or ", "to ", "in ", "for ")) and len(body_l.split()) < 5:
        return True
    return False


def _section_level(section: str) -> int:
    if section.lower().startswith("annexure"):
        return 1
    if section.startswith("("):
        return 99
    return section.rstrip(".").count(".") + 1


def _page_for_index(lines_with_pages: List[Tuple[str, str]], index: int) -> str:
    if index < 0 or index >= len(lines_with_pages):
        return "Unknown"
    return lines_with_pages[index][1]


def _is_upper_heading(text: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z ]", "", text).strip()
    if not cleaned:
        return False
    words = cleaned.split()
    if len(words) > 10:
        return False
    upper_ratio = sum(1 for ch in cleaned if ch.isupper()) / max(sum(1 for ch in cleaned if ch.isalpha()), 1)
    return upper_ratio > 0.65


def _combine_section_text(heading: str, parts: List[str]) -> str:
    body_parts = []
    if heading:
        body_parts.append(heading.strip())
    body_parts.extend(part.strip() for part in parts if part.strip())
    text = " ".join(body_parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def breakdown_regulatory_text(raw_text: str) -> List[Dict[str, str]]:
    """Break directive text into section rows before obligation extraction.

    Creates a new row for valid clause markers such as 1, 1.1, 2.2.1,
    Annexure markers, and child list markers. It preserves wording until the
    next valid section marker and avoids splitting dates/amounts/random numbers.
    """
    normalised = normalize_text(raw_text)
    lines_with_pages: List[Tuple[str, str]] = []
    page = "Unknown"
    for line in normalised.split("\n"):
        if line.startswith("[[PAGE:"):
            page = line.replace("[[PAGE:", "").replace("]]", "")
            continue
        if line.strip():
            lines_with_pages.append((line.strip(), page))

    # Native PDF extraction often places the marker and heading on separate
    # lines ("2.1" then "Board of directors"). Join only a standalone numeric
    # marker to the immediately following logical line; numbers inside dates,
    # amounts, or sentences remain untouched.
    joined_lines: List[Tuple[str, str]] = []
    index = 0
    while index < len(lines_with_pages):
        line, line_page = lines_with_pages[index]
        if re.fullmatch(r"\d+(?:\.\d+)*\.?", line) and index + 1 < len(lines_with_pages):
            next_line, next_page = lines_with_pages[index + 1]
            has_digit_dot_marker = "." in line
            if (
                next_page == line_page
                and not re.fullmatch(r"\d+(?:\.\d+)*\.?", next_line)
                and (has_digit_dot_marker or _is_upper_heading(next_line))
            ):
                joined_lines.append((f"{line} {next_line}", line_page))
                index += 2
                continue
        joined_lines.append((line, line_page))
        index += 1
    lines_with_pages = joined_lines

    rows: List[Dict[str, str]] = []
    current_section = "Introduction"
    current_heading = ""
    current_parts: List[str] = []
    current_page = "Unknown"
    sequence = 1
    seen_first_numbered_section = False

    def flush() -> None:
        nonlocal sequence, current_section, current_heading, current_parts, current_page
        wording = _combine_section_text(current_heading, current_parts)
        if wording:
            rows.append({
                "Sequence": sequence,
                "Section": current_section,
                "Language from Directive": wording,
                "Page": current_page,
            })
            sequence += 1
        current_heading = ""
        current_parts = []

    for idx, (line, line_page) in enumerate(lines_with_pages):
        annexure = ANNEXURE_PATTERN.match(line)
        section_match = SECTION_PATTERN.match(line)
        child_match = ROMAN_OR_ALPHA_PATTERN.match(line)

        if annexure:
            flush()
            current_section = annexure.group(1).upper()
            current_heading = annexure.group(2).strip()
            current_page = line_page
            seen_first_numbered_section = True
            continue

        if section_match:
            raw_candidate = section_match.group("section")
            candidate = raw_candidate.rstrip(".)")
            body = section_match.group("body").strip()
            digit_dot_marker = "." in raw_candidate or raw_candidate.endswith(")")
            if (digit_dot_marker or _is_upper_heading(body)) and not _is_false_positive_section(candidate, body):
                flush()
                current_section = candidate
                current_page = line_page
                seen_first_numbered_section = True
                current_heading = body if _is_upper_heading(body) or len(body.split()) <= 9 else ""
                if not current_heading:
                    current_parts.append(body)
                continue

        # Include list markers like (a) and (i) as child rows if inside a numbered section.
        if child_match and seen_first_numbered_section and current_section != "Introduction":
            marker, body = child_match.group(1), child_match.group(2)
            # Keep the child as part of the same parent section to match Mandar's desired Excel structure.
            current_parts.append(f"{marker} {body}")
            continue

        if not seen_first_numbered_section and current_section == "Introduction":
            if current_page == "Unknown":
                current_page = line_page
            current_parts.append(line)
        else:
            current_parts.append(line)

    flush()

    # Remove weak introduction rows that only contain page/header artifacts.
    cleaned_rows: List[Dict[str, str]] = []
    for row in rows:
        wording = row["Language from Directive"].strip()
        if row["Section"] == "Introduction" and len(wording.split()) < 8:
            continue
        cleaned_rows.append(row)

    for idx, row in enumerate(cleaned_rows, start=1):
        row["Sequence"] = idx

    # OCR sometimes keeps several consecutively numbered child clauses on one
    # physical line. Split only later siblings of the current clause (for
    # example 7.7.10 after 7.7.9), which avoids splitting legal cross-references.
    split_rows: List[Dict[str, str]] = []
    for row in cleaned_rows:
        section = str(row["Section"])
        wording = str(row["Language from Directive"])
        parts = section.split(".")
        if len(parts) < 2 or not all(part.isdigit() for part in parts):
            split_rows.append(row)
            continue
        parent_prefix = ".".join(parts[:-1])
        current_number = int(parts[-1])
        marker_re = re.compile(rf"(?<![\d.])({re.escape(parent_prefix)}\.(\d+))[.)]?\s+")
        markers = [
            match for match in marker_re.finditer(wording)
            if int(match.group(2)) > current_number
        ]
        if not markers:
            split_rows.append(row)
            continue
        starts = [0, *(match.start() for match in markers), len(wording)]
        sections = [section, *(match.group(1) for match in markers)]
        for split_index, (start, end) in enumerate(zip(starts, starts[1:])):
            body = wording[start:end].strip()
            if split_index:
                body = marker_re.sub("", body, count=1).strip()
            if body:
                split_rows.append({**row, "Section": sections[split_index], "Language from Directive": body})
    cleaned_rows = split_rows

    for idx, row in enumerate(cleaned_rows, start=1):
        row["Sequence"] = idx

    # Repair a narrow class of OCR errors where the middle component of a
    # sequential child clause is misread (7.8.7 between 7.5.6 and 7.5.8). The
    # correction is made only when the surrounding numeric sequence proves it.
    for idx in range(1, len(cleaned_rows) - 1):
        previous = str(cleaned_rows[idx - 1]["Section"])
        current = str(cleaned_rows[idx]["Section"])
        following = str(cleaned_rows[idx + 1]["Section"])
        prev_parts, current_parts, next_parts = previous.split("."), current.split("."), following.split(".")
        if not (len(prev_parts) == len(current_parts) == len(next_parts) == 3):
            continue
        if not all(part.isdigit() for part in [*prev_parts, *current_parts, *next_parts]):
            continue
        if (
            prev_parts[:2] == next_parts[:2]
            and int(current_parts[2]) == int(prev_parts[2]) + 1
            and int(next_parts[2]) == int(current_parts[2]) + 1
        ):
            cleaned_rows[idx]["Section"] = ".".join([*prev_parts[:2], current_parts[2]])

    # Repair a misread parent marker immediately before its correctly recognised
    # children (7.6 followed by 7.5.1 after 7.4 means the parent is 7.5).
    for idx in range(1, len(cleaned_rows) - 1):
        previous = str(cleaned_rows[idx - 1]["Section"]).split(".")
        current = str(cleaned_rows[idx]["Section"]).split(".")
        following = str(cleaned_rows[idx + 1]["Section"]).split(".")
        if not (len(previous) == len(current) == 2 and len(following) == 3):
            continue
        if not all(part.isdigit() for part in [*previous, *current, *following]):
            continue
        expected = int(previous[1]) + 1
        if previous[0] == current[0] == following[0] and int(following[1]) == expected and int(current[1]) != expected:
            cleaned_rows[idx]["Section"] = f"{current[0]}.{expected}"

    if not cleaned_rows:
        return [{"Sequence": 1, "Section": "Introduction", "Language from Directive": normalised.strip(), "Page": "Unknown"}]
    return cleaned_rows
