from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from rapidfuzz import fuzz

from app.services.llm_service import chat_json
from app.services.pdf_service import extract_pdf_text, extraction_summary
from app.services.prompt_service import GAP_REVIEW_SYSTEM_PROMPT, gap_review_user_prompt
from app.services.storage import output_path


REQUIRED_REGISTER_COLUMNS = [
    "Section",
    "Language from Directive",
    "Obligation",
    "Obligation Category",
    "Primary Responsible Department",
    "Support Function",
]
OPTIONAL_REGISTER_COLUMNS = ["Priority", "Actionable"]
GAP_COLUMNS = REQUIRED_REGISTER_COLUMNS + [
    "Coverage Status",
    "Review Rationale",
    "Policy Gap and Recommendations",
    "Policy Page",
    "Corresponding Policy Text",
    "Priority",
]
VALID_STATUSES = ("Completely Covered", "Partially Covered", "Completely Missing")

NEGATIVE_PHRASES = (
    "does not currently require",
    "does not yet require",
    "not defined",
    "not mandatory",
    "not required",
    "no requirement",
    "not documented",
)
STOPWORDS = {
    "this", "that", "with", "from", "shall", "must", "have", "will", "they", "their", "there",
    "under", "section", "directive", "institution", "regulatory", "obligation", "material", "policy",
    "business", "function", "functions", "entity", "insurer", "insurers", "regulated", "requirement",
    "provision", "applies", "applicability", "scope", "respect", "aspects",
}
PAGE_SPLIT = re.compile(r"(?:^|\n+)--- Page\s+(\d+)(?:\s*\|[^\n-]*)?\s*---\n", flags=re.I)
FOREIGN_JURISDICTIONS = re.compile(
    r"\b(saudi arabia|uae|united arab emirates|insurance authority|sama|central bank of saudi)\b",
    flags=re.I,
)
SOUTH_AFRICA_TERMS = re.compile(
    r"\b(south africa|south african|fsca|financial sector conduct authority|financial services board|fsb)\b",
    flags=re.I,
)

OCR_REPAIRS = (
    (re.compile(r"\bAninsurer\b", re.I), "An insurer"),
    (re.compile(r"\bA-written\b", re.I), "A written"),
    (re.compile(r"\bregulatary\b", re.I), "regulatory"),
    (re.compile(r"\bcperations\b", re.I), "operations"),
    (re.compile(r"\bcontre!\s+function\b", re.I), "control function"),
    (re.compile(r"\breferrec\b", re.I), "referred"),
    (re.compile(r"\bOctcber\b", re.I), "October"),
    (re.compile(r"\bAsscciation\b", re.I), "Association"),
    (re.compile(r"\bgovemed\b", re.I), "governed"),
    (re.compile(r"\bgovemance\b", re.I), "governance"),
    (re.compile(r"\bprior te\b", re.I), "prior to"),
    (re.compile(r"\brelating ta\b", re.I), "relating to"),
    (re.compile(r"\bpersen te\b", re.I), "person to"),
)


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern, replacement in OCR_REPAIRS:
        text = pattern.sub(replacement, text)
    return text


