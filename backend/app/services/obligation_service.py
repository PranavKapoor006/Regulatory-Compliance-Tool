from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text
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
        (r"\bprior te\b", "prior to"),
        (r"\brelating ta\b", "relating to"),
        (r"\bpersen te\b", "person to"),
    )
    for pattern, replacement in repairs:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def _is_structural_stem(text: str) -> bool:
    cleaned = _clean(text)
    return bool(
        re.search(r"(?:at\s+least|following|as\s+follows)\s*[-—:]?\.?$", cleaned, flags=re.I)
        or re.search(r"\b(?:must|shall)\b.{0,100}[-—:]\.?$", cleaned, flags=re.I)
        or re.search(r"\b(?:applies|apply)\s+to\s*[~\-—:]?\.?$", cleaned, flags=re.I)
        or re.search(r"\b(?:must|shall)\b.{0,300}\b(?:of|following|least)\s*[-—~]", cleaned, flags=re.I)
    )


def is_actionable(text: str, parent_context: str = "") -> bool:
    text = _clean(text)
    if _is_structural_stem(text):
        return False
    if re.search(r"\b(?:forms?|directive)\b.*\bavailable\s+on\s+the\s+website\b", text, flags=re.I) and not re.search(r"\b(?:insurer|entity|board|management)\b.{0,80}\b(?:must|shall)\b", text, flags=re.I):
        return False
    if re.search(r"\binternational standards?\b.*\brequire", text, flags=re.I) and not re.search(r"\binsurers?\b.{0,100}\b(?:must|shall|required)\b", text, flags=re.I):
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
    text = _clean(wording)
    if not is_actionable(text, parent_context):
        return "Informational or contextual text; no standalone implementation obligation is created."

    # Preserve the regulatory condition and actor where possible while normalising
    # shall/required language into a reviewable must statement.
    # Child list items inherit the parent's actor and action. Without this,
    # "the proposed outsourcing" could be assessed as an internal-information
    # requirement even though its parent says "notify the Registrar of—".
    inherited = ""
    if parent_context and _is_structural_stem(parent_context):
        inherited = re.sub(r"\s*[-—~][\s\S]*$", "", _clean(parent_context)).rstrip(" :")
    source_text = f"{inherited} {text}".strip() if inherited else text
    actionable_parts = [
        part.strip()
        # A period is a boundary only when the next sentence starts with a
        # capital letter. This preserves legal citations such as "Act No. 71
        # of 2008" instead of truncating the obligation after "No.".
        for part in re.split(r"(?<=[;!?])\s+|(?<=\.)\s+(?=[A-Z])", source_text)
        if ACTION_PATTERN.search(part)
        and not re.search(r"\bavailable\s+on\s+the\s+website\b", part, flags=re.I)
    ]
    statement = " ".join(actionable_parts) or source_text
    actor = re.search(r"\b(?:An insurer|The insurer|Insurers|The regulated entity|The board|A minimum|At least|Each|Any|The Registrars?|There must)\b", statement)
    if actor and 0 < actor.start() < 140:
        statement = statement[actor.start():]
    statement = re.sub(r"\bshall\b", "must", statement, flags=re.I)
    statement = re.sub(r"\bis required to\b", "must", statement, flags=re.I)
    statement = statement.rstrip(" ;")
    direct_match = re.search(r"\bdirect\s+long-term and short-term insurers.*?\bto comply\b(?P<rest>.*)", statement, flags=re.I)
    if direct_match:
        statement = f"Insurers must comply{direct_match.group('rest')}"
    elif re.search(r"\bthis directive applies\b", statement, flags=re.I) and not re.search(r"\bmust\b", statement, flags=re.I):
        statement = f"The regulated entity must comply with this applicability and scope provision: {statement}"
    elif not re.search(r"\bmust\b", statement, flags=re.I):
        statement = f"The regulated entity must comply with this requirement: {statement or 'the stated regulatory requirement'}"
    if not statement.endswith("."):
        statement += "."
    return statement


def _parent_context(rows: List[Dict[str, Any]], index: int) -> str:
    section = str(rows[index].get("Section", ""))
    if not re.fullmatch(r"\d+(?:\.\d+)+", section):
        return ""
    parent = section.rsplit(".", 1)[0]
    for prior in reversed(rows[:index]):
        if str(prior.get("Section")) == parent:
            return _clean(prior.get("Language from Directive"))
    return ""


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
    ]:
        counts = df[column].value_counts(dropna=False).rename_axis("Value").reset_index(name="Count")
        counts.insert(0, "Dimension", dimension)
        frames.append(counts)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["Dimension", "Value", "Count"])


def _write_excel(path: Path, obligations: pd.DataFrame, breakdown: pd.DataFrame, statistics: pd.DataFrame, logs: List[Dict[str, Any]]) -> None:
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        sheets = {
            "Obligations": obligations,
            "Text Breakdown": breakdown,
            "Statistics": statistics,
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


def extract_obligations_from_pdf(pdf_path: Path) -> Dict[str, Any]:
    raw_text, pages = extract_pdf_text(pdf_path)
    if len(_clean(raw_text)) < 80:
        raise ValueError("The PDF did not produce enough readable text for obligation extraction.")
    valid, validation_message, _validation = validate_directive_candidate(pdf_path, raw_text)
    if not valid:
        raise ValueError(validation_message)

    breakdown = breakdown_regulatory_text(raw_text)
    obligation_rows: List[Dict[str, str]] = []
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
            obligation_rows.append(_structural_parent_row(section, wording))
            continue
        generated = _llm_rows(pdf_path.name, section, wording, parent)
        if generated:
            obligation_rows.extend(generated)
            llm_row_count += len(generated)
        else:
            obligation_rows.append(_fallback_row(section, wording, parent))

    df_obligations = pd.DataFrame(obligation_rows, columns=OBLIGATION_COLUMNS)
    df_breakdown = pd.DataFrame(breakdown)
    statistics = _statistics_frame(df_obligations)
    logs = [
        {"stage": "Select PDF", "status": "Completed", "message": f"Loaded and validated {pdf_path.name}. {validation_message}", "row_count": 1},
        {"stage": "Breakdown", "status": "Completed", "message": extraction_summary(pages), "row_count": len(df_breakdown)},
        {"stage": "Extraction", "status": "Completed", "message": f"Generated the obligation register ({llm_row_count} AI-generated row(s); remaining rows used deterministic taxonomy rules).", "row_count": len(df_obligations)},
        {"stage": "Results", "status": "Completed", "message": "Generated Excel and CSV outputs.", "row_count": len(df_obligations)},
    ]

    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", pdf_path.stem).strip("_")
    excel_path = output_path(f"{stem}_obligation_extraction.xlsx")
    csv_path = output_path(f"{stem}_obligation_extraction.csv")
    _write_excel(excel_path, df_obligations, df_breakdown, statistics, logs)
    df_obligations.to_csv(csv_path, index=False)

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
        ],
        "tabs": {
            "obligations": df_obligations.to_dict(orient="records"),
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
        "output_files": {"excel": excel_path.name, "csv": csv_path.name},
    }
