from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


GAP_STATUSES = {"Partially Covered", "Completely Missing"}

SHORTCOMINGS = [
    {
        "number": 1,
        "name": "Improper exclusion of intra-group and foreign providers",
        "sections": {"3.3", "3.4.1", "3.4.2"},
        "evidence": ["applies only to independent service providers", "fall outside this policy"],
        "recommendation": [
            "related",
            "outside of south africa",
            "group regulatory compliance",
            "third-party risk",
            "retrospective inventory",
            "due-diligence reviews",
        ],
    },
    {
        "number": 2,
        "name": "Board and executive accountability improperly transferred",
        "sections": {"6.1"},
        "evidence": ["responsibility for the outsourced activity transfers", "not responsible"],
        "recommendation": [
            "remain responsible and accountable",
            "accountability is not transferred",
            "board risk and capital committee",
            "group chief risk officer",
            "delegated-authority schedule",
            "attestation",
        ],
    },
    {
        "number": 3,
        "name": "Mandatory materiality tests made optional",
        "sections": {"5.2.2", "5.2.3"},
        "evidence": ["optional and may be omitted", "commercially routine"],
        "recommendation": [
            "appropriate internal controls",
            "legal and regulatory requirements",
            "difficulty and time",
            "in-house",
            "may not be waived",
            "third-party risk",
            "retrospective reassessment log",
        ],
    },
    {
        "number": 4,
        "name": "Provider consent can block regulator access",
        "sections": {"7.7.10", "7.7.15"},
        "evidence": ["subject to the provider's prior written consent", "may withhold information"],
        "recommendation": [
            "unrestricted and timely access",
            "not subject to provider consent",
            "commercial-sensitivity",
            "legal",
            "executed amendments",
            "access test",
        ],
    },
    {
        "number": 5,
        "name": "Conflict disclosure and mitigation is optional",
        "sections": {"6.3"},
        "evidence": ["encouraged to disclose", "when convenient", "need not be entered"],
        "recommendation": [
            "disclose",
            "immediately",
            "recorded",
            "assessed by compliance",
            "avoided",
            "mitigation",
            "chief compliance officer",
            "provider declarations",
        ],
    },
    {
        "number": 6,
        "name": "Claims remuneration linked to adverse claim outcomes",
        "sections": {"6.4.4"},
        "evidence": ["performance fee", "claims repudiated", "not paid", "partially paid"],
        "recommendation": [
            "must never be linked",
            "claims repudiated",
            "not paid",
            "partially paid",
            "chief compliance officer",
            "group legal",
            "incentive recalculation",
            "customer-impact review",
        ],
    },
    {
        "number": 7,
        "name": "Policy review cycle exceeds annual minimum",
        "sections": {"7.3"},
        "evidence": ["reviewed once every 24 months"],
        "recommendation": [
            "reviewed at least annually",
            "reviewed and adapted promptly",
            "policy owner",
            "group chief risk officer",
            "30 calendar days",
            "annual review calendar",
            "overdue-review issue log",
        ],
    },
    {
        "number": 8,
        "name": "Regulator notification occurs after effectiveness",
        "sections": {"8.1.1", "8.1.2", "8.1.3"},
        "evidence": ["30 calendar days after", "identify the service provider", "key risks and mitigation"],
        "recommendation": [
            "one month before",
            "effective date",
            "regulatory compliance",
            "15 business days",
            "24-month reconciliation",
            "submission timestamps",
            "late-notice",
            "regulator receipt",
        ],
    },
]


