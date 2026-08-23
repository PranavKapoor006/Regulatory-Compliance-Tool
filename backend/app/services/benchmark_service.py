from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


GAP_SHEET = "Gap Known Answer"
EXTRACTION_SHEET = "Extraction QA"
RECOMMENDATION_SHEET = "Recommendation QA"
BENCHMARK_VERSION = "2026-07-27.5"


def _clean(value: Any) -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def _percentage(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2) if denominator else 100.0


def _required_phrases(value: Any) -> List[str]:
    return [
        phrase.strip()
        for phrase in _clean(value).split(";")
        if phrase.strip()
    ]


def _all_phrases_present(required: Iterable[str], actual: str) -> bool:
    normal_actual = _normal(actual)
    return all(_normal(phrase) in normal_actual for phrase in required)


def load_known_answers(path: Path) -> Dict[str, pd.DataFrame]:
    workbook = pd.ExcelFile(path)
    if GAP_SHEET not in workbook.sheet_names:
        raise ValueError(f"Benchmark workbook must contain a '{GAP_SHEET}' sheet.")
    return {
        "gap": pd.read_excel(path, sheet_name=GAP_SHEET, dtype={"Section": str}),
        "extraction": (
            pd.read_excel(path, sheet_name=EXTRACTION_SHEET, dtype={"Section": str})
            if EXTRACTION_SHEET in workbook.sheet_names
            else pd.DataFrame()
        ),
        "recommendations": (
            pd.read_excel(
                path,
                sheet_name=RECOMMENDATION_SHEET,
                dtype={"Section": str},
            )
            if RECOMMENDATION_SHEET in workbook.sheet_names
            else pd.DataFrame()
        ),
    }


