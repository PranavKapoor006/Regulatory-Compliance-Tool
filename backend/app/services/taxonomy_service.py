from __future__ import annotations

from functools import lru_cache
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from app.core.config import get_settings

CATEGORY_KEYWORDS = {
    "Outsourcing / Third Party": ["outsourc", "third party", "service provider", "agreement", "contract", "sub-outsourc", "procurement", "selection", "monitor"],
    "Governance": ["board", "director", "governance", "committee", "approval", "oversight", "senior management"],
    "Risk Management": ["risk", "risk management", "internal control", "assess", "mitigate", "material", "operational", "reputation"],
    "Regulatory Compliance": ["directive", "act", "legal", "law", "regulatory", "comply", "compliance", "registrar", "authority"],
    "Regulatory reporting and returns": ["notify", "notification", "submit", "report", "return", "registrar", "disclose", "inform"],
    "Finance": ["financial", "finance", "fee", "payment", "capital", "audit", "remuneration"],
    "Informational / Context": ["purpose", "background", "definition", "means", "website", "subject"],
}

DEFAULT_DEPARTMENT_MAP = {
    "Risk Management": ["Enterprise Risk Management", "Operational Risk"],
    "Legal & Compliance": ["Regulatory Compliance", "Legal Advisory"],
    "Operations": ["Business Operations", "Outsourcing Management"],
    "Board / Senior Management": ["Board Governance", "Executive Management"],
    "Procurement": ["Vendor Management", "Third Party Due Diligence"],
    "Finance": ["Finance Control", "Accounting"],
    "Information Technology": ["Information Security", "IT Operations"],
}


@lru_cache(maxsize=1)
def load_taxonomy() -> Dict:
    settings = get_settings()
    candidates = sorted(settings.taxonomy_root.glob("*.xlsx"))
    rows: List[Dict[str, str]] = []
    mapping: Dict[str, List[str]] = {}

    if candidates:
        try:
            df = pd.read_excel(candidates[0]).fillna("")
            df.columns = [str(c).strip() for c in df.columns]
            dept_col = next((c for c in df.columns if c.lower() in {"department", "function", "primary responsible department"}), None)
            support_col = next((c for c in df.columns if c.lower() in {"sub-department", "sub department", "support function", "role"}), None)
            role_col = next((c for c in df.columns if "role" in c.lower() or "responsib" in c.lower()), None)
            desc_col = next((c for c in df.columns if "description" in c.lower() or "detail" in c.lower()), None)
            if dept_col and support_col:
                for _, item in df.iterrows():
                    dept = str(item.get(dept_col, "")).strip()
                    support = str(item.get(support_col, "")).strip()
                    if not dept or not support:
                        continue
                    role = str(item.get(role_col, "")).strip() if role_col else ""
                    desc = str(item.get(desc_col, "")).strip() if desc_col else ""
                    rows.append({"department": dept, "support_function": support, "role": role, "description": desc})
                    mapping.setdefault(dept, [])
                    if support not in mapping[dept]:
                        mapping[dept].append(support)
        except Exception:
            rows = []
            mapping = {}

    if not mapping:
        mapping = DEFAULT_DEPARTMENT_MAP
        for dept, supports in mapping.items():
            for support in supports:
                rows.append({"department": dept, "support_function": support, "role": "", "description": ""})

    prompt_lines: List[str] = []
    for dept in sorted(mapping):
        prompt_lines.append(f"Department: {dept}")
        for support in mapping[dept]:
            detail = next((r for r in rows if r["department"] == dept and r["support_function"] == support), {})
            prompt_lines.append(f"- Support Function: {support}")
            if detail.get("role"):
                prompt_lines.append(f"  Role: {detail['role']}")
            if detail.get("description"):
                prompt_lines.append(f"  Description: {detail['description']}")

    return {"rows": rows, "mapping": mapping, "departments": sorted(mapping), "prompt_text": "\n".join(prompt_lines)}


def allowed_categories() -> List[str]:
    return list(CATEGORY_KEYWORDS.keys())


def allowed_departments() -> List[str]:
    return load_taxonomy()["departments"]


def taxonomy_prompt_text() -> str:
    return load_taxonomy()["prompt_text"]


def classify_category(text: str) -> str:
    text_l = text.lower()
    best = "Regulatory Compliance"
    score = -1
    for category, keywords in CATEGORY_KEYWORDS.items():
        current = sum(1 for keyword in keywords if keyword in text_l)
        if current > score:
            best = category
            score = current
    return best if score > 0 else "Informational / Context"


def _best_department(text: str) -> str:
    text_l = text.lower()
    taxonomy = load_taxonomy()
    best_dept = taxonomy["departments"][0] if taxonomy["departments"] else "Legal & Compliance"
    best_score = -1
    for row in taxonomy["rows"]:
        haystack = f"{row['department']} {row['support_function']} {row.get('role','')} {row.get('description','')}".lower()
        score = sum(1 for token in re_tokens(text_l) if token in haystack)
        # domain hints
        if "outsourc" in text_l and any(x in haystack for x in ["vendor", "third", "outsourc", "procurement"]):
            score += 4
        if "board" in text_l and "board" in haystack:
            score += 5
        if "risk" in text_l and "risk" in haystack:
            score += 4
        if "notify" in text_l and "compliance" in haystack:
            score += 3
        if score > best_score:
            best_score = score
            best_dept = row["department"]
    return best_dept


def re_tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z]{3,}", text.lower())[:80]]


def _best_support(dept: str, text: str) -> str:
    text_l = text.lower()
    taxonomy = load_taxonomy()
    rows = [r for r in taxonomy["rows"] if r["department"] == dept]
    if not rows:
        return taxonomy["mapping"].get(dept, [dept])[0]
    best_support = rows[0]["support_function"]
    best_score = -1
    for row in rows:
        haystack = f"{row['support_function']} {row.get('role','')} {row.get('description','')}".lower()
        score = sum(1 for token in re_tokens(text_l) if token in haystack)
        if score > best_score:
            best_score = score
            best_support = row["support_function"]
    return best_support


def classify_department_support(text: str) -> Tuple[str, str]:
    dept = _best_department(text)
    support = _best_support(dept, text)
    return dept, support


def validate_department_support(department: str, support: str) -> Tuple[str, str]:
    taxonomy = load_taxonomy()
    if department not in taxonomy["mapping"]:
        department = _best_department(f"{department} {support}")
    valid_supports = taxonomy["mapping"].get(department, [])
    if valid_supports and support not in valid_supports:
        support = _best_support(department, support)
    return department, support or (valid_supports[0] if valid_supports else department)