def load_register(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        workbook = pd.ExcelFile(path)
        preferred = next((name for name in workbook.sheet_names if name.strip().lower() == "obligations"), workbook.sheet_names[0])
        df = pd.read_excel(path, sheet_name=preferred)
    else:
        df = pd.read_csv(path)
    df.columns = [str(column).strip() for column in df.columns]
    missing = [column for column in REQUIRED_REGISTER_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    columns = REQUIRED_REGISTER_COLUMNS + [column for column in OPTIONAL_REGISTER_COLUMNS if column in df.columns]
    result = df[columns].fillna("").copy()
    if result.empty:
        raise ValueError("The obligation register contains no data rows.")
    for column in ("Language from Directive", "Obligation"):
        result[column] = result[column].map(_clean)
    return result


def chunk_policy_text(raw_text: str, max_chars: int = 1800) -> List[Dict[str, str]]:
    """Split policy text into page-aware evidence chunks."""
    parts = PAGE_SPLIT.split(raw_text.strip())
    page_sections: List[Tuple[str, str]] = []
    if len(parts) > 1:
        for index in range(1, len(parts), 2):
            if index + 1 < len(parts):
                page_sections.append((parts[index], parts[index + 1]))
    else:
        page_sections.append(("Unknown", raw_text))

    chunks: List[Dict[str, str]] = []
    for page, page_text in page_sections:
        cleaned = re.sub(r"[ \t]+", " ", page_text.replace("\r", "\n"))
        starts = list(re.finditer(r"(?m)^\s*(?:\d+(?:\.\d+)*\.?|[A-Z][A-Z /&-]{3,})\s+", cleaned))
        boundaries = sorted(set([0, *(match.start() for match in starts), len(cleaned)]))
        for start, end in zip(boundaries, boundaries[1:]):
            section = cleaned[start:end].strip()
            if not section:
                continue
            for part_start in range(0, len(section), max_chars):
                text = section[part_start:part_start + max_chars].strip()
                if text:
                    chunks.append({"page": str(page), "text": text})
    if not chunks and raw_text.strip():
        chunks.append({"page": "Unknown", "text": raw_text.strip()[:max_chars]})
    return chunks


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z-]{3,}", text.lower())
    output: List[str] = []
    for word in words:
        if word not in STOPWORDS and word not in output:
            output.append(word)
    return output


def _contains_negative(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in NEGATIVE_PHRASES)


def _is_informational(row: pd.Series) -> bool:
    if str(row.get("Actionable", "")).strip().lower() in {"no", "false", "0"}:
        return True
    combined = f"{row.get('Obligation', '')} {row.get('Language from Directive', '')}".lower()
    return any(phrase in combined for phrase in (
        "informational or contextual",
        "contextual background",
        "does not create",
        "no standalone implementation obligation",
        "no direct implementation obligation",
    ))


def _is_structural_parent(register: pd.DataFrame, index: int) -> bool:
    section = str(register.iloc[index].get("Section", "")).strip()
    text = _clean(register.iloc[index].get("Language from Directive", ""))
    if not section or not re.fullmatch(r"\d+(?:\.\d+)*", section):
        return False
    unfinished = bool(
        re.search(r"(?:at\s+least|following|as\s+follows)\s*[-—:]?\.?$", text, flags=re.I)
        or re.search(r"\b(?:must|shall)\b.{0,100}[-—:]\.?$", text, flags=re.I)
        or re.search(r"\b(?:applies|apply)\s+to\s*[~\-—:]?\.?$", text, flags=re.I)
        or re.search(r"\b(?:must|shall)\b.{0,300}\b(?:of|following|least)\s*[-—~]", text, flags=re.I)
    )
    if not unfinished:
        return False
    child_prefix = f"{section}."
    return any(str(register.iloc[next_index].get("Section", "")).startswith(child_prefix) for next_index in range(index + 1, min(index + 12, len(register))))


def rank_policy_matches(obligation: str, directive_text: str, chunks: List[Dict[str, str]], limit: int = 3) -> List[Dict[str, Any]]:
    search_text = f"{obligation} {directive_text}"
    terms = _keywords(search_text)[:45]
    ranked: List[Dict[str, Any]] = []
    for chunk_index, chunk in enumerate(chunks):
        text = chunk["text"]
        lowered = text.lower()
        hits = [term for term in terms if term in lowered]
        keyword_score = len(hits) / max(len(terms), 1)
        fuzzy_score = fuzz.token_set_ratio(search_text[:1200], text[:1800]) / 100
        score = (keyword_score * 0.72) + (fuzzy_score * 0.28)
        ranked.append({
            "candidate_id": f"candidate-{chunk_index + 1}",
            "page": chunk["page"],
            "text": text,
            "score": score,
            "keyword_score": keyword_score,
            "hits": hits,
        })
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[: max(limit, 1)]


def best_policy_match(obligation: str, directive_text: str, chunks: List[Dict[str, str]]) -> Tuple[float, Dict[str, str], float, List[str]]:
    """Backward-compatible best-match helper used by tests and integrations."""
    ranked = rank_policy_matches(obligation, directive_text, chunks, 1)
    if not ranked:
        return 0.0, {"page": "", "text": ""}, 0.0, _keywords(f"{obligation} {directive_text}")
    best = ranked[0]
    terms = _keywords(f"{obligation} {directive_text}")[:45]
    return best["score"], {"page": best["page"], "text": best["text"]}, best["keyword_score"], [term for term in terms if term not in best["text"].lower()]


def _requires_sa_jurisdiction(directive_text: str, obligation: str) -> bool:
    combined = f"{directive_text} {obligation}"
    return bool(re.search(
        r"\b(FSCA|FSB|Registrar|South Africa|this Directive|the Acts|appl(?:y|ies)|reinsurer|statutory actuary|appointed auditors?)\b",
        combined,
        flags=re.I,
    ))


def _jurisdiction_mismatch(directive_text: str, obligation: str, evidence: str) -> bool:
    return _requires_sa_jurisdiction(directive_text, obligation) and bool(FOREIGN_JURISDICTIONS.search(evidence)) and not bool(SOUTH_AFRICA_TERMS.search(evidence))


def coverage_status(score: float, keyword_score: float, chunk_text: str, jurisdiction_mismatch: bool = False) -> str:
    if not chunk_text:
        return "Completely Missing"
    if _contains_negative(chunk_text):
        return "Partially Covered" if keyword_score >= 0.22 else "Completely Missing"
    if jurisdiction_mismatch:
        return "Partially Covered" if score >= 0.24 or keyword_score >= 0.15 else "Completely Missing"
    if score >= 0.58 and keyword_score >= 0.38:
        return "Completely Covered"
    if score >= 0.28 or keyword_score >= 0.18:
        return "Partially Covered"
    return "Completely Missing"


def _material_gaps(directive_text: str, obligation: str, evidence: str) -> List[str]:
    required = f"{directive_text} {obligation}".lower()
    present = evidence.lower()
    gaps: List[str] = []
    rules = [
        (r"\bappl(?:y|ies|icability)\b|\bscope\b", r"\bappl(?:y|ies|icability)\b|\bscope\b", "the directive's defined scope and applicability"),
        (r"all aspects? of (?:the )?insurance business", r"all aspects? of (?:the )?insurance business", "coverage of all outsourced aspects of the insurance business"),
        (r"does not apply to intermediary services", r"does not apply to intermediary services", "the exclusion for intermediary services"),
        (r"\brelated part(?:y|ies)\b|\binter-related\b", r"\brelated part(?:y|ies)\b|\binter-related\b", "application to related and inter-related parties"),
        (r"\breinsur(?:er|ance)\b", r"\breinsur(?:er|ance)\b", "application to relevant reinsurance arrangements"),
        (r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b", r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b", "the required regulatory notification or reporting duty"),
        (r"\bboard\b.{0,50}\bapprov|\bapprov(?:al|e|ed)\b", r"\bboard\b.{0,50}\bapprov|\bapprov(?:al|e|ed)\b", "the required approval"),
        (r"\bmonitor\b|\breview\b", r"\bmonitor\b|\breview\b", "ongoing monitoring and review"),
        (r"\brecord\b|\bretain\b|\bdocument", r"\brecord\b|\bretain\b|\bdocument", "documented evidence and record retention"),
        (r"\bno later than\b|\bwithin\s+\d+|\bprior to\b|\bimmediately\b|\bmonthly\b|\bquarterly\b|\bannually\b", r"\bno later than\b|\bwithin\s+\d+|\bprior to\b|\bimmediately\b|\bmonthly\b|\bquarterly\b|\bannually\b", "the specified timing or frequency"),
        (r"\bmay not\b|\bmust not\b|\bdoes not apply\b|\bprohibit", r"\bmay not\b|\bmust not\b|\bdoes not apply\b|\bprohibit", "the stated prohibition or exception"),
    ]
    for required_pattern, evidence_pattern, label in rules:
        if re.search(required_pattern, required, flags=re.I) and not re.search(evidence_pattern, present, flags=re.I):
            gaps.append(label)
    if _jurisdiction_mismatch(directive_text, obligation, evidence):
        gaps.insert(0, "express application to South African operations and the FSCA/FSB framework")
    return list(dict.fromkeys(gaps))


def _draft_policy_requirement(section: str, directive_text: str, obligation: str) -> str:
    combined = f"{directive_text} {obligation}".lower()
    clean_obligation = _clean(obligation).rstrip(" .-—")
    clean_obligation = re.sub(r"^The regulated entity must comply with (?:this requirement|this applicability and scope provision):\s*", "", clean_obligation, flags=re.I)
    if section == "9.2" or "1 january 2013" in combined:
        return (
            "Perform and document a legacy-contract review for South African outsourcing arrangements entered into before Directive 159 took effect. "
            "Confirm that each arrangement was brought into compliance when extended, renewed or amended, record any historical exception, and remediate any surviving non-compliant contract."
        )
    if "intermediary services" in combined and "all aspects" in combined:
        return "For its South African insurance operations, the policy must apply Directive 159 to every outsourced aspect of the insurer's insurance business while expressly excluding intermediary services from this scope."
    if re.search(r"\bnotify\b|\bnotification\b|\breport\b|\bsubmit\b", combined):
        return f"Add a South African regulatory-reporting clause for section {section} requiring the insurer to {clean_obligation[:420]}, naming the FSCA/Registrar, the responsible function, the deadline, escalation route, and evidence of submission."
    if re.search(r"\bcontract\b|\bagreement\b", combined):
        return f"Amend the outsourcing-contract standard for South African operations so every relevant agreement expressly requires the following section {section} control: {clean_obligation[:420]}."
    if re.search(r"\bpolicy\b", combined):
        return f"Revise the South African outsourcing policy to state the section {section} requirement directly: {clean_obligation[:440]}."
    return f"Add a South African FSCA compliance clause for section {section} stating that the insurer must {clean_obligation[:440]}."


def recommendation_for(
    status: str,
    obligation: str,
    missing_terms: List[str] | None = None,
    negative_evidence: bool = False,
    *,
    section: str = "",
    directive_text: str = "",
    evidence: str = "",
) -> str:
    """Generate a useful deterministic fallback without raw keyword lists."""
    if status == "Completely Covered":
        return ""
    requirement = _draft_policy_requirement(section or "the relevant", directive_text, obligation)
    if status == "Completely Missing":
        return requirement
    gaps = _material_gaps(directive_text, obligation, evidence)
    if negative_evidence:
        gaps.insert(0, "removal of policy wording that makes the requirement optional or unavailable")
    gap_text = "; ".join(gaps[:4])
    if gap_text:
        return f"The cited policy text addresses the general subject but does not establish {gap_text}. {requirement}"
    return f"The cited policy wording is relevant but is not specific enough to demonstrate full compliance with section {section}. {requirement}"


def _evidence_excerpt(text: str, obligation: str, max_chars: int = 700) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    terms = _keywords(obligation)
    lowered = cleaned.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(min(positions) - 100, 0) if positions else 0
    excerpt = cleaned[start:start + max_chars].strip()
    return ("…" if start else "") + excerpt + ("…" if start + max_chars < len(cleaned) else "")


def _normalised_contains(container: str, quote: str) -> bool:
    normal_container = re.sub(r"\s+", " ", container).strip().lower()
    normal_quote = re.sub(r"\s+", " ", quote).strip().lower()
    return bool(normal_quote) and normal_quote in normal_container


def _gemini_assessments(tasks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not _enabled("ENABLE_LLM_GAP_REVIEW") or not tasks:
        return {}
    batch_size = max(1, min(int(os.getenv("GAP_REVIEW_BATCH_SIZE", "5")), 10))
    assessments: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start:start + batch_size]
        prompt_items = []
        for task in batch:
            prompt_items.append({
                "id": task["id"],
                "section": task["section"],
                "directive_language": task["directive_text"],
                "obligation": task["obligation"],
                "obligation_category": task["category"],
                "candidate_policy_evidence": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "page": candidate["page"],
                        "text": candidate["text"][:1400],
                    }
                    for candidate in task["candidates"]
                ],
            })
        result = chat_json(GAP_REVIEW_SYSTEM_PROMPT, gap_review_user_prompt(prompt_items))
        returned = result.get("assessments", []) if isinstance(result, dict) else []
        if not isinstance(returned, list):
            continue
        valid_ids = {task["id"] for task in batch}
        for assessment in returned:
            if not isinstance(assessment, dict) or str(assessment.get("id", "")) not in valid_ids:
                continue
            assessments[str(assessment["id"])] = assessment
    return assessments


