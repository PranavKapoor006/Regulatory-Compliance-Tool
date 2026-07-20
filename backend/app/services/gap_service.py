from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from rapidfuzz import fuzz

from app.services.pdf_service import extract_pdf_text, extraction_summary
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
}
PAGE_SPLIT = re.compile(r"(?:^|\n+)--- Page\s+(\d+)(?:\s*\|[^\n-]*)?\s*---\n", flags=re.I)


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
    result = df[columns].fillna("")
    if result.empty:
        raise ValueError("The obligation register contains no data rows.")
    return result


def chunk_policy_text(raw_text: str, max_chars: int = 1800) -> List[Dict[str, str]]:
    """Split policy text into page-aware, reviewable evidence chunks."""
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
        boundaries = [0, *(match.start() for match in starts), len(cleaned)]
        boundaries = sorted(set(boundaries))
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


def best_policy_match(obligation: str, directive_text: str, chunks: List[Dict[str, str]]) -> Tuple[float, Dict[str, str], float, List[str]]:
    search_text = f"{obligation} {directive_text}"
    terms = _keywords(search_text)[:45]
    best_score = 0.0
    best_keyword_score = 0.0
    best_chunk = {"page": "", "text": ""}
    best_missing: List[str] = terms
    for chunk in chunks:
        text = chunk["text"]
        lowered = text.lower()
        hits = [term for term in terms if term in lowered]
        keyword_score = len(hits) / max(len(terms), 1)
        fuzzy_score = fuzz.token_set_ratio(search_text[:1200], text[:1800]) / 100
        score = (keyword_score * 0.72) + (fuzzy_score * 0.28)
        if score > best_score:
            best_score = score
            best_keyword_score = keyword_score
            best_chunk = chunk
            best_missing = [term for term in terms if term not in lowered]
    return best_score, best_chunk, best_keyword_score, best_missing


def coverage_status(score: float, keyword_score: float, chunk_text: str) -> str:
    if not chunk_text:
        return "Completely Missing"
    if _contains_negative(chunk_text):
        return "Partially Covered" if keyword_score >= 0.22 else "Completely Missing"
    if score >= 0.58 and keyword_score >= 0.38:
        return "Completely Covered"
    if score >= 0.28 or keyword_score >= 0.18:
        return "Partially Covered"
    return "Completely Missing"


def recommendation_for(status: str, obligation: str, missing_terms: List[str], negative_evidence: bool) -> str:
    if status == "Completely Covered":
        return ""
    requirement = re.sub(r"\s+", " ", obligation).strip().rstrip(".")
    if len(requirement) > 260:
        requirement = requirement[:257].rstrip() + "..."
    missing = ", ".join(missing_terms[:6])
    if status == "Partially Covered":
        detail = f" Explicitly address the missing elements: {missing}." if missing else " Add the missing conditions, ownership, timing, and evidence requirements."
        if negative_evidence:
            detail = " Remove the conflicting limitation and make the requirement mandatory." + detail
        return f"Update the policy so it fully requires: {requirement}.{detail}"
    return f"Add a policy clause that requires: {requirement}. Assign an accountable owner, implementation control, review frequency, and retained evidence."


def _priority(status: str, existing: str) -> str:
    if status == "Completely Missing":
        return "High"
    if status == "Partially Covered":
        return "Medium"
    return existing if existing in {"High", "Medium", "Low"} else "Low"


def _write_excel(path: Path, assessment: pd.DataFrame, statistics: pd.DataFrame, logs: List[Dict[str, Any]]) -> None:
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        for name, frame in {
            "Gap Assessment": assessment,
            "Statistics": statistics,
            "Process Log": pd.DataFrame(logs),
        }.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(len(frame), 1), max(len(frame.columns) - 1, 0))
            for index, column in enumerate(frame.columns):
                lengths = [len(str(column)), *(len(str(value)) for value in frame[column].head(80))]
                sheet.set_column(index, index, min(max(lengths) + 2, 60))


def review_policy_gaps(register_path: Path, policy_path: Path) -> Dict[str, Any]:
    register = load_register(register_path)
    policy_text, pages = extract_pdf_text(policy_path)
    if len(re.sub(r"\s+", "", policy_text)) < 50:
        raise ValueError("Could not extract readable text from the uploaded policy PDF.")
    chunks = chunk_policy_text(policy_text)
    rows: List[Dict[str, str]] = []

    for _, row in register.iterrows():
        obligation = str(row["Obligation"])
        directive_text = str(row["Language from Directive"])
        informational = _is_informational(row)
        if informational:
            status = "Completely Covered"
            match = {"page": "", "text": ""}
            recommendation = "Informational item only; no policy amendment is required."
        else:
            score, match, keyword_score, missing_terms = best_policy_match(obligation, directive_text, chunks)
            status = coverage_status(score, keyword_score, match["text"])
            recommendation = recommendation_for(status, obligation, missing_terms, _contains_negative(match["text"]))

        evidence_text = match["text"] if status != "Completely Missing" else ""
        evidence_page = match["page"] if evidence_text else ""
        existing_priority = str(row.get("Priority", ""))
        rows.append({
            "Section": row["Section"],
            "Language from Directive": directive_text,
            "Obligation": obligation,
            "Obligation Category": row["Obligation Category"],
            "Primary Responsible Department": row["Primary Responsible Department"],
            "Support Function": row["Support Function"],
            "Coverage Status": status,
            "Policy Gap and Recommendations": recommendation,
            "Policy Page": evidence_page,
            "Corresponding Policy Text": evidence_text,
            "Priority": _priority(status, existing_priority),
        })

    df_gap = pd.DataFrame(rows, columns=GAP_COLUMNS)
    if not set(df_gap["Coverage Status"]).issubset(VALID_STATUSES):
        raise RuntimeError("Gap analysis produced an unsupported coverage status.")

    stats_frames: List[pd.DataFrame] = []
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
        stats_frames.append(counts)
    statistics = pd.concat(stats_frames, ignore_index=True, sort=False).fillna("")

    logs = [
        {"stage": "Select Inputs", "status": "Completed", "message": f"Validated register columns and loaded {policy_path.name}.", "row_count": len(register)},
        {"stage": "Gap Analysis", "status": "Completed", "message": f"Compared every obligation only against uploaded policy evidence. {extraction_summary(pages)}", "row_count": len(df_gap)},
        {"stage": "Results", "status": "Completed", "message": "Generated gap assessment, statistics, Excel, and CSV outputs.", "row_count": len(df_gap)},
    ]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", register_path.stem).strip("_")
    excel_path = output_path(f"{stem}_policy_gap_assessment.xlsx")
    csv_path = output_path(f"{stem}_policy_gap_assessment.csv")
    _write_excel(excel_path, df_gap, statistics, logs)
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