def _actual_frame(actual_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(actual_rows).copy()
    if "Section" not in frame.columns:
        raise ValueError("Actual assessment rows must contain a Section column.")
    frame["Section"] = frame["Section"].astype(str)
    return frame


def score_gap_benchmark(
    expected: pd.DataFrame,
    actual_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    actual = _actual_frame(actual_rows)
    required_expected = {
        "Section",
        "Expected Status",
        "Expected Missing Elements",
        "Expected Evidence Phrase",
        "Recommendation Must Mention",
    }
    missing_columns = sorted(required_expected - set(expected.columns))
    if missing_columns:
        raise ValueError(
            "Gap benchmark is missing required columns: "
            + ", ".join(missing_columns)
        )

    joined = expected.merge(
        actual,
        on="Section",
        how="left",
        suffixes=("_Expected", "_Actual"),
        indicator=True,
    )
    evaluated: List[Dict[str, Any]] = []
    for _, row in joined.iterrows():
        expected_status = _clean(row.get("Expected Status"))
        actual_status = _clean(row.get("Coverage Status"))
        found = row["_merge"] == "both"
        evidence = _clean(row.get("Corresponding Policy Text"))
        page = _clean(row.get("Policy Page"))
        recommendation_bundle = " ".join(
            _clean(row.get(column))
            for column in (
                "Policy Gap and Recommendations",
                "Draft Policy Clause",
                "Recommendation Owner",
                "Target Timeframe",
                "Implementation Evidence",
            )
        )
        expected_evidence = _required_phrases(row.get("Expected Evidence Phrase"))
        expected_missing = set(
            _normal(value)
            for value in _required_phrases(row.get("Expected Missing Elements"))
        )
        actual_missing = set(
            _normal(value)
            for value in _required_phrases(row.get("Missing Elements"))
        )
        expected_recommendation = _required_phrases(
            row.get("Recommendation Must Mention")
        )

        status_correct = found and actual_status == expected_status
        if expected_status == "Completely Missing":
            evidence_correct = found and not evidence and not page
        else:
            evidence_correct = (
                found
                and bool(evidence)
                and bool(page)
                and _all_phrases_present(expected_evidence, evidence)
            )
        missing_elements_correct = (
            found
            and (
                not expected_missing
                or expected_missing.issubset(actual_missing)
            )
        )
        if expected_status == "Completely Covered":
            recommendation_correct = (
                found
                and not _clean(row.get("Policy Gap and Recommendations"))
                and not _clean(row.get("Draft Policy Clause"))
            )
        else:
            required_fields_present = all(
                _clean(row.get(column))
                for column in (
                    "Policy Gap and Recommendations",
                    "Draft Policy Clause",
                    "Recommendation Owner",
                    "Target Timeframe",
                    "Implementation Evidence",
                )
            )
            recommendation_correct = (
                found
                and required_fields_present
                and _all_phrases_present(
                    expected_recommendation,
                    recommendation_bundle,
                )
            )
        evaluated.append(
            {
                "section": _clean(row.get("Section")),
                "expected_status": expected_status,
                "actual_status": actual_status or "(missing row)",
                "status_correct": status_correct,
                "evidence_correct": evidence_correct,
                "missing_elements_correct": missing_elements_correct,
                "recommendation_correct": recommendation_correct,
            }
        )

    population = len(evaluated)
    status_correct = sum(item["status_correct"] for item in evaluated)
    evidence_correct = sum(item["evidence_correct"] for item in evaluated)
    missing_correct = sum(item["missing_elements_correct"] for item in evaluated)
    recommendation_correct = sum(
        item["recommendation_correct"] for item in evaluated
    )
    expected_noncomplete = [
        item for item in evaluated
        if item["expected_status"] != "Completely Covered"
    ]
    expected_nonmissing = [
        item for item in evaluated
        if item["expected_status"] != "Completely Missing"
    ]
    false_complete = sum(
        item["actual_status"] == "Completely Covered"
        for item in expected_noncomplete
    )
    false_missing = sum(
        item["actual_status"] == "Completely Missing"
        for item in expected_nonmissing
    )
    status_accuracy = _percentage(status_correct, population)
    evidence_accuracy = _percentage(evidence_correct, population)
    missing_accuracy = _percentage(missing_correct, population)
    recommendation_accuracy = _percentage(recommendation_correct, population)
    overall = round(
        (
            status_accuracy
            + evidence_accuracy
            + missing_accuracy
            + recommendation_accuracy
        )
        / 4,
        2,
    )
    return {
        "population": population,
        "coverage_status_accuracy_percentage": status_accuracy,
        "evidence_grounding_accuracy_percentage": evidence_accuracy,
        "missing_element_accuracy_percentage": missing_accuracy,
        "recommendation_accuracy_percentage": recommendation_accuracy,
        "overall_benchmark_percentage": overall,
        "false_complete_count": false_complete,
        "false_complete_rate_percentage": _percentage(
            false_complete,
            len(expected_noncomplete),
        ),
        "false_missing_count": false_missing,
        "false_missing_rate_percentage": _percentage(
            false_missing,
            len(expected_nonmissing),
        ),
        "mentor_gap_threshold_met": status_accuracy >= 70.0,
        "rows": evaluated,
    }


def score_extraction_benchmark(
    expected: pd.DataFrame,
    register_path: Path,
) -> Dict[str, Any]:
    if expected.empty:
        return {
            "population": 0,
            "checks_passed": 0,
            "accuracy_percentage": 100.0,
            "splitting_errors": 0,
            "ocr_cleaning_errors": 0,
            "rows": [],
        }
    register = pd.read_excel(
        register_path,
        sheet_name="Obligations",
        dtype={"Section": str},
    )
    by_section = {
        str(row["Section"]): row
        for _, row in register.iterrows()
    }
    evaluated: List[Dict[str, Any]] = []
    splitting_errors = 0
    ocr_cleaning_errors = 0
    for _, expected_row in expected.iterrows():
        section = _clean(expected_row.get("Section"))
        actual = by_section.get(section)
        check_type = _clean(expected_row.get("Check Type"))
        passed = actual is not None
        detail = ""
        if actual is None:
            detail = "Expected section is missing from the obligation register."
        elif check_type == "Actionable":
            expected_actionable = _normal(
                expected_row.get("Expected Actionable")
            ) in {"yes", "true", "1"}
            actual_actionable = _normal(actual.get("Actionable")) in {
                "yes",
                "true",
                "1",
            }
            passed = actual_actionable == expected_actionable
            detail = (
                f"Expected actionable={expected_actionable}; "
                f"actual={actual_actionable}."
            )
            if not passed:
                splitting_errors += 1
        elif check_type == "Text Cleanliness":
            text = f"{_clean(actual.get('Language from Directive'))} {_clean(actual.get('Obligation'))}"
            required = _required_phrases(
                expected_row.get("Required Clean Phrase")
            )
            forbidden = _required_phrases(
                expected_row.get("Forbidden OCR Pattern")
            )
            passed = _all_phrases_present(required, text) and not any(
                re.search(pattern, text, flags=re.I)
                for pattern in forbidden
            )
            detail = "Required phrase and forbidden OCR-pattern check."
            if not passed:
                ocr_cleaning_errors += 1
        else:
            passed = False
            detail = f"Unsupported extraction check type: {check_type}"
        evaluated.append(
            {
                "section": section,
                "check_type": check_type,
                "passed": passed,
                "detail": detail,
            }
        )

    checks_passed = sum(item["passed"] for item in evaluated)
    return {
        "population": len(evaluated),
        "checks_passed": checks_passed,
        "accuracy_percentage": _percentage(checks_passed, len(evaluated)),
        "splitting_errors": splitting_errors,
        "ocr_cleaning_errors": ocr_cleaning_errors,
        "mentor_obligation_threshold_met": (
            _percentage(checks_passed, len(evaluated)) >= 80.0
        ),
        "rows": evaluated,
    }


def score_recommendation_benchmark(
    expected: pd.DataFrame,
    actual_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Score complete remediation packages, not merely populated fields."""
    if expected.empty:
        return {
            "population": 0,
            "packages_correct": 0,
            "accuracy_percentage": 100.0,
            "rows": [],
        }
    required_columns = {
        "Section",
        "Expected Gap Type",
        "Recommendation Must Mention",
        "Draft Must Mention",
        "Owner Must Mention",
        "Timeframe Must Mention",
        "Evidence Must Mention",
        "Forbidden Pattern",
    }
    missing_columns = sorted(required_columns - set(expected.columns))
    if missing_columns:
        raise ValueError(
            "Recommendation benchmark is missing required columns: "
            + ", ".join(missing_columns)
        )
    actual = _actual_frame(actual_rows)
    joined = expected.merge(
        actual,
        on="Section",
        how="left",
        suffixes=("_Expected", "_Actual"),
        indicator=True,
    )
    evaluated: List[Dict[str, Any]] = []
    for _, row in joined.iterrows():
        found = row["_merge"] == "both"
        status = _clean(row.get("Coverage Status"))
        recommendation = _clean(row.get("Policy Gap and Recommendations"))
        draft = _clean(row.get("Draft Policy Clause"))
        owner = _clean(row.get("Recommendation Owner"))
        timeframe = _clean(row.get("Target Timeframe"))
        evidence = _clean(row.get("Implementation Evidence"))
        gap_type = _clean(row.get("Gap Type"))
        forbidden_pattern = _clean(row.get("Forbidden Pattern"))
        field_results = {
            "gap_type": (
                found
                and gap_type == _clean(row.get("Expected Gap Type"))
            ),
            "recommendation": (
                found
                and _all_phrases_present(
                    _required_phrases(row.get("Recommendation Must Mention")),
                    recommendation,
                )
            ),
            "draft_clause": (
                found
                and _all_phrases_present(
                    _required_phrases(row.get("Draft Must Mention")),
                    draft,
                )
            ),
            "owner": (
                found
                and _all_phrases_present(
                    _required_phrases(row.get("Owner Must Mention")),
                    owner,
                )
            ),
            "timeframe": (
                found
                and _all_phrases_present(
                    _required_phrases(row.get("Timeframe Must Mention")),
                    timeframe,
                )
            ),
            "implementation_evidence": (
                found
                and _all_phrases_present(
                    _required_phrases(row.get("Evidence Must Mention")),
                    evidence,
                )
            ),
            "forbidden_wording_absent": (
                found
                and (
                    not forbidden_pattern
                    or not re.search(
                        forbidden_pattern,
                        " ".join((recommendation, draft, owner, timeframe, evidence)),
                        flags=re.I,
                    )
                )
            ),
            "gap_status": status in {"Partially Covered", "Completely Missing"},
        }
        correct = found and all(field_results.values())
        evaluated.append(
            {
                "section": _clean(row.get("Section")),
                "correct": correct,
                "checks": field_results,
                "failed_checks": [
                    name for name, passed in field_results.items()
                    if not passed
                ],
            }
        )

    population = len(evaluated)
    packages_correct = sum(item["correct"] for item in evaluated)
    component_names = list(evaluated[0]["checks"]) if evaluated else []
    component_accuracy = {
        f"{name}_accuracy_percentage": _percentage(
            sum(item["checks"][name] for item in evaluated),
            population,
        )
        for name in component_names
    }
    return {
        "population": population,
        "packages_correct": packages_correct,
        "accuracy_percentage": _percentage(packages_correct, population),
        **component_accuracy,
        "rows_requiring_correction": [
            item["section"] for item in evaluated if not item["correct"]
        ],
        "rows": evaluated,
    }


def score_benchmark(
    known_answer_path: Path,
    actual_gap_rows: List[Dict[str, Any]],
    *,
    obligation_register_path: Path | None = None,
) -> Dict[str, Any]:
    known = load_known_answers(known_answer_path)
    report = {
        "gap": score_gap_benchmark(known["gap"], actual_gap_rows),
        "recommendations": score_recommendation_benchmark(
            known["recommendations"],
            actual_gap_rows,
        ),
    }
    if obligation_register_path is not None:
        report["obligation_extraction"] = score_extraction_benchmark(
            known["extraction"],
            obligation_register_path,
        )
    return report