def _apply_gemini_assessment(task: Dict[str, Any], assessment: Dict[str, Any]) -> Dict[str, str] | None:
    status = _clean(assessment.get("coverage_status"))
    if status not in VALID_STATUSES:
        return None
    candidate_id = _clean(assessment.get("candidate_id"))
    candidate = next((item for item in task["candidates"] if item["candidate_id"] == candidate_id), None)
    if status == "Completely Missing":
        candidate = None
    elif candidate is None:
        return None

    evidence = ""
    page = ""
    if candidate:
        quote = _clean(assessment.get("evidence_quote"))
        evidence = quote if _normalised_contains(candidate["text"], quote) else _evidence_excerpt(candidate["text"], task["obligation"])
        page = str(candidate["page"])
        if _jurisdiction_mismatch(task["directive_text"], task["obligation"], candidate["text"]) and status == "Completely Covered":
            status = "Partially Covered"
        if status == "Completely Covered" and _material_gaps(task["directive_text"], task["obligation"], candidate["text"]):
            status = "Partially Covered"

    recommendation = _clean(assessment.get("recommendation"))
    if status == "Completely Covered":
        recommendation = ""
    elif not recommendation or re.search(r"explicitly address the missing elements", recommendation, flags=re.I):
        recommendation = recommendation_for(
            status,
            task["obligation"],
            section=task["section"],
            directive_text=task["directive_text"],
            evidence=candidate["text"] if candidate else "",
        )
    return {
        "status": status,
        "rationale": _clean(assessment.get("rationale")) or "Gemini assessed the obligation against the selected policy evidence.",
        "recommendation": recommendation,
        "page": page,
        "evidence": evidence,
    }


