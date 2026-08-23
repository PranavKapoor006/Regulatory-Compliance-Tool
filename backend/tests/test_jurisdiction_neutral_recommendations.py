from __future__ import annotations

import unittest

from app.services.gap_service import (
    _jurisdiction_mismatch,
    _jurisdiction_neutral_text,
    recommendation_for,
)
from app.services.prompt_service import GAP_REVIEW_SYSTEM_PROMPT


class JurisdictionNeutralRecommendationTests(unittest.TestCase):
    def test_authority_name_is_not_a_coverage_gap(self) -> None:
        directive = "The entity must notify the Registrar before outsourcing."
        evidence = "The entity shall notify the Insurance Authority before outsourcing."
        self.assertFalse(_jurisdiction_mismatch(directive, directive, evidence))

    def test_prompt_scores_substance_not_country_name(self) -> None:
        self.assertIn("jurisdiction-neutrally", GAP_REVIEW_SYSTEM_PROMPT)
        self.assertIn("must not increase or reduce coverage", GAP_REVIEW_SYSTEM_PROMPT)

    def test_partial_recommendation_preserves_supported_control(self) -> None:
        result = recommendation_for(
            "Partially Covered",
            "The entity must monitor service levels and policyholder outcomes.",
            section="7.10",
            directive_text="Monitor service levels and policyholder outcomes.",
            evidence="The entity shall monitor service levels monthly.",
            material_gaps=["policyholder service outcomes"],
        )
        self.assertIn("Retain the supported control", result)
        self.assertIn("only the residual requirement", result)

    def test_generated_clause_is_authority_neutral(self) -> None:
        text = _jurisdiction_neutral_text(
            "The South African insurer must notify the FSCA under FSCA Directive 159."
        )
        self.assertNotIn("South African", text)
        self.assertNotIn("FSCA", text)
        self.assertIn("the regulator", text)
        self.assertIn("applicable directive", text)


if __name__ == "__main__":
    unittest.main()
