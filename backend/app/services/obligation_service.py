from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text
from app.services.pdf_service import extract_pdf_text
from app.services.storage import output_path

OBLIGATION_COLUMNS = [
    "Section",
    "Language from Directive",
    "Obligation",
    "Obligation Category",
    "Primary Responsible Department",
    "Support Function",
    "Priority",
    "Actionable",
]

ACTION_WORDS = ["must", "shall", "required", "require", "ensure", "direct", "comply", "submit", "maintain", "establish", "monitor", "assess"]

CATEGORY_RULES = [
    ("Risk Management", ["risk", "mitigate", "exposure", "control", "impact"]),
    ("Governance", ["board", "committee", "governance", "approval", "management"]),
    ("Reporting / Notification", ["notify", "report", "submit", "inform", "disclose"]),
    ("Outsourcing / Third Party", ["outsourc", "service provider", "third party", "agreement"]),
    ("Legal & Compliance", ["comply", "regulation", "act", "directive", "legislative"]),
]

DEPARTMENT_RULES = [
    ("Risk Management", "Enterprise Risk Management", ["risk", "mitigate", "exposure"]),
    ("Legal & Compliance", "Regulatory Compliance", ["comply", "regulation", "act", "directive", "legal"]),
    ("Operations", "Business Operations", ["process", "service", "outsourc", "operations"]),
    ("Board / Senior Management", "Governance Office", ["board", "senior management", "committee"]),
    ("Finance", "Finance Control", ["financial", "capital", "amount", "fee"]),
]


def is_actionable(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ACTION_WORDS)


def classify_category(text: str) -> str:
    lowered = text.lower()
    scores = [(name, sum(1 for token in tokens if token in lowered)) for name, tokens in CATEGORY_RULES]
    best_name, best_score = max(scores, key=lambda x: x[1])
    if best_score == 0:
        return "Informational / Context"
    return best_name


def classify_department(text: str) -> tuple[str, str]:
    lowered = text.lower()
    scores = [(dept, support, sum(1 for token in tokens if token in lowered)) for dept, support, tokens in DEPARTMENT_RULES]
    dept, support, score = max(scores, key=lambda x: x[2])
    if score == 0:
        return "Legal & Compliance", "Regulatory Compliance"
    return dept, support


def priority(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ["must", "shall", "prohibit", "breach", "non-compliance", "effective date"]):
        return "High"
    if any(token in lowered for token in ["should", "may", "consider"]):
        return "Low"
    return "Medium"


def generate_obligation(section: str, wording: str) -> str:
    short = re.sub(r"\s+", " ", wording).strip()
    if not is_actionable(short):
        return "This section provides contextual or background information and does not create a direct compliance obligation."
    actor = "Insurers"
    if "bank" in short.lower():
        actor = "The institution"
    if len(short) > 240:
        short = short[:237].rstrip() + "..."
    return f"{actor} must address the requirement described in section {section}: {short}"


def extract_obligations_from_pdf(pdf_path: Path) -> Dict:
    raw_text, _pages = extract_pdf_text(pdf_path)
    breakdown = breakdown_regulatory_text(raw_text)
    obligation_rows: List[Dict[str, str]] = []

    for item in breakdown:
        wording = item["Language from Directive"]
        actionable = "Yes" if is_actionable(wording) else "No"
        category = classify_category(wording)
        department, support = classify_department(wording)
        obligation_rows.append({
            "Section": item["Section"],
            "Language from Directive": wording,
            "Obligation": generate_obligation(item["Section"], wording),
            "Obligation Category": category,
            "Primary Responsible Department": department,
            "Support Function": support,
            "Priority": priority(wording),
            "Actionable": actionable,
        })

    df_obligations = pd.DataFrame(obligation_rows, columns=OBLIGATION_COLUMNS)
    df_breakdown = pd.DataFrame(breakdown)
    category_counts = df_obligations["Obligation Category"].value_counts().reset_index()
    category_counts.columns = ["Category", "Count"]
    dept_counts = df_obligations["Primary Responsible Department"].value_counts().reset_index()
    dept_counts.columns = ["Department", "Count"]
    priority_counts = df_obligations["Priority"].value_counts().reset_index()
    priority_counts.columns = ["Priority", "Count"]
    actionable_counts = df_obligations["Actionable"].value_counts().reset_index()
    actionable_counts.columns = ["Actionable", "Count"]

    stem = pdf_path.stem.replace(" ", "_")
    excel_path = output_path(f"{stem}_obligation_extraction.xlsx")
    csv_path = output_path(f"{stem}_obligation_extraction.csv")
    with pd.ExcelWriter(excel_path, engine="xlsxwriter") as writer:
        df_obligations.to_excel(writer, sheet_name="Obligations", index=False)
        df_breakdown.to_excel(writer, sheet_name="Text Breakdown", index=False)
        category_counts.to_excel(writer, sheet_name="Category Statistics", index=False)
        dept_counts.to_excel(writer, sheet_name="Department Statistics", index=False)
        priority_counts.to_excel(writer, sheet_name="Priority Statistics", index=False)
        actionable_counts.to_excel(writer, sheet_name="Actionable Statistics", index=False)
    df_obligations.to_csv(csv_path, index=False)

    kpis = [
        {"label": "Total Sections", "value": len(df_breakdown)},
        {"label": "Actionable Obligations", "value": int((df_obligations["Actionable"] == "Yes").sum())},
        {"label": "Categories", "value": int(df_obligations["Obligation Category"].nunique())},
        {"label": "Departments", "value": int(df_obligations["Primary Responsible Department"].nunique())},
    ]
    logs = [
        {"stage": "Select PDF", "status": "Completed", "message": f"Loaded {pdf_path.name}", "row_count": 1},
        {"stage": "Breakdown", "status": "Completed", "message": "Generated clause-wise text breakdown", "row_count": len(df_breakdown)},
        {"stage": "Extraction", "status": "Completed", "message": "Generated obligation register", "row_count": len(df_obligations)},
    ]
    return {
        "kpis": kpis,
        "tabs": {
            "obligations": df_obligations.to_dict(orient="records"),
            "text_breakdown": df_breakdown.to_dict(orient="records"),
            "statistics": {
                "category": category_counts.to_dict(orient="records"),
                "department": dept_counts.to_dict(orient="records"),
                "priority": priority_counts.to_dict(orient="records"),
                "actionable": actionable_counts.to_dict(orient="records"),
            },
            "process_log": logs,
        },
        "logs": logs,
        "output_files": {"excel": excel_path.name, "csv": csv_path.name},
    }
