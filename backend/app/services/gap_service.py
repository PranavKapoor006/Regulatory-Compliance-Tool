from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from rapidfuzz import fuzz

from app.services.pdf_service import extract_pdf_text
from app.services.storage import output_path

REQUIRED_REGISTER_COLUMNS = [
    "Section",
    "Language from Directive",
    "Obligation",
    "Obligation Category",
    "Primary Responsible Department",
    "Support Function",
]

GAP_COLUMNS = REQUIRED_REGISTER_COLUMNS + [
    "Coverage Status",
    "Policy Gap and Recommendations",
    "Policy Page",
    "Corresponding Policy Text",
    "Priority",
]

NEGATIVE_PHRASES = [
    "does not currently require",
    "does not yet require",
    "not defined",
    "not mandatory",
    "not required",
    "no requirement",
    "not documented",
]

STOPWORDS = {
    "this", "that", "with", "from", "shall", "must", "have", "will", "they", "their", "there",
    "under", "section", "directive", "institution", "regulatory", "obligation", "material", "outsourcing",
    "outsourced", "arrangement", "arrangements", "policy", "business", "function", "functions",
}


def load_register(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    df.columns = [str(col).strip() for col in df.columns]
    missing = [col for col in REQUIRED_REGISTER_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    return df[REQUIRED_REGISTER_COLUMNS].fillna("")


def chunk_policy_text(raw_text: str, max_chars: int = 1800) -> List[Dict[str, str]]:
    """Split policy text into reviewable sections so evidence is specific, not one huge page."""
    page_parts = re.split(r"(?:^|\n+)--- Page (\d+) ---\n", raw_text.strip())
    chunks: List[Dict[str, str]] = []

    if len(page_parts) == 1:
        page_parts = ["", "Unknown", raw_text]

    iterator = iter(page_parts)
    _prefix = next(iterator, "")
    for page, text in zip(iterator, iterator):
        cleaned = re.sub(r"\r", "\n", text)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        # Split at top-level numbered headings, e.g. 1. Policy objective, 2. Governance.
        starts = list(re.finditer(r"(?m)^\s*(\d+)\.\s+", cleaned))
        if not starts:
            for index in range(0, len(cleaned), max_chars):
                chunk = cleaned[index:index + max_chars].strip()
                if chunk:
                    chunks.append({"page": str(page), "text": chunk})
            continue
        intro = cleaned[: starts[0].start()].strip()
        if intro:
            chunks.append({"page": str(page), "text": intro})
        for idx, match in enumerate(starts):
            end = starts[idx + 1].start() if idx + 1 < len(starts) else len(cleaned)
            chunk = cleaned[match.start():end].strip()
            if len(chunk) > max_chars:
                # Keep readable slices if an unusually long section appears.
                for part_start in range(0, len(chunk), max_chars):
                    part = chunk[part_start:part_start + max_chars].strip()
                    if part:
                        chunks.append({"page": str(page), "text": part})
            elif chunk:
                chunks.append({"page": str(page), "text": chunk})
    if not chunks and raw_text.strip():
        chunks.append({"page": "Unknown", "text": raw_text.strip()[:max_chars]})
    return chunks


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[A-Za-z][A-Za-z-]{3,}", text.lower())
    return [w for w in words if w not in STOPWORDS]


def _contains_negative(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in NEGATIVE_PHRASES)


def _is_informational(obligation: str, directive_text: str = "") -> bool:
    combined = f"{obligation} {directive_text}".lower()
    return any(
        phrase in combined
        for phrase in [
            "contextual background",
            "background context",
            "does not create",
            "no direct implementation obligation",
            "not applicable",
            "informational",
        ]
    )


def best_policy_match(obligation: str, directive_text: str, chunks: List[Dict[str, str]]) -> Tuple[float, Dict[str, str], float]:
    search_text = f"{obligation} {directive_text}"
    terms = _keywords(search_text)[:35]
    best_score = 0.0
    best_keyword_score = 0.0
    best_chunk = {"page": "Not located", "text": "No directly matching policy text was identified in the uploaded policy."}
    for chunk in chunks:
        text = chunk["text"]
        lowered = text.lower()
        keyword_hits = sum(1 for term in terms if term in lowered)
        keyword_score = keyword_hits / max(len(terms), 1)
        fuzzy_score = fuzz.token_set_ratio(search_text[:1000], text[:1600]) / 100
        score = (keyword_score * 0.68) + (fuzzy_score * 0.32)
        if score > best_score:
            best_score = score
            best_keyword_score = keyword_score
            best_chunk = chunk
    return best_score, best_chunk, best_keyword_score


def coverage_status(score: float, keyword_score: float, obligation: str, directive_text: str, chunk_text: str) -> str:
    if _is_informational(obligation, directive_text):
        return "Not Applicable / Informational"

    # If the policy explicitly says something is not defined/required, do not mark it as covered.
    if _contains_negative(chunk_text):
        if any(token in f"{obligation} {directive_text}".lower() for token in ["notify", "notification", "annual independent", "tested exit", "exit plan"]):
            return "Completely Missing"
        return "Partially Covered"

    if score >= 0.56 and keyword_score >= 0.34:
        return "Completely Covered"
    if score >= 0.27 or keyword_score >= 0.18:
        return "Partially Covered"
    return "Completely Missing"


def recommendation_for(status: str, obligation: str, directive_text: str, chunk_text: str) -> str:
    if status == "Completely Covered":
        return "No gap identified. No policy update is required based on the matched policy evidence."
    if status == "Partially Covered":
        return (
            "Policy partially addresses the topic but does not fully evidence the regulatory requirement. "
            "Update the policy to explicitly cover the missing obligation elements, owner, control evidence, and review frequency."
        )
    if status == "Completely Missing":
        if _contains_negative(chunk_text):
            return (
                "Policy evidence indicates this requirement is not currently defined or mandatory. "
                "Add a specific policy clause to address the obligation and assign ownership for implementation and evidence retention."
            )
        return (
            "No directly supporting policy text was identified. Add a new policy clause addressing this obligation, including ownership, controls, monitoring, and evidence requirements."
        )
    return "This directive section is background or contextual and does not create a direct policy action; no policy amendment is required unless management wants to reference it for completeness."


def _priority_for_status(status: str, existing_priority: str = "") -> str:
    if status == "Completely Missing":
        return "High"
    if status == "Partially Covered":
        return "Medium"
    if existing_priority:
        return existing_priority
    return "Low"


def review_policy_gaps(register_path: Path, policy_path: Path) -> Dict:
    register = load_register(register_path)
    policy_text, _pages = extract_pdf_text(policy_path)
    if len(policy_text.strip()) < 50:
        raise ValueError("Could not extract readable text from the uploaded policy PDF. Please upload a text-based PDF.")

    chunks = chunk_policy_text(policy_text)
    rows: List[Dict[str, str]] = []

    for _, row in register.iterrows():
        obligation = str(row["Obligation"])
        directive_text = str(row["Language from Directive"])
        score, match, keyword_score = best_policy_match(obligation, directive_text, chunks)
        status = coverage_status(score, keyword_score, obligation, directive_text, match["text"])

        if status == "Completely Missing":
            if _contains_negative(match["text"]):
                policy_text_excerpt = match["text"]
                policy_page = match["page"]
            else:
                policy_text_excerpt = "No directly matching policy text was identified in the uploaded policy."
                policy_page = "Not located"
        elif status == "Not Applicable / Informational":
            policy_text_excerpt = "No policy evidence required because the directive item is informational/background only."
            policy_page = "Not applicable"
        else:
            policy_text_excerpt = match["text"]
            policy_page = match["page"]

        existing_priority = str(row.get("Priority", "")) if "Priority" in register.columns else ""
        rows.append({
            "Section": row["Section"],
            "Language from Directive": directive_text,
            "Obligation": obligation,
            "Obligation Category": row["Obligation Category"],
            "Primary Responsible Department": row["Primary Responsible Department"],
            "Support Function": row["Support Function"],
            "Coverage Status": status,
            "Policy Gap and Recommendations": recommendation_for(status, obligation, directive_text, match["text"]),
            "Policy Page": policy_page,
            "Corresponding Policy Text": policy_text_excerpt,
            "Priority": _priority_for_status(status, existing_priority),
        })

    df_gap = pd.DataFrame(rows, columns=GAP_COLUMNS)
    status_counts = df_gap["Coverage Status"].value_counts().reset_index()
    status_counts.columns = ["Coverage Status", "Count"]
    category_counts = df_gap.groupby(["Obligation Category", "Coverage Status"]).size().reset_index(name="Count")
    dept_counts = df_gap.groupby(["Primary Responsible Department", "Coverage Status"]).size().reset_index(name="Count")
    priority_counts = df_gap["Priority"].value_counts().reset_index()
    priority_counts.columns = ["Priority", "Count"]

    stem = register_path.stem.replace(" ", "_")
    excel_path = output_path(f"{stem}_policy_gap_assessment.xlsx")
    csv_path = output_path(f"{stem}_policy_gap_assessment.csv")
    with pd.ExcelWriter(excel_path, engine="xlsxwriter") as writer:
        df_gap.to_excel(writer, sheet_name="Gap Assessment", index=False)
        status_counts.to_excel(writer, sheet_name="Status Statistics", index=False)
        category_counts.to_excel(writer, sheet_name="Category Statistics", index=False)
        dept_counts.to_excel(writer, sheet_name="Department Statistics", index=False)
        priority_counts.to_excel(writer, sheet_name="Priority Statistics", index=False)
    df_gap.to_csv(csv_path, index=False)

    kpis = [
        {"label": "Total Obligations", "value": len(df_gap)},
        {"label": "Completely Covered", "value": int((df_gap["Coverage Status"] == "Completely Covered").sum())},
        {"label": "Partially Covered", "value": int((df_gap["Coverage Status"] == "Partially Covered").sum())},
        {"label": "Completely Missing", "value": int((df_gap["Coverage Status"] == "Completely Missing").sum())},
    ]
    logs = [
        {"stage": "Select Inputs", "status": "Completed", "message": "Validated obligation register and policy PDF", "row_count": len(register)},
        {"stage": "Gap Analysis", "status": "Completed", "message": "Mapped obligations to uploaded policy text using section-level policy evidence", "row_count": len(df_gap)},
        {"stage": "Results", "status": "Completed", "message": "Generated policy gap assessment outputs", "row_count": len(df_gap)},
    ]
    return {
        "kpis": kpis,
        "tabs": {
            "gap_assessment": df_gap.to_dict(orient="records"),
            "statistics": {
                "status": status_counts.to_dict(orient="records"),
                "category": category_counts.to_dict(orient="records"),
                "department": dept_counts.to_dict(orient="records"),
                "priority": priority_counts.to_dict(orient="records"),
            },
            "process_log": logs,
        },
        "logs": logs,
        "output_files": {"excel": excel_path.name, "csv": csv_path.name},
    }
