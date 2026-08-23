from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from rapidfuzz.fuzz import partial_ratio
except ImportError:  # pragma: no cover - production installs rapidfuzz
    def partial_ratio(first: str, second: str) -> int:
        """Small stdlib fallback so extraction can still start without rapidfuzz."""
        if not first or not second:
            return 0
        shorter, longer = sorted((first, second), key=len)
        if shorter in longer:
            return 100
        if len(longer) <= len(shorter):
            return int(round(100 * SequenceMatcher(None, shorter, longer).ratio()))
        best = 0.0
        window = len(shorter)
        step = max(1, window // 20)
        for start in range(0, len(longer) - window + 1, step):
            best = max(best, SequenceMatcher(None, shorter, longer[start:start + window]).ratio())
        return int(round(100 * best))

import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text, normalize_text
from app.services.document_validation_service import validate_directive_candidate
from app.services.llm_service import chat_json
from app.services.pdf_service import extract_pdf_text, extraction_summary
from app.services.prompt_service import OBLIGATION_SYSTEM_PROMPT, obligation_user_prompt
from app.services.storage import output_path
from app.services.taxonomy_service import (
    allowed_categories,
    classify_category,
    classify_department_support,
    validate_department_support,
)

OBLIGATION_PIPELINE_VERSION = "2026-08-06.2"

OBLIGATION_COLUMNS = [
    "Section",
    "Language from Directive",
    "Obligation",
    "Obligation Category",
    "Primary Responsible Department",
    "Support Function",
    "Priority",
    "Actionable",
    "Source Page",
    "Document Accuracy %",
    "Accuracy Rating",
    "Accuracy Notes",
]

INTERNAL_OBLIGATION_EXPORT_COLUMNS = [
    "Document Accuracy %",
    "Accuracy Rating",
    "Accuracy Notes",
]

ACTION_PATTERN = re.compile(
    r"\b(must|shall|required|require[sd]?|ensure[sd]?|notify|submit|maintain|"
    r"establish|implement|document|review|approve|monitor|report|comply|provide|"
    r"prohibit|may\s+not|assess|identify|develop|secure|retain|record|appl(?:y|ies)|"
    r"remain(?:s|ed|ing)?\s+responsible)\b",
    flags=re.I,
)
NON_ACTIONABLE_PATTERN = re.compile(
    r"\b(purpose|background|available\s+on\s+the\s+website|means|definition|"
    r"for\s+information|guidance\s+only)\b",
    flags=re.I,
)
ACCURACY_METHOD = (
    "Document-grounded extraction estimate based on source-page traceability, "
    "preservation of material legal elements, answer completeness, and native/OCR text quality. "
    "It is not a substitute for independent legal review."
)
ACCURACY_STOPWORDS = {
    "about", "after", "against", "also", "been", "being", "between", "could", "from",
    "have", "into", "must", "shall", "that", "their", "there", "these", "this", "those",
    "under", "which", "with", "would", "insurer", "regulated", "entity", "requirement",
    "comply", "compliance",
}
MATERIAL_PATTERNS = {
    "prohibition": re.compile(r"\b(?:may\s+not|must\s+not|shall\s+not|prohibit(?:ed|s)?)\b", re.I),
    "condition": re.compile(r"\b(?:if|unless|where|when|whenever|provided\s+that|subject\s+to|except|regardless)\b", re.I),
    "timing": re.compile(r"\b(?:within|before|after|immediately|annually|monthly|quarterly|no\s+later\s+than|at\s+least)\b", re.I),
    "regulator": re.compile(r"\b(?:FSCA|FSB|Registrar|Prudential\s+Authority|regulator|authority)\b", re.I),
    "approval": re.compile(r"\b(?:approve[sd]?|approval|consent|authoris(?:e|ed|ation))\b", re.I),
    "reporting": re.compile(r"\b(?:notify|notification|submit|report|disclose)\b", re.I),
    "records": re.compile(r"\b(?:retain|record|document|evidence|register)\b", re.I),
    "monitoring": re.compile(r"\b(?:monitor|review|assess|oversight|audit)\b", re.I),
    "cross-reference": re.compile(
        r"\bparagraphs?\s+\d+(?:\.\d+)?(?:\s+(?:to|and)\s+\d+(?:\.\d+)?)?",
        re.I,
    ),
}
MATERIAL_QUANTITY_PATTERN = re.compile(
    r"\b(?:\d+(?:[.,]\d+)*|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirty|sixty)\s*(?:business\s+|calendar\s+)?"
    r"(?:hours?|days?|weeks?|months?|years?|percent|%)\b",
    re.I,
)
TRAILING_SECTION_HEADING_PATTERN = re.compile(
    r"\s+(?:"
    r"principles\s+with\s+which\s+any\s+outsourcing\s+must\s+comply|"
    r"written\s+contracts?|management\s+and\s+regular\s+review|"
    r"internal\s+review\s+and\s+approvals?|outsourcing\s+policy|"
    r"notification\s+of\s+outsourcing\s+of\s+control,\s*management\s+or\s+material\s+functions?"
    r")\s*$",
    re.I,
)
OCR_NOISE_TOKEN_PATTERN = re.compile(
    r"\b(?:"
    r"bireaive|coinplanee|complanos|compliarice|cough|disclve|feepeoivel|"
    r"moron|oumoureng|pecyeal|piste|scotons|sheseee|sortert|wie?nanes|"
    r"weieanee|spaniel"
    r")\b",
    re.I,
)
OCR_FOOTER_PATTERN = re.compile(
    r"\b(?:"
    r"REGIST(?:RAR)?\s*['’]?\s*S?\s+OF\s+LONG-TERM\s+AND\s+SHORT-TERM\s+INSURANCE|"
    r"DIRECTIVE\s+\d{1,4}(?:\.[A-Z])?(?:\.[A-Z])?\s*[,.:]?\s*\(?(?:LT|ST)\b"
    r")",
    re.I,
)


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    repairs = (
        (r"\bAninsurer\b", "An insurer"),
        (r"\bA-written\b", "A written"),
        (r"\bregulatary\b", "regulatory"),
        (r"\bcperations\b", "operations"),
        (r"\bcontre!\s+function\b", "control function"),
        (r"\breferrec\b", "referred"),
        (r"\bOctcber\b", "October"),
        (r"\bAsscciation\b", "Association"),
        (r"\bgovemed\b", "governed"),
        (r"\bgovemance\b", "governance"),
        (r"\bbeard of directors\b", "board of directors"),
        (r"\bfer the insurance ousiness\b", "for the insurance business"),
        (r"\bPrincipies\b", "Principles"),
        (r"\bobligatians\b", "obligations"),
        (r"\bRemuneration paic\b", "Remuneration paid"),
        (r"\bmust net result\b", "must not result"),
        (r"\bcommission cr a binder fee\b", "commission or a binder fee"),
        (r"\bset cut in\b", "set out in"),
        (r"\bprior te\b", "prior to"),
        (r"\brelating ta\b", "relating to"),
        (r"\bpersen te\b", "person to"),
        (r"\bRegistrar te\b", "Registrar to"),
        (r"\baccess\s+\{to\b", "access to"),
        (r"\bActs\}", "Acts)"),
        (r"\binsurers compliance\b", "insurer's compliance"),
        (r"\bLTast\b", "LT Act"),
        (r"\b42\s+April\s+2042\b", "12 April 2012"),
        (r"\bSections?\s+&3\\+b\)\{i\)", "Sections 9(3)(b)(i)"),
        (r"\bsections?\s+12\(1\\+\(c\)", "sections 12(1)(c)"),
        (r"\bsection\s+%3\\+b\\+i\)", "section 9(3)(b)(i)"),
        (r"\bsection\s+9?%3\\+b\\+[xi]+\)", "section 9(3)(b)(i)"),
        (r"\bfong-term\b", "long-term"),
        (r"\bTine\b", "The"),
        (r"[‘']egislation\b", "legislation"),
    )
    for pattern, replacement in repairs:
        text = re.sub(pattern, replacement, text, flags=re.I)
    # Tesseract frequently substitutes braces for parentheses in legal
    # citations and contract wording. Curly braces have no operative use in
    # the supported source documents, so normalise them before export.
    text = text.replace("{", "(").replace("}", ")")
    return text


def _looks_like_ocr_intrusion(value: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9]+", value)
    if len(tokens) < 5:
        return False
    upper_tokens = sum(1 for token in tokens if len(token) >= 2 and token.isupper())
    short_tokens = sum(1 for token in tokens if len(token) <= 2)
    return bool(
        OCR_NOISE_TOKEN_PATTERN.search(value)
        or upper_tokens >= 2
        or short_tokens / max(len(tokens), 1) >= 0.35
        or re.search(r"[\\{}]|\b\d+[A-Z]{2,}\b", value)
    )


def _has_strong_ocr_noise(value: str) -> bool:
    """Detect corruption strongly enough to remove without guessing legal text."""
    tokens = re.findall(r"[A-Za-z0-9]+", value)
    if not tokens:
        return False
    short_tokens = sum(1 for token in tokens if len(token) <= 2)
    return bool(
        OCR_NOISE_TOKEN_PATTERN.search(value)
        or OCR_FOOTER_PATTERN.search(value)
        or re.search(r"[\\{}]|\b\d+[A-Z]{2,}\b", value)
        or (len(tokens) >= 6 and short_tokens / len(tokens) >= 0.35)
    )


def _clean_operative_fragment(text: str, parent_context: str = "") -> str:
    """Remove headings, footers and obvious OCR intrusions from one legal clause."""
    cleaned = TRAILING_SECTION_HEADING_PATTERN.sub("", _clean(text)).strip()

    # Cover-page OCR can merge a status table into the short document subject.
    # Keep the meaningful subject line while discarding dates and table debris.
    if re.search(r"\bIssue\s+date\b.*\b(?:Directive\s+Status|Withdrawal\s+date)\b", cleaned, re.I):
        subject = re.search(r"\bCompliance\s+with\s+sections?\b.+$", cleaned, re.I)
        if subject:
            cleaned = subject.group(0).strip()

    cleaned = re.split(
        r"(?<=\.)\s+(?=Insurance\s+core\s+principles\b)",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0]

    # Explanatory footnotes can be concatenated to the preceding clause by a
    # scanned-page layout. They are useful in the PDF but must not become part
    # of the operative clause or the generated register.
    cleaned = re.split(
        r"(?<=\.)\s+\d+\s+(?=(?:This\s+includes|Means)\b)",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0]

    # Signature blocks and repeated page headers can follow a completed legal
    # sentence. Remove only a recognised document-footer start.
    footer = OCR_FOOTER_PATTERN.search(cleaned)
    if footer and footer.start() > 0:
        prefix = cleaned[:footer.start()].rstrip()
        if prefix.endswith((".", ";", ":", ")")):
            cleaned = prefix

    # A numbered list child normally ends at its first semicolon. OCR can append
    # a page header, signature block, or the footnote from the bottom of the
    # scanned page after that delimiter.
    if _is_structural_stem(parent_context) and ";" in cleaned:
        first_clause, remainder = cleaned.split(";", 1)
        if remainder.strip() and not re.fullmatch(r"\s*(?:and|or)\s*", remainder, re.I):
            cleaned = first_clause.strip() + ";"

    # Binder footnotes in scanned directives often start immediately after a
    # child's closing colon. Keep the operative child, not the footnote.
    cleaned = re.split(
        r"(?<=:)\s+(?=(?:(?:[-*]\s*)+)?(?:the\s+\w+\s+functions?\s+referred\s+to|"
        r"directive\s+\d|compliance\s+with\s+sections?))",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0]

    # Repair an internal page-header intrusion without guessing the legal text:
    # retain the stable words on both sides only when the intervening span has
    # strong OCR-noise signals.
    meet_match = re.search(
        r"\bmeet\b(?P<noise>(?:\s+\S+){4,45}?)\s+\bregulatory\s+requirements\b",
        cleaned,
        re.I,
    )
    if meet_match and _looks_like_ocr_intrusion(meet_match.group("noise")):
        cleaned = (
            cleaned[:meet_match.start()]
            + "meet regulatory requirements"
            + cleaned[meet_match.end():]
        )

    # A structural parent may end at a list dash while the next page's header
    # is inserted before its numbered children. Preserve the legal list stem
    # and remove only a tail with strong OCR signals.
    structural_tail = re.search(
        r"^(?P<stem>.+?\b(?:of|following|least|may))\s*(?P<dash>[-—~])\s+(?P<tail>.+)$",
        cleaned,
        re.I,
    )
    if structural_tail and ACTION_PATTERN.search(structural_tail.group("stem")):
        if _has_strong_ocr_noise(structural_tail.group("tail")):
            cleaned = structural_tail.group("stem").rstrip() + " —"

    # A completed sentence followed by a dense OCR fragment (for example a
    # cropped page border) is safe to truncate. Evaluate from the last sentence
    # boundary so legitimate multi-sentence clauses remain intact.
    sentence_boundaries = list(re.finditer(r"[.;](?=\s+\S)", cleaned))
    for boundary in reversed(sentence_boundaries):
        tail = cleaned[boundary.end():].strip()
        if tail and _has_strong_ocr_noise(tail) and not ACTION_PATTERN.search(tail):
            cleaned = cleaned[:boundary.end()].strip()
            break

    # A page footer after a completed child clause is never part of the answer.
    if _is_structural_stem(parent_context):
        terminator = re.search(r"[.;](?=\s+[A-Z][A-Za-z]{2,}(?:\s|$))", cleaned)
        if terminator:
            tail = cleaned[terminator.end():]
            if _looks_like_ocr_intrusion(tail):
                cleaned = cleaned[:terminator.end()]

    return TRAILING_SECTION_HEADING_PATTERN.sub("", cleaned).strip()


def sanitize_source_wording(text: str, parent_context: str = "") -> str:
    """Return source-faithful clause text that is safe to place in final outputs."""
    return _clean_operative_fragment(text, parent_context)


def _is_structural_stem(text: str) -> bool:
    cleaned = _clean(text)
    return bool(
        re.search(r"(?:at\s+least|following|as\s+follows)\s*[-—:]?\.?$", cleaned, flags=re.I)
        or re.search(r"\b(?:must|shall|may\s+not)\b.{0,100}[-—:]\.?$", cleaned, flags=re.I)
        or re.search(r"\b(?:applies|apply)\s+to\s*[~\-—:]?\.?$", cleaned, flags=re.I)
        or re.search(r"\b(?:must|shall|may\s+not)\b.{0,300}\b(?:of|following|least|may)\s*[-—~]", cleaned, flags=re.I)
    )


def _parent_stem(text: str) -> str:
    """Return the operative parent text without an OCR footnote after its list dash."""
    cleaned = _clean(text)
    # Require whitespace around the list delimiter. A bare ``-`` also occurs
    # inside legal terms such as "long-term" and must never truncate the actor.
    cleaned = re.sub(r"\s+(?:[-—~])(?:\s+.*)?$", "", cleaned).rstrip(" :")
    return cleaned


def _inherits_parent_action(text: str, parent_context: str) -> bool:
    if not parent_context or not ACTION_PATTERN.search(parent_context):
        return False
    if _is_structural_stem(parent_context):
        return True
    # Short numbered list fragments frequently contain an incidental verb such
    # as "comply" but still depend on the parent's primary action ("notify",
    # "assess", "include", etc.).
    has_standalone_actor = bool(
        re.search(
            r"\b(?:an?\s+insurer|the\s+insurer|insurers|the\s+regulated\s+entity|"
            r"the\s+board|management|the\s+service\s+provider)\b.{0,90}"
            r"\b(?:must|shall|may\s+not|is\s+required\s+to)\b",
            text,
            re.I,
        )
    )
    return not has_standalone_actor and len(_clean(text).split()) <= 45


def is_actionable(text: str, parent_context: str = "") -> bool:
    text = _clean(text)
    if _is_structural_stem(text):
        return False
    if (
        re.search(r"\bthis\s+directive\s+does\s+not\s+apply\b", text, re.I)
        and not re.search(r"\b(?:must|shall|required)\b", text, re.I)
    ):
        return False
    if (
        re.search(r"\bguidance\s+on\s+risks\b", text, re.I)
        and re.search(r"\bshould\b", text, re.I)
        and not re.search(r"\b(?:must|shall|required|may\s+not)\b", text, re.I)
    ):
        return False
    if re.search(r"\b(?:forms?|directive)\b.*\bavailable\s+on\s+the\s+website\b", text, flags=re.I) and not re.search(r"\b(?:insurer|entity|board|management)\b.{0,80}\b(?:must|shall)\b", text, flags=re.I):
        return False
    if re.search(r"\binternational standards?\b.*\brequire", text, flags=re.I) and not re.search(r"\binsurers?\b.{0,100}\b(?:must|shall|required)\b", text, flags=re.I):
        return False
    # Legislative background describing a Registrar's power or a registration
    # decision is not itself an implementation duty of the insurer. Do not
    # manufacture "the regulated entity must comply" from the authority's act.
    authority_only = bool(
        re.search(r"\bregistrar\s+may\s+prohibit\b", text, re.I)
        or re.search(
            r"\bapplication\b.{0,220}\bmay\s+not\s+be\s+granted\s+by\s+the\s+registrar\b",
            text,
            re.I,
        )
    )
    entity_duty = bool(
        re.search(
            r"\b(?:an?\s+insurer|the\s+insurer|insurers|the\s+board|managing\s+executives)\s+"
            r"(?:must|shall|is\s+required\s+to|remain(?:s)?\s+responsible|may\s+not\s+(?!be\s+granted))\b",
            text,
            re.I,
        )
    )
    if authority_only and not entity_duty:
        return False
    if ACTION_PATTERN.search(text):
        return True
    # A list item inherits the action from a parent clause such as "A policy must
    # include—". This prevents child rows from being incorrectly marked contextual.
    if parent_context and ACTION_PATTERN.search(parent_context):
        return len(text.split()) >= 2 and not NON_ACTIONABLE_PATTERN.search(text)
    return False


def priority(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\b(must|shall|may not|prohibit|immediately|no later than|breach|non-compliance)\b", lowered):
        return "High"
    if re.search(r"\b(should|may|consider|guidance)\b", lowered):
        return "Low"
    return "Medium"


def generate_obligation(section: str, wording: str, parent_context: str = "") -> str:
    text = _clean_operative_fragment(wording, parent_context)
    if not is_actionable(text, parent_context):
        return "Informational or contextual text; no standalone implementation obligation is created."

    # Preserve the regulatory condition and actor where possible while normalising
    # shall/required language into a reviewable must statement.
    # Child list items inherit the parent's actor and action. Without this,
    # "the proposed outsourcing" could be assessed as an internal-information
    # requirement even though its parent says "notify the Registrar of—".
    inherited = ""
    if _inherits_parent_action(text, parent_context):
        inherited = _parent_stem(parent_context)
    source_text = f"{inherited} {text}".strip() if inherited else text
    # Keep the complete operative clause. Filtering sentence-by-sentence can
    # silently drop a preceding condition, exception, deadline, or approval
    # sentence merely because that sentence does not repeat the word "must".
    statement = source_text
    actor = re.search(r"\b(?:An insurer|The insurer|Insurers|The regulated entity|The board|A minimum|At least|Each|Any|The Registrars?|There must)\b", statement)
    material_prefix = statement[:actor.start()] if actor else ""
    if (
        actor
        and 0 < actor.start() < 140
        and not re.search(r"\b(?:if|unless|where|when|provided|subject|except|before|after|within|regardless)\b", material_prefix, re.I)
    ):
        statement = statement[actor.start():]
    statement = re.sub(r"\bshall\b", "must", statement, flags=re.I)
    statement = re.sub(r"\bmay\s+not\b", "must not", statement, flags=re.I)
    statement = re.sub(r"\bis required to\b", "must", statement, flags=re.I)
    statement = re.sub(r"\bremain(?:s)?\s+responsible\b", "must remain responsible", statement, flags=re.I)
    # Signature blocks and OCR footer fragments can be appended to the final
    # operative sentence on the page. They are not part of the obligation.
    if re.search(r"\b(?:must|shall|required|notify|bring)\b", statement, re.I):
        statement = re.split(
            r"\s+(?=(?:REGIST\s+S|DIRECTIVE\s+\d+[.,])\b)",
            statement,
            maxsplit=1,
            flags=re.I,
        )[0]
    statement = statement.rstrip(" ,;:—-~")
    statement = re.sub(r"(?:[,;]\s*)?\b(?:and|or)\s*$", "", statement, flags=re.I).rstrip(" ;")
    direct_match = re.search(r"\bdirect\s+long-term and short-term insurers.*?\bto comply\b(?P<rest>.*)", statement, flags=re.I)
    if direct_match:
        statement = f"Insurers must comply{direct_match.group('rest')}"
    elif re.search(r"\bthis directive applies\b", statement, flags=re.I) and not re.search(r"\bmust\b", statement, flags=re.I):
        statement = f"The regulated entity must comply with this applicability and scope provision: {statement}"
    elif not re.search(r"\bmust\b", statement, flags=re.I):
        statement = f"The regulated entity must comply with this requirement: {statement or 'the stated regulatory requirement'}"
    statement = re.sub(r"[,;:]\s*\.$", ".", statement)
    if not statement.endswith("."):
        statement += "."
    return statement


def _text_cleanliness_score(value: str) -> int:
    text = _clean(value)
    if re.search(r"informational|contextual|parent clause", text, re.I):
        return 100
    penalty = 0
    penalty += min(45, len(OCR_NOISE_TOKEN_PATTERN.findall(text)) * 9)
    penalty += min(20, len(re.findall(r"[\\{}]|(?:^|\s)[=_~]{1,}(?:\s|$)", text)) * 4)
    if TRAILING_SECTION_HEADING_PATTERN.search(text):
        penalty += 15
    if len(text.split()) > 120:
        penalty += 15
    if _looks_like_ocr_intrusion(text) and OCR_NOISE_TOKEN_PATTERN.search(text):
        penalty += 10
    return max(35, 100 - penalty)


def _normalise_for_accuracy(value: Any) -> str:
    text = _clean(value).lower()
    text = re.sub(r"\bshall\b|\bis required to\b", "must", text)
    text = re.sub(r"\bmay\s+not\b|\bshall\s+not\b", "must not", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalise_document_source_for_accuracy(value: Any) -> str:
    """Apply the same OCR repairs used by section breakdown before matching pages."""
    return _normalise_for_accuracy(normalize_text(str(value or "")))


def _clean_clause_token_traceability(source_norm: str, page_norm: str) -> int:
    """Score a clean clause when OCR layout disrupts otherwise matching word order.

    Rotated scanned pages can return adjacent columns or list items in a different
    reading order. Phrase similarity then understates traceability even when all
    legally meaningful clause tokens are present on the assigned page. This
    fallback is deliberately limited to clauses with at least four meaningful
    tokens; contaminated source clauses remain governed by the stricter phrase
    and cleanliness checks.
    """
    source_tokens = _important_tokens(source_norm)
    page_tokens = _important_tokens(page_norm)
    if len(source_tokens) < 4 or not page_tokens:
        return 0
    coverage = len(source_tokens & page_tokens) / len(source_tokens)
    if coverage >= 0.999:
        return 100
    if coverage >= 0.90:
        return 96
    if coverage >= 0.80:
        return 92
    return 0


def _important_tokens(value: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z][a-z'-]{3,}", _normalise_for_accuracy(value)):
        if token in ACCURACY_STOPWORDS:
            continue
        tokens.add(token[:8])
    return tokens


def _material_elements(value: str) -> Dict[str, List[str]]:
    text = _clean(value)
    elements: Dict[str, List[str]] = {}
    for name, pattern in MATERIAL_PATTERNS.items():
        matches = sorted({_normalise_for_accuracy(match.group(0)) for match in pattern.finditer(text)})
        if matches:
            elements[name] = matches
    quantities = sorted({_normalise_for_accuracy(match.group(0)) for match in MATERIAL_QUANTITY_PATTERN.finditer(text)})
    if quantities:
        elements["quantities/deadlines"] = quantities
    return elements


def _text_quality_score(page_data: Dict[str, Any] | None) -> int:
    if not page_data:
        return 55
    readability = int(page_data.get("score") or 0)
    if page_data.get("method") == "ocr":
        return max(55, min(95, 55 + int(readability / 15)))
    return max(70, min(100, 75 + int(readability / 20)))


def assess_obligation_accuracy(
    *,
    section: str,
    source_text: str,
    obligation: str,
    actionable: bool,
    source_page: str,
    pages: List[dict],
    parent_context: str = "",
) -> Dict[str, Any]:
    page_data = next((page for page in pages if str(page.get("page")) == str(source_page)), None)
    page_text = str((page_data or {}).get("text") or "")
    # Section breakdown repairs stable OCR substitutions such as ``insurars``,
    # ``iiability`` and ``seoured``. Page traceability must apply those same
    # repairs; otherwise a correctly extracted child clause can be falsely
    # capped at 74% merely because the raw page still contains the OCR spelling.
    source_norm = _normalise_document_source_for_accuracy(source_text)
    page_norm = _normalise_document_source_for_accuracy(page_text)
    source_cleanliness = _text_cleanliness_score(source_text)
    if source_norm and source_norm in page_norm:
        source_fidelity = 100
    elif source_norm and page_norm:
        source_fidelity = int(round(partial_ratio(source_norm, page_norm)))
        # Only clean clauses may use unordered legal-token coverage. Dirty OCR
        # remains conservative and must still be verified against the PDF.
        if source_cleanliness >= 90:
            source_fidelity = max(
                source_fidelity,
                _clean_clause_token_traceability(source_norm, page_norm),
            )
    else:
        source_fidelity = 45

    if not actionable:
        material_score = 100
        missing: List[str] = []
        complete_answer = bool(
            re.search(r"informational|contextual|parent clause", obligation, re.I)
        )
    else:
        # Build the comparison population from the complete operative clause,
        # not from every token that happened to be extracted into the same
        # source row. This excludes non-operative headings, website notices,
        # signature/footer noise, and a structural parent that the child does
        # not actually inherit. It still preserves conditions, deadlines,
        # prohibitions, regulator duties, approvals, and cross-references.
        #
        # Candidate LLM answers are therefore checked against the deterministic
        # source-grounded obligation rather than being allowed to define their
        # own completeness population.
        complete_source = generate_obligation(section, source_text, parent_context)
        required_elements = _material_elements(complete_source)
        obligation_norm = _normalise_for_accuracy(obligation)
        missing = []
        total_elements = 0
        preserved_elements = 0
        for name, values in required_elements.items():
            for value in values:
                total_elements += 1
                value_norm = _normalise_for_accuracy(value)
                if value_norm and value_norm in obligation_norm:
                    preserved_elements += 1
                else:
                    missing.append(f"{name}: {value}")
        element_score = 100 if total_elements == 0 else int(round(100 * preserved_elements / total_elements))
        source_tokens = _important_tokens(complete_source)
        answer_tokens = _important_tokens(obligation)
        token_score = 100 if not source_tokens else int(round(100 * len(source_tokens & answer_tokens) / len(source_tokens)))
        material_score = element_score
        complete_answer = bool(
            re.search(r"\bmust(?:\s+not)?\b", obligation, re.I)
            and obligation.rstrip().endswith((".", ";"))
            and len(obligation.split()) >= 6
            and not re.search(r"(?:—|-|:|\b(?:and|or|of|to|the))\s*\.$", obligation.strip(), re.I)
        )

    answer_completeness = 100 if complete_answer else 35
    text_quality = _text_quality_score(page_data)
    text_cleanliness = _text_cleanliness_score(obligation)
    overall = int(round(
        (0.22 * source_fidelity)
        + (0.28 * material_score)
        + (0.10 * token_score if actionable else 0.10 * 100)
        + (0.08 * text_quality)
        + (0.08 * answer_completeness)
        + (0.10 * text_cleanliness)
        + (0.14 * source_cleanliness)
    ))
    # A fluent cleaned answer is not enough to justify "High" confidence when
    # the source clause itself contains obvious OCR/page-layout corruption.
    # Keep the answer available, but cap its estimate and require human review.
    if source_cleanliness < 85 or text_cleanliness < 85:
        overall = min(overall, 84)
    if source_fidelity < 75:
        overall = min(overall, 74)
    if page_data and page_data.get("method") == "ocr":
        overall = min(overall, 95)
    rating = "High" if overall >= 90 else "Medium" if overall >= 75 else "Low"
    notes: List[str] = []
    if missing:
        notes.append("Missing material element(s): " + "; ".join(missing[:8]))
    if not complete_answer:
        notes.append("Answer completeness check failed.")
    if source_fidelity < 90:
        notes.append("Source wording could not be matched strongly to the stated source page.")
    if text_cleanliness < 85:
        notes.append("Extracted answer contains probable OCR or page-layout contamination and requires manual review.")
    if source_cleanliness < 85:
        notes.append("Source clause contains probable OCR or page-layout contamination; verify the cleaned obligation against the original PDF.")
    if page_data and page_data.get("method") == "ocr":
        notes.append(
            "OCR source page; accuracy is capped at 95% because exact character equivalence "
            f"to the PDF was not independently verified. Readability indicator {int(page_data.get('score') or 0)}."
        )
    if not notes:
        notes.append("Complete obligation is traceable to the source page and preserves detected material elements.")
    return {
        "Section": section,
        "Source Page": source_page,
        "Actionable": "Yes" if actionable else "No",
        "Document Accuracy %": overall,
        "Accuracy Rating": rating,
        "Source Fidelity %": source_fidelity,
        "Material Elements %": material_score,
        "Semantic Coverage %": token_score if actionable else 100,
        "Missing Material Elements": len(missing),
        "Answer Completeness %": answer_completeness,
        "OCR/Text Quality %": text_quality,
        "Source Cleanliness %": source_cleanliness,
        "Text Cleanliness %": text_cleanliness,
        "Manual Review Required": "Yes" if (
            missing
            or not complete_answer
            or source_cleanliness < 85
            or text_cleanliness < 85
            or source_fidelity < 75
        ) else "No",
        "Accuracy Notes": " ".join(notes),
    }


def _parent_context(rows: List[Dict[str, Any]], index: int) -> str:
    section = str(rows[index].get("Section", ""))
    if not re.fullmatch(r"\d+(?:\.\d+)+", section):
        return ""
    parent = section.rsplit(".", 1)[0]
    for prior in reversed(rows[:index]):
        if str(prior.get("Section")) == parent:
            return _clean(prior.get("Language from Directive"))
    return ""


def sanitize_breakdown_sources(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Clean every exported source row while preserving section order and hierarchy."""
    sanitized: List[Dict[str, Any]] = []
    for item in rows:
        row = dict(item)
        sanitized.append(row)
        parent = _parent_context(sanitized, len(sanitized) - 1)
        row["Language from Directive"] = sanitize_source_wording(
            row.get("Language from Directive", ""),
            parent,
        )
    return sanitized


def _residual_export_artifacts(rows: List[Dict[str, Any]]) -> List[str]:
    """Return sections that still contain high-confidence OCR/page artifacts."""
    failures: List[str] = []
    for row in rows:
        wording = _clean(row.get("Language from Directive"))
        if (
            OCR_NOISE_TOKEN_PATTERN.search(wording)
            or OCR_FOOTER_PATTERN.search(wording)
            or re.search(r"[\\{}]", wording)
        ):
            failures.append(str(row.get("Section", "Unknown")))
    return failures


def _fallback_row(section: str, wording: str, parent_context: str) -> Dict[str, str]:
    actionable = is_actionable(wording, parent_context)
    category = classify_category(f"{parent_context} {wording}")
    department, support = classify_department_support(f"{category} {parent_context} {wording}")
    return {
        "Section": section,
        "Language from Directive": wording,
        "Obligation": generate_obligation(section, wording, parent_context),
        "Obligation Category": category,
        "Primary Responsible Department": department,
        "Support Function": support,
        "Priority": priority(f"{parent_context} {wording}") if actionable else "Low",
        "Actionable": "Yes" if actionable else "No",
    }


def _structural_parent_row(section: str, wording: str) -> Dict[str, str]:
    category = classify_category(wording)
    department, support = classify_department_support(f"{category} {wording}")
    return {
        "Section": section,
        "Language from Directive": wording,
        "Obligation": "Parent clause only; the actionable requirements are captured in the numbered child clauses that follow.",
        "Obligation Category": category,
        "Primary Responsible Department": department,
        "Support Function": support,
        "Priority": "Low",
        "Actionable": "No",
    }


def _llm_rows(directive_name: str, section: str, wording: str, parent_context: str) -> List[Dict[str, str]]:
    if not _enabled("ENABLE_LLM_EXTRACTION"):
        return []
    result = chat_json(
        OBLIGATION_SYSTEM_PROMPT,
        obligation_user_prompt(directive_name, section, wording, parent_context),
    )
    candidates = result.get("obligations", []) if isinstance(result, dict) else []
    rows: List[Dict[str, str]] = []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        obligation = _clean(item.get("obligation"))
        deterministic_actionable = is_actionable(wording, parent_context)
        actionable = "Yes" if deterministic_actionable or str(item.get("actionable", "Yes")).strip().lower() in {"yes", "true", "1"} else "No"
        category = _clean(item.get("obligation_category"))
        if category not in allowed_categories():
            category = classify_category(f"{wording} {obligation}")
        department, support = validate_department_support(
            _clean(item.get("primary_responsible_department")),
            _clean(item.get("support_function")),
        )
        if not obligation:
            continue
        if deterministic_actionable and re.search(r"informational|contextual|no standalone", obligation, flags=re.I):
            obligation = generate_obligation(section, wording, parent_context)
        rows.append({
            "Section": section,
            "Language from Directive": wording,
            "Obligation": obligation,
            "Obligation Category": category,
            "Primary Responsible Department": department,
            "Support Function": support,
            "Priority": _clean(item.get("priority")) if _clean(item.get("priority")) in {"High", "Medium", "Low"} else priority(wording),
            "Actionable": actionable,
        })
    return rows


def _statistics_frame(df: pd.DataFrame) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for dimension, column in [
        ("Category", "Obligation Category"),
        ("Department", "Primary Responsible Department"),
        ("Priority", "Priority"),
        ("Actionable", "Actionable"),
        ("Accuracy Rating", "Accuracy Rating"),
    ]:
        counts = df[column].value_counts(dropna=False).rename_axis("Value").reset_index(name="Count")
        counts.insert(0, "Dimension", dimension)
        frames.append(counts)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["Dimension", "Value", "Count"])


def _write_excel(
    path: Path,
    obligations: pd.DataFrame,
    accuracy_review: pd.DataFrame,
    breakdown: pd.DataFrame,
    statistics: pd.DataFrame,
    logs: List[Dict[str, Any]],
) -> None:
    include_internal_quality = _enabled("EXPORT_INTERNAL_QUALITY_METRICS")
    exported_obligations = obligations if include_internal_quality else obligations.drop(
        columns=INTERNAL_OBLIGATION_EXPORT_COLUMNS,
        errors="ignore",
    )
    exported_statistics = statistics if include_internal_quality else statistics[
        statistics["Dimension"] != "Accuracy Rating"
    ]
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        sheets = {
            "Obligations": exported_obligations,
            "Text Breakdown": breakdown,
            "Statistics": exported_statistics,
            "Process Log": pd.DataFrame(logs),
        }
        if include_internal_quality:
            sheets = {
                "Obligations": exported_obligations,
                "Accuracy Review": accuracy_review,
                "Text Breakdown": breakdown,
                "Statistics": exported_statistics,
                "Process Log": pd.DataFrame(logs),
            }
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(len(frame), 1), max(len(frame.columns) - 1, 0))
            for index, column in enumerate(frame.columns):
                sample = [len(str(column)), *(len(str(value)) for value in frame[column].head(80))]
                sheet.set_column(index, index, min(max(sample) + 2, 60))


def extract_obligations_from_pdf(
    pdf_path: Path,
    input_mode: str = "direct-upload",
) -> Dict[str, Any]:
    raw_text, pages = extract_pdf_text(pdf_path)
    if len(_clean(raw_text)) < 80:
        raise ValueError("The PDF did not produce enough readable text for obligation extraction.")
    valid, validation_message, _validation = validate_directive_candidate(pdf_path, raw_text)
    if not valid:
        raise ValueError(validation_message)

    breakdown = sanitize_breakdown_sources(breakdown_regulatory_text(raw_text))
    residual_artifacts = _residual_export_artifacts(breakdown)
    if residual_artifacts:
        sections = ", ".join(residual_artifacts[:12])
        raise ValueError(
            "Extraction quality control blocked residual OCR/page artifacts in "
            f"source section(s): {sections}. Review the scanned PDF before export."
        )
    obligation_rows: List[Dict[str, str]] = []
    accuracy_rows: List[Dict[str, Any]] = []
    llm_row_count = 0
    for index, item in enumerate(breakdown):
        section = str(item["Section"])
        wording = _clean(item["Language from Directive"])
        parent = _parent_context(breakdown, index)
        child_prefix = f"{section}."
        has_children = bool(re.fullmatch(r"\d+(?:\.\d+)*", section)) and any(
            str(later.get("Section", "")).startswith(child_prefix)
            for later in breakdown[index + 1:index + 12]
        )
        if has_children and _is_structural_stem(wording):
            selected_rows = [_structural_parent_row(section, wording)]
        else:
            generated = _llm_rows(pdf_path.name, section, wording, parent)
            if generated:
                generated_checks = [
                    assess_obligation_accuracy(
                        section=section,
                        source_text=wording,
                        obligation=row["Obligation"],
                        actionable=row["Actionable"] == "Yes",
                        source_page=str(item.get("Page", "Unknown")),
                        pages=pages,
                        parent_context=parent,
                    )
                    for row in generated
                ]
                generated_is_complete = all(
                    check["Answer Completeness %"] == 100
                    and check["Material Elements %"] >= 85
                    for check in generated_checks
                )
                if generated_is_complete:
                    selected_rows = generated
                    llm_row_count += len(generated)
                else:
                    selected_rows = [_fallback_row(section, wording, parent)]
            else:
                selected_rows = [_fallback_row(section, wording, parent)]

        for row in selected_rows:
            accuracy = assess_obligation_accuracy(
                section=section,
                source_text=wording,
                obligation=row["Obligation"],
                actionable=row["Actionable"] == "Yes",
                source_page=str(item.get("Page", "Unknown")),
                pages=pages,
                parent_context=parent,
            )
            row["Source Page"] = accuracy["Source Page"]
            row["Document Accuracy %"] = accuracy["Document Accuracy %"]
            row["Accuracy Rating"] = accuracy["Accuracy Rating"]
            row["Accuracy Notes"] = accuracy["Accuracy Notes"]
            obligation_rows.append(row)
            accuracy_rows.append(accuracy)

    failed_completeness = [
        row for row in accuracy_rows
        if row["Actionable"] == "Yes"
        and (row["Answer Completeness %"] < 100 or row["Missing Material Elements"] > 0)
    ]
    if failed_completeness:
        sections = ", ".join(str(row["Section"]) for row in failed_completeness[:12])
        raise ValueError(
            "Extraction quality control blocked incomplete obligation answer(s). "
            f"Review source sections: {sections}."
        )

    df_obligations = pd.DataFrame(obligation_rows, columns=OBLIGATION_COLUMNS)
    df_accuracy = pd.DataFrame(accuracy_rows)
    df_breakdown = pd.DataFrame(breakdown)
    statistics = _statistics_frame(df_obligations)
    actionable_accuracy = df_accuracy[df_accuracy["Actionable"] == "Yes"]
    accuracy_population = actionable_accuracy if not actionable_accuracy.empty else df_accuracy
    overall_accuracy = int(round(float(accuracy_population["Document Accuracy %"].mean()))) if not accuracy_population.empty else 0
    high_confidence_count = int((accuracy_population["Accuracy Rating"] == "High").sum()) if not accuracy_population.empty else 0
    low_confidence_count = int((accuracy_population["Accuracy Rating"] == "Low").sum()) if not accuracy_population.empty else 0
    actionable_manual_review_count = int(
        (actionable_accuracy["Manual Review Required"] == "Yes").sum()
    ) if not actionable_accuracy.empty else 0
    all_manual_review_count = int(
        (df_accuracy["Manual Review Required"] == "Yes").sum()
    ) if not df_accuracy.empty else 0
    logs = [
        {
            "stage": "Pipeline",
            "status": "Completed",
            "message": (
                f"Obligation extraction pipeline {OBLIGATION_PIPELINE_VERSION}; "
                f"validated input mode: {input_mode}."
            ),
            "row_count": len(df_obligations),
        },
        {"stage": "Select PDF", "status": "Completed", "message": f"Loaded and validated {pdf_path.name}. {validation_message}", "row_count": 1},
        {"stage": "Breakdown", "status": "Completed", "message": extraction_summary(pages), "row_count": len(df_breakdown)},
        {"stage": "Extraction", "status": "Completed", "message": f"Generated the obligation register ({llm_row_count} AI-generated row(s); remaining rows used deterministic taxonomy rules).", "row_count": len(df_obligations)},
        {
            "stage": "Quality Control",
            "status": "Completed",
            "message": (
                f"Validated {len(df_accuracy)} extracted row(s) against source pages; "
                f"{actionable_manual_review_count} actionable row(s) and "
                f"{all_manual_review_count} total row(s) require manual review. "
                "AI-generated output must be approved by a qualified compliance professional before use."
            ),
            "row_count": len(df_accuracy),
        },
        {"stage": "Results", "status": "Completed", "message": "Generated Excel and CSV outputs.", "row_count": len(df_obligations)},
    ]

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", pdf_path.stem).strip("_")
    excel_path = output_path(f"{stem}_obligation_extraction.xlsx")
    csv_path = output_path(f"{stem}_obligation_extraction.csv")
    _write_excel(excel_path, df_obligations, df_accuracy, df_breakdown, statistics, logs)
    csv_obligations = df_obligations if _enabled("EXPORT_INTERNAL_QUALITY_METRICS") else df_obligations.drop(
        columns=INTERNAL_OBLIGATION_EXPORT_COLUMNS,
        errors="ignore",
    )
    csv_obligations.to_csv(csv_path, index=False)

    category_stats = statistics[statistics["Dimension"] == "Category"][["Value", "Count"]].rename(columns={"Value": "Category"})
    department_stats = statistics[statistics["Dimension"] == "Department"][["Value", "Count"]].rename(columns={"Value": "Department"})
    priority_stats = statistics[statistics["Dimension"] == "Priority"][["Value", "Count"]].rename(columns={"Value": "Priority"})
    actionable_stats = statistics[statistics["Dimension"] == "Actionable"][["Value", "Count"]].rename(columns={"Value": "Actionable"})
    return {
        "kpis": [
            {"label": "Total Sections", "value": len(df_breakdown)},
            {"label": "Actionable Obligations", "value": int((df_obligations["Actionable"] == "Yes").sum())},
            {"label": "Categories", "value": int(df_obligations["Obligation Category"].nunique())},
            {"label": "Departments", "value": int(df_obligations["Primary Responsible Department"].nunique())},
            {"label": "High-priority Obligations", "value": int((df_obligations["Priority"] == "High").sum())},
            {"label": "Actionable Review Rows", "value": actionable_manual_review_count},
        ],
        "tabs": {
            "obligations": df_obligations.to_dict(orient="records"),
            "accuracy_review": df_accuracy.to_dict(orient="records"),
            "text_breakdown": df_breakdown.to_dict(orient="records"),
            "statistics": {
                "category": category_stats.to_dict(orient="records"),
                "department": department_stats.to_dict(orient="records"),
                "priority": priority_stats.to_dict(orient="records"),
                "actionable": actionable_stats.to_dict(orient="records"),
            },
            "process_log": logs,
        },
        "logs": logs,
        "extraction_pipeline": {
            "pipeline_version": OBLIGATION_PIPELINE_VERSION,
            "input_mode": input_mode,
            "crawler_enabled": True,
        },
        "accuracy": {
            "overall_percentage": overall_accuracy,
            "rating": "High" if overall_accuracy >= 90 else "Medium" if overall_accuracy >= 75 else "Low",
            "population": "actionable obligations" if not actionable_accuracy.empty else "all extracted rows",
            "high_confidence_rows": high_confidence_count,
            "low_confidence_rows": low_confidence_count,
            "manual_review_rows": actionable_manual_review_count,
            "actionable_manual_review_rows": actionable_manual_review_count,
            "all_manual_review_rows": all_manual_review_count,
            "method": ACCURACY_METHOD,
        },
        "output_files": {"excel": excel_path.name, "csv": csv_path.name},
        "output_profile": "internal-quality" if _enabled("EXPORT_INTERNAL_QUALITY_METRICS") else "client-safe",
    }
