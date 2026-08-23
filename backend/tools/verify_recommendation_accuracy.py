"""Independent Directive 159 recommendation-accuracy gate.

Usage (from backend):
    python tools/verify_recommendation_accuracy.py --register path.xlsx --policy path.pdf

The expected decisions are the clause-level, jurisdiction-neutral classifications
from the 2026-08-18 independent review. The script disables model calls so the
result is reproducible and never consumes API quota.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

os.environ["ENABLE_LLM_GAP_REVIEW"] = "false"
os.environ["EXPORT_INTERNAL_QUALITY_METRICS"] = "true"

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.gap_service import review_policy_gaps  # noqa: E402


COMPLETE = {
    "3.3", "3.4.2", "3.7", "4.3.2", "7.1", "7.2.3", "7.2.4", "7.3",
    "7.4", "7.5.1", "7.5.2", "7.5.3", "7.5.5", "7.5.6", "7.5.7",
    "7.5.8", "7.5.9", "7.7.10", "7.7.11", "7.9", "7.11.1", "7.11.2",
    "7.11.3", "9.1",
}
MISSING = {
    "3.6", "6.4.2", "6.4.3", "6.4.4", "6.5", "7.7.1", "7.7.2",
    "7.7.12", "7.7.13", "7.7.15", "7.7.17", "7.7.19", "7.8",
}
PARTIAL = {
    "1", "3.1", "3.2", "3.4.1", "4.3.1", "5.2.1", "5.2.2", "5.2.3",
    "6.1", "6.2.1", "6.2.2", "6.2.3", "6.2.4", "6.3", "6.4.1", "7.2.1",
    "7.2.2", "7.2.5", "7.5.4", "7.6", "7.7.3", "7.7.4", "7.7.5",
    "7.7.6", "7.7.7", "7.7.8", "7.7.9", "7.7.14", "7.7.16", "7.7.18",
    "7.7.20", "7.10", "8.1.1", "8.1.2", "8.1.3", "8.2", "9.2", "10",
}
EXPECTED = {
    **{section: "Completely Covered" for section in COMPLETE},
    **{section: "Completely Missing" for section in MISSING},
    **{section: "Partially Covered" for section in PARTIAL},
}


def _recommendation_pass(row: dict, expected_status: str) -> bool:
    recommendation = str(row.get("Policy Gap and Recommendations", ""))
    draft = str(row.get("Draft Policy Clause", ""))
    if row.get("Coverage Status") != expected_status:
        return False
    if expected_status == "Completely Covered":
        return not recommendation and not draft
    required_fields = (
        recommendation,
        draft,
        str(row.get("Recommendation Owner", "")),
        str(row.get("Target Timeframe", "")),
        str(row.get("Implementation Evidence", "")),
    )
    if not all(required_fields):
        return False
    if expected_status == "Partially Covered" and not re.search(
        r"retain the supported control.*residual requirement",
        recommendation,
        flags=re.I | re.S,
    ):
        return False
    forbidden_gap_reason = re.compile(
        r"jurisdiction(?:al)? mismatch|wrong jurisdiction|foreign jurisdiction|"
        r"South African / FSCA jurisdiction",
        flags=re.I,
    )
    return not forbidden_gap_reason.search(
        " ".join([
            recommendation,
            str(row.get("Review Rationale", "")),
            str(row.get("Missing Elements", "")),
        ])
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--minimum", type=float, default=70.0)
    args = parser.parse_args()

    result = review_policy_gaps(args.register.resolve(), args.policy.resolve())
    rows = {
        str(row["Section"]): row
        for row in result["tabs"]["gap_assessment"]
        if str(row["Section"]) in EXPECTED
    }
    missing_sections = sorted(set(EXPECTED) - set(rows))
    if missing_sections:
        print("FAIL: benchmark sections missing from the register:", ", ".join(missing_sections))
        return 2

    status_passes = sum(
        rows[section]["Coverage Status"] == expected
        for section, expected in EXPECTED.items()
    )
    recommendation_passes = sum(
        _recommendation_pass(rows[section], expected)
        for section, expected in EXPECTED.items()
    )
    population = len(EXPECTED)
    status_accuracy = round(status_passes / population * 100, 1)
    recommendation_accuracy = round(recommendation_passes / population * 100, 1)
    print(f"Coverage-status accuracy: {status_accuracy:.1f}% ({status_passes}/{population})")
    print(
        f"Recommendation-package accuracy: {recommendation_accuracy:.1f}% "
        f"({recommendation_passes}/{population})"
    )
    print(f"Required minimum: {args.minimum:.1f}%")
    passed = status_accuracy >= args.minimum and recommendation_accuracy >= args.minimum
    print("RESULT:", "PASS" if passed else "FAIL")
    if not passed:
        for section, expected in EXPECTED.items():
            row = rows[section]
            if row["Coverage Status"] != expected or not _recommendation_pass(row, expected):
                print(
                    f"  {section}: expected {expected}; got {row['Coverage Status']}; "
                    "recommendation package "
                    f"{'passed' if _recommendation_pass(row, expected) else 'failed'}"
                )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