def _fallback_assessment(task: Dict[str, Any]) -> Dict[str, str]:
    candidate = task["candidates"][0] if task["candidates"] else None
    if not candidate:
        status = "Completely Missing"
        evidence_text = ""
    else:
        evidence_text = candidate["text"]
        status = coverage_status(
            candidate["score"],
            candidate["keyword_score"],
            evidence_text,
            _jurisdiction_mismatch(task["directive_text"], task["obligation"], evidence_text),
        )
    gaps = _material_gaps(task["directive_text"], task["obligation"], evidence_text)
    if status == "Completely Covered":
        rationale = "The selected policy evidence directly covers the material elements of the obligation."
    elif status == "Partially Covered":
        rationale = "Relevant policy language was found, but full coverage is not demonstrated"
        if gaps:
            rationale += f" because it omits {', '.join(gaps[:3])}"
        rationale += "."
    else:
        rationale = "No policy evidence directly establishes this South African FSCA requirement."
    return {
        "status": status,
        "rationale": rationale,
        "recommendation": recommendation_for(
            status,
            task["obligation"],
            negative_evidence=_contains_negative(evidence_text),
            section=task["section"],
            directive_text=task["directive_text"],
            evidence=evidence_text,
        ),
        "page": str(candidate["page"]) if candidate and status != "Completely Missing" else "",
        "evidence": _evidence_excerpt(evidence_text, task["obligation"]) if candidate and status != "Completely Missing" else "",
    }


