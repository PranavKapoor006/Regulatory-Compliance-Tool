from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Tuple

NON_DIRECTIVE_TITLE_PATTERNS = [
    r"regulatory\s+strategy",
    r"strategic\s+plan",
    r"annual\s+report",
    r"integrated\s+report",
    r"corporate\s+plan",
    r"discussion\s+document",
    r"consultation\s+paper",
    r"roadmap",
]

DIRECTIVE_SIGNALS = [
    r"\bdirective\b",
    r"\bcircular\b",
    r"\bconduct\s+standard\b",
    r"\bguidance\s+notice\b",
    r"\bcommunication\b",
]


def valid_pdf_path(path: Path) -> bool:
    """Return True only for a local file with a genuine PDF signature."""
    try:
        if not path.is_file() or path.stat().st_size < 200:
            return False
        with path.open("rb") as handle:
            return handle.read(1024).lstrip().startswith(b"%PDF-")
    except OSError:
        return False


def validate_directive_candidate(path: Path, text: str) -> Tuple[bool, str, Dict[str, str]]:
    """Validate whether a PDF looks like a directive/circular obligation source.

    The tool can technically read any PDF, but the business workflow requires a
    regulatory directive/circular as input. Strategic plans or public roadmaps
    should not be used to generate insurer obligations.
    """
    title_text = f"{path.name}\n{text[:3500]}".lower()

    for pattern in NON_DIRECTIVE_TITLE_PATTERNS:
        if re.search(pattern, title_text, flags=re.I):
            return (
                False,
                "This document appears to be a strategy/report-style document, not a directive/circular. "
                "Use a directive or circular such as Directive 159.A.i for obligation extraction.",
                {"document_type": "Non-directive / strategy-style document", "matched_pattern": pattern},
            )

    has_directive_signal = any(re.search(pattern, title_text, flags=re.I) for pattern in DIRECTIVE_SIGNALS)
    has_clause_structure = bool(re.search(r"(?m)^\s*\d+(?:\.\d+)*\.?\s+[A-ZA-Za-z]", text))
    has_regulatory_language = bool(
        re.search(r"\b(must|shall|required|directive|registrar|authority|insurer|licensee|outsourcing)\b", text, flags=re.I)
    )

    if not has_directive_signal and not (has_clause_structure and has_regulatory_language):
        return (
            False,
            "This PDF does not look like a directive/circular with clause-level regulatory obligations. "
            "Please upload a directive/circular document for obligation extraction.",
            {"document_type": "Unclear regulatory obligation source", "matched_pattern": "low directive confidence"},
        )

    return True, "Document passed directive/circular validation.", {"document_type": "Directive/circular candidate", "matched_pattern": "directive signals"}