def _normal(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _contains_all(text: str, phrases: list[str]) -> bool:
    normal = _normal(text)
    return all(_normal(phrase) in normal for phrase in phrases)


def score_assessment(path: Path) -> dict[str, Any]:
    frame = pd.read_excel(
        path,
        sheet_name="Gap Assessment",
        dtype={"Section": str},
    ).fillna("")
    frame["Section"] = frame["Section"].astype(str).str.strip()
    actionable = frame[
        frame["Coverage Status"].astype(str).str.strip()
        != "Not Applicable / Informational"
    ].copy()
    expected_gap_sections = set().union(
        *(spec["sections"] for spec in SHORTCOMINGS)
    )
    actual_gap_mask = actionable["Coverage Status"].astype(str).str.strip().isin(
        GAP_STATUSES
    )
    expected_gap_mask = actionable["Section"].isin(expected_gap_sections)
    true_positive = int((actual_gap_mask & expected_gap_mask).sum())
    false_positive = int((actual_gap_mask & ~expected_gap_mask).sum())
    false_negative = int((~actual_gap_mask & expected_gap_mask).sum())
    true_negative = int((~actual_gap_mask & ~expected_gap_mask).sum())
    row_population = len(actionable)
    row_accuracy = (
        (true_positive + true_negative) / row_population * 100
        if row_population
        else 0.0
    )
    row_precision = (
        true_positive / (true_positive + false_positive) * 100
        if true_positive + false_positive
        else 0.0
    )
    row_recall = (
        true_positive / (true_positive + false_negative) * 100
        if true_positive + false_negative
        else 0.0
    )
    by_section = {
        section: group.to_dict(orient="records")
        for section, group in frame.groupby("Section", sort=False)
    }

    results: list[dict[str, Any]] = []
    dimensions = {
        "gap_detection": 0,
        "exact_policy_evidence": 0,
        "regulatory_mapping": 0,
        "recommendation_package": 0,
    }
    for spec in SHORTCOMINGS:
        rows = [
            row
            for section in spec["sections"]
            for row in by_section.get(section, [])
        ]
        found_sections = {str(row.get("Section", "")) for row in rows}
        statuses = {_normal(row.get("Coverage Status", "")) for row in rows}
        expected_statuses = {_normal(status) for status in GAP_STATUSES}
        detection = bool(rows) and statuses and statuses.issubset(expected_statuses)
        mapping = found_sections == spec["sections"] and detection

        evidence_bundle = " ".join(
            str(row.get("Corresponding Policy Text", ""))
            for row in rows
        )
        evidence = mapping and _contains_all(
            evidence_bundle,
            spec["evidence"],
        )

        recommendation_bundle = " ".join(
            str(row.get(column, ""))
            for row in rows
            for column in (
                "Policy Gap and Recommendations",
                "Draft Policy Clause",
                "Recommendation Owner",
                "Target Timeframe",
                "Implementation Evidence",
            )
        )
        required_fields = all(
            all(
                _normal(row.get(column, ""))
                for column in (
                    "Policy Gap and Recommendations",
                    "Draft Policy Clause",
                    "Recommendation Owner",
                    "Target Timeframe",
                    "Implementation Evidence",
                )
            )
            for row in rows
        )
        recommendation = (
            mapping
            and required_fields
            and _contains_all(recommendation_bundle, spec["recommendation"])
        )

        checks = {
            "gap_detection": detection,
            "exact_policy_evidence": evidence,
            "regulatory_mapping": mapping,
            "recommendation_package": recommendation,
        }
        for name, passed in checks.items():
            dimensions[name] += int(passed)
        results.append({
            "number": spec["number"],
            "name": spec["name"],
            "sections": sorted(spec["sections"]),
            "checks": checks,
            "points": sum(int(value) for value in checks.values()),
            "maximum_points": 4,
        })

    points = sum(dimensions.values())
    maximum = len(SHORTCOMINGS) * len(dimensions)
    resolved_path = path.resolve()
    try:
        assessment_path = str(resolved_path.relative_to(Path.cwd().resolve()))
    except ValueError:
        assessment_path = resolved_path.name

    return {
        "benchmark": "ABSA-D159-SYN-001",
        "assessment": assessment_path,
        "shortcomings": len(SHORTCOMINGS),
        "dimensions": {
            name: {
                "passed": passed,
                "maximum": len(SHORTCOMINGS),
                "accuracy_percentage": round(passed / len(SHORTCOMINGS) * 100, 2),
            }
            for name, passed in dimensions.items()
        },
        "score": {
            "points": points,
            "maximum_points": maximum,
            "accuracy_percentage": round(points / maximum * 100, 2),
            "mentor_threshold_met": (points / maximum * 100) >= 70,
        },
        "full_actionable_population": {
            "population": row_population,
            "expected_gap_rows": int(expected_gap_mask.sum()),
            "actual_gap_rows": int(actual_gap_mask.sum()),
            "true_positive_gap_rows": true_positive,
            "false_positive_gap_rows": false_positive,
            "false_negative_gap_rows": false_negative,
            "true_negative_covered_rows": true_negative,
            "classification_accuracy_percentage": round(row_accuracy, 2),
            "gap_precision_percentage": round(row_precision, 2),
            "gap_recall_percentage": round(row_recall, 2),
            "mentor_threshold_met": row_accuracy >= 70.0,
        },
        "rows": results,
        "interpretation": (
            "The 32-point score measures the eight seeded shortcomings. The full "
            "actionable-population score additionally treats every other actionable "
            "row in corrected File 1 v2 as expected covered. Neither score establishes "
            "legal accuracy on arbitrary policies."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score an Aegis synthetic-policy assessment against its hidden 32-point ground truth."
    )
    parser.add_argument("assessment", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = score_assessment(args.assessment)
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