def _priority(status: str, existing: str) -> str:
    if status == "Completely Missing":
        return "High"
    if status == "Partially Covered":
        return "Medium"
    return existing if existing in {"High", "Medium", "Low"} else "Low"


def _statistics_frame(df_gap: pd.DataFrame) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for dimension, column in [
        ("Status", "Coverage Status"),
        ("Category", "Obligation Category"),
        ("Department", "Primary Responsible Department"),
        ("Priority", "Priority"),
    ]:
        counts = df_gap.groupby([column, "Coverage Status"]).size().reset_index(name="Count") if column != "Coverage Status" else df_gap[column].value_counts().rename_axis("Coverage Status").reset_index(name="Count")
        counts.insert(0, "Dimension", dimension)
        if column != "Coverage Status":
            counts = counts.rename(columns={column: "Value"})
        else:
            counts.insert(1, "Value", counts["Coverage Status"])
        frames.append(counts)
    return pd.concat(frames, ignore_index=True, sort=False).fillna("")


def _write_excel(path: Path, assessment: pd.DataFrame, statistics: pd.DataFrame, logs: List[Dict[str, Any]], method: str) -> None:
    status_counts = assessment["Coverage Status"].value_counts()
    top_gaps = (
        assessment[assessment["Coverage Status"] != "Completely Covered"]
        .assign(
            _priority_rank=assessment["Priority"].map({"High": 0, "Medium": 1, "Low": 2}).fillna(3),
            _status_rank=assessment["Coverage Status"].map({"Completely Missing": 0, "Partially Covered": 1}).fillna(2),
        )
        .sort_values(["_priority_rank", "_status_rank", "Section"])
        [["Section", "Obligation", "Coverage Status", "Policy Gap and Recommendations", "Priority"]]
        .head(12)
    )

    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        workbook = writer.book
        dark = "#1F2937"
        gold = "#F2C811"
        header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": dark, "border": 1, "text_wrap": True, "valign": "vcenter"})
        title_format = workbook.add_format({"bold": True, "font_size": 20, "font_color": dark})
        subtitle_format = workbook.add_format({"font_size": 10, "font_color": "#4B5563", "text_wrap": True})
        kpi_label = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": dark, "align": "center", "border": 1})
        kpi_value = workbook.add_format({"bold": True, "font_size": 18, "bg_color": gold, "align": "center", "border": 1})
        wrap = workbook.add_format({"text_wrap": True, "valign": "top"})

        summary = workbook.add_worksheet("Executive Summary")
        writer.sheets["Executive Summary"] = summary
        summary.hide_gridlines(2)
        summary.merge_range("A1:H2", "FSCA Directive 159 Policy Gap Assessment", title_format)
        summary.merge_range("A3:H4", f"Evidence-grounded review method: {method}. Results support compliance review and require professional approval before implementation.", subtitle_format)
        labels = ["Total Obligations", "Completely Covered", "Partially Covered", "Completely Missing"]
        values = [len(assessment), int(status_counts.get("Completely Covered", 0)), int(status_counts.get("Partially Covered", 0)), int(status_counts.get("Completely Missing", 0))]
        for index, (label, value) in enumerate(zip(labels, values)):
            column = index * 2
            summary.merge_range(5, column, 5, column + 1, label, kpi_label)
            summary.merge_range(6, column, 7, column + 1, value, kpi_value)
        summary.merge_range("A10:E10", "Highest-priority gaps requiring review", header_format)
        top_gaps.to_excel(writer, sheet_name="Executive Summary", startrow=10, index=False)
        for col, name in enumerate(top_gaps.columns):
            summary.write(10, col, name, header_format)
        summary.set_column("A:A", 12)
        summary.set_column("B:B", 48, wrap)
        summary.set_column("C:C", 22)
        summary.set_column("D:D", 62, wrap)
        summary.set_column("E:E", 12)
        summary.set_column("F:H", 14)
        summary.set_default_row(18)
        summary.set_row(0, 26)
        summary.set_row(2, 34)
        for row_index in range(11, 11 + len(top_gaps)):
            summary.set_row(row_index, 58)
        summary.autofilter(10, 0, 10 + len(top_gaps), len(top_gaps.columns) - 1)
        summary.freeze_panes(11, 0)
        summary.conditional_format(11, 2, 10 + len(top_gaps), 2, {"type": "text", "criteria": "containing", "value": "Completely Missing", "format": workbook.add_format({"bg_color": "#FECACA", "font_color": "#991B1B"})})

        assessment.to_excel(writer, sheet_name="Gap Assessment", index=False)
        gap_sheet = writer.sheets["Gap Assessment"]
        gap_sheet.hide_gridlines(2)
        gap_sheet.freeze_panes(1, 2)
        gap_sheet.autofilter(0, 0, len(assessment), len(assessment.columns) - 1)
        gap_sheet.set_row(0, 34)
        widths = {
            "Section": 11,
            "Language from Directive": 52,
            "Obligation": 58,
            "Obligation Category": 24,
            "Primary Responsible Department": 24,
            "Support Function": 24,
            "Coverage Status": 22,
            "Review Rationale": 44,
            "Policy Gap and Recommendations": 64,
            "Policy Page": 11,
            "Corresponding Policy Text": 64,
            "Priority": 11,
        }
        for index, column in enumerate(assessment.columns):
            gap_sheet.write(0, index, column, header_format)
            gap_sheet.set_column(index, index, widths.get(column, 20), wrap)
        narrative_columns = [
            "Language from Directive", "Obligation", "Review Rationale",
            "Policy Gap and Recommendations", "Corresponding Policy Text",
        ]
        for row_index, (_, data_row) in enumerate(assessment.iterrows(), start=1):
            estimated_lines = max(
                1,
                *(
                    (len(str(data_row.get(column, ""))) // max(widths[column] - 4, 10)) + 1
                    for column in narrative_columns
                ),
            )
            gap_sheet.set_row(row_index, min(max(30, estimated_lines * 15), 180))
        status_column = assessment.columns.get_loc("Coverage Status")
        status_formats = {
            "Completely Covered": workbook.add_format({"bg_color": "#DCFCE7", "font_color": "#166534", "bold": True}),
            "Partially Covered": workbook.add_format({"bg_color": "#FEF3C7", "font_color": "#92400E", "bold": True}),
            "Completely Missing": workbook.add_format({"bg_color": "#FEE2E2", "font_color": "#991B1B", "bold": True}),
        }
        for status, cell_format in status_formats.items():
            gap_sheet.conditional_format(1, status_column, len(assessment), status_column, {"type": "text", "criteria": "containing", "value": status, "format": cell_format})

        statistics.to_excel(writer, sheet_name="Statistics", index=False)
        stat_sheet = writer.sheets["Statistics"]
        stat_sheet.hide_gridlines(2)
        stat_sheet.freeze_panes(1, 0)
        stat_sheet.autofilter(0, 0, len(statistics), len(statistics.columns) - 1)
        stat_sheet.set_column("A:C", 28)
        stat_sheet.set_column("D:D", 12)
        for index, column in enumerate(statistics.columns):
            stat_sheet.write(0, index, column, header_format)

        pd.DataFrame(logs).to_excel(writer, sheet_name="Process Log", index=False)
        log_sheet = writer.sheets["Process Log"]
        log_sheet.hide_gridlines(2)
        log_sheet.freeze_panes(1, 0)
        log_sheet.set_column("A:B", 20)
        log_sheet.set_column("C:C", 90, wrap)
        log_sheet.set_column("D:D", 12)
        for index, column in enumerate(pd.DataFrame(logs).columns):
            log_sheet.write(0, index, column, header_format)


def review_policy_gaps(register_path: Path, policy_path: Path) -> Dict[str, Any]:
    register = load_register(register_path)
    policy_text, pages = extract_pdf_text(policy_path)
    if len(re.sub(r"\s+", "", policy_text)) < 50:
        raise ValueError("Could not extract readable text from the uploaded policy PDF.")
    chunks = chunk_policy_text(policy_text)
    candidate_limit = max(1, min(int(os.getenv("GAP_REVIEW_CANDIDATES", "3")), 5))

    prepared: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    for index, (_, row) in enumerate(register.iterrows()):
        section = str(row["Section"])
        directive_text = _clean(row["Language from Directive"])
        obligation = _clean(row["Obligation"])
        base = {"index": index, "row": row, "section": section, "directive_text": directive_text, "obligation": obligation}
        if _is_informational(row):
            base["special"] = {
                "status": "Completely Covered",
                "rationale": "Informational or contextual directive text; no standalone policy requirement is assessed.",
                "recommendation": "Informational item only; no policy amendment is required.",
                "page": "",
                "evidence": "",
            }
        elif _is_structural_parent(register, index):
            base["special"] = {
                "status": "Completely Covered",
                "rationale": "This is an unfinished parent stem; its substantive requirements are assessed in the child clauses that follow.",
                "recommendation": "Parent clause only; review the separately assessed child requirements.",
                "page": "",
                "evidence": "",
            }
        else:
            task = {
                "id": f"row-{index}",
                "section": section,
                "directive_text": directive_text,
                "obligation": obligation,
                "category": str(row["Obligation Category"]),
                "candidates": rank_policy_matches(obligation, directive_text, chunks, candidate_limit),
            }
            base["task"] = task
            tasks.append(task)
        prepared.append(base)

    gemini_results = _gemini_assessments(tasks)
    rows: List[Dict[str, str]] = []
    gemini_count = 0
    for item in prepared:
        row = item["row"]
        if "special" in item:
            assessment = item["special"]
        else:
            task = item["task"]
            assessment = _apply_gemini_assessment(task, gemini_results.get(task["id"], {})) if task["id"] in gemini_results else None
            if assessment:
                gemini_count += 1
            else:
                assessment = _fallback_assessment(task)
        status = assessment["status"]
        existing_priority = str(row.get("Priority", ""))
        rows.append({
            "Section": item["section"],
            "Language from Directive": item["directive_text"],
            "Obligation": item["obligation"],
            "Obligation Category": row["Obligation Category"],
            "Primary Responsible Department": row["Primary Responsible Department"],
            "Support Function": row["Support Function"],
            "Coverage Status": status,
            "Review Rationale": assessment["rationale"],
            "Policy Gap and Recommendations": assessment["recommendation"],
            "Policy Page": assessment["page"] if status != "Completely Missing" else "",
            "Corresponding Policy Text": assessment["evidence"] if status != "Completely Missing" else "",
            "Priority": _priority(status, existing_priority),
        })

    df_gap = pd.DataFrame(rows, columns=GAP_COLUMNS)
    if not set(df_gap["Coverage Status"]).issubset(VALID_STATUSES):
        raise RuntimeError("Gap analysis produced an unsupported coverage status.")
    missing_rows = df_gap["Coverage Status"] == "Completely Missing"
    if (df_gap.loc[missing_rows, ["Policy Page", "Corresponding Policy Text"]].astype(bool).any(axis=None)):
        raise RuntimeError("Missing obligations must not contain fabricated policy evidence.")

    statistics = _statistics_frame(df_gap)
    method = "Gemini-assisted evidence review with deterministic validation" if gemini_count else "deterministic evidence review (Gemini unavailable or disabled)"
    logs = [
        {"stage": "Select Inputs", "status": "Completed", "message": f"Validated register columns and loaded {policy_path.name}.", "row_count": len(register)},
        {"stage": "Evidence Retrieval", "status": "Completed", "message": f"Selected up to {candidate_limit} page-aware evidence candidates for each actionable obligation. {extraction_summary(pages)}", "row_count": len(tasks)},
        {"stage": "Gap Analysis", "status": "Completed", "message": f"Completed {method}. Gemini produced {gemini_count} validated assessment(s); remaining rows used the jurisdiction-aware fallback.", "row_count": len(df_gap)},
        {"stage": "Results", "status": "Completed", "message": "Generated executive summary, detailed assessment, statistics, Excel, and CSV outputs.", "row_count": len(df_gap)},
    ]

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", register_path.stem).strip("_")
    excel_path = output_path(f"{stem}_policy_gap_assessment.xlsx")
    csv_path = output_path(f"{stem}_policy_gap_assessment.csv")
    _write_excel(excel_path, df_gap, statistics, logs, method)
    df_gap.to_csv(csv_path, index=False)

    status_counts = df_gap["Coverage Status"].value_counts()
    kpis = [
        {"label": "Total Obligations", "value": len(df_gap)},
        {"label": "Completely Covered", "value": int(status_counts.get("Completely Covered", 0))},
        {"label": "Partially Covered", "value": int(status_counts.get("Partially Covered", 0))},
        {"label": "Completely Missing", "value": int(status_counts.get("Completely Missing", 0))},
    ]
    assert sum(int(item["value"]) for item in kpis[1:]) == len(df_gap)

    def stat_rows(dimension: str) -> List[Dict[str, Any]]:
        return statistics[statistics["Dimension"] == dimension].drop(columns=["Dimension"]).to_dict(orient="records")

    return {
        "kpis": kpis,
        "tabs": {
            "gap_assessment": df_gap.to_dict(orient="records"),
            "statistics": {
                "status": stat_rows("Status"),
                "category": stat_rows("Category"),
                "department": stat_rows("Department"),
                "priority": stat_rows("Priority"),
            },
            "process_log": logs,
        },
        "logs": logs,
        "output_files": {"excel": excel_path.name, "csv": csv_path.name},
    }
