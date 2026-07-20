from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import fitz
import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text
from app.services.crawler_service import CrawlerService
from app.services.gap_service import (
    VALID_STATUSES,
    _apply_gemini_assessment,
    _is_structural_parent,
    _jurisdiction_mismatch,
    chunk_policy_text,
    coverage_status,
    recommendation_for,
    review_policy_gaps,
)
from app.services.obligation_service import extract_obligations_from_pdf


def make_pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 545, 790), text, fontsize=10)
    document.save(path)
    document.close()


class BreakdownTests(unittest.TestCase):
    def test_clause_markers_preserve_hierarchy_without_splitting_dates_or_amounts(self) -> None:
        raw = """--- Page 1 | method=native ---
        Circular issued 30 January 2026. The amount is 1.25 million.
        1.
        PURPOSE
        The insurer must maintain controls.
        2. REQUIREMENTS
        2.1 The insurer shall notify the authority within 10 days.
        2.2.1 Records must be retained.
        """
        rows = breakdown_regulatory_text(raw)
        self.assertEqual([row["Section"] for row in rows], ["Introduction", "1", "2", "2.1", "2.2.1"])
        self.assertEqual(rows[1]["Page"], "1")
        self.assertIn("1.25 million", rows[0]["Language from Directive"])


class CrawlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CrawlerService()

    def test_archived_report_is_not_normalised_as_a_directive(self) -> None:
        row = {
            "File": {"Name": "1999streport.pdf", "ServerRelativeUrl": "/Regulatory Frameworks/Archived Documents/1999streport.pdf"},
            "Created": "2018-01-01T00:00:00Z",
        }
        self.assertIsNone(self.service._normalise_sp_item(row, "test", 1))

    def test_explicit_fsca_category_is_kept(self) -> None:
        row = {
            "File": {"Name": "Directive 200.A.i.pdf", "ServerRelativeUrl": "/Enforcement-Matters/Directives/Directive 200.A.i.pdf"},
            "Category1": "Insurer / Micro Insurer",
            "Year0": "2024",
            "Document_x0020_No": "200.A.i",
        }
        record = self.service._normalise_sp_item(row, "test", 1)
        self.assertIsNotNone(record)
        self.assertEqual(record.section, "Insurer / Micro Insurer")
        self.assertEqual(record.year, "2024")

    def test_reference_fallback_requires_exact_identity(self) -> None:
        generic = {"title": "Joint directive guidance", "filename": "guidance.pdf", "document_no": "2.0", "description": ""}
        self.assertIsNone(self.service._reference_match(generic))
        exact = {"title": "Directive 159.A.i", "filename": "Directive 159.A.i.pdf", "document_no": "159.A.i", "description": ""}
        match = self.service._reference_match(exact)
        self.assertIsNotNone(match)
        self.assertIn("159", match.name)


class WorkflowTests(unittest.TestCase):
    def test_native_pdf_extraction_and_output_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "Directive 240.A.i.pdf"
            make_pdf(pdf, [
                "DIRECTIVE 240.A.i\n1. PURPOSE\nThis Directive applies to insurers.\n"
                "2. REQUIREMENTS\n2.1 The insurer must maintain a board-approved policy.\n"
                "2.2 The insurer shall notify the FSCA within 10 business days."
            ])
            result = extract_obligations_from_pdf(pdf)
            sections = [row["Section"] for row in result["tabs"]["text_breakdown"]]
            self.assertIn("2.1", sections)
            self.assertGreater(result["kpis"][1]["value"], 0)
            output = Path(__file__).resolve().parents[1] / "storage" / "generated_outputs" / result["output_files"]["excel"]
            with pd.ExcelFile(output) as workbook:
                self.assertEqual(workbook.sheet_names, ["Obligations", "Text Breakdown", "Statistics", "Process Log"])

    def test_policy_review_uses_three_statuses_and_reconciles_kpis(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            register = root / "register.csv"
            pd.DataFrame([
                {
                    "Section": "1.1", "Language from Directive": "The insurer must maintain a board-approved outsourcing policy.",
                    "Obligation": "The insurer must maintain a board-approved outsourcing policy.", "Obligation Category": "Governance",
                    "Primary Responsible Department": "Legal & Compliance", "Support Function": "Regulatory Compliance", "Priority": "High", "Actionable": "Yes",
                },
                {
                    "Section": "1.2", "Language from Directive": "The insurer must notify the FSCA within 10 business days.",
                    "Obligation": "The insurer must notify the FSCA within 10 business days.", "Obligation Category": "Regulatory reporting and returns",
                    "Primary Responsible Department": "Legal & Compliance", "Support Function": "Regulatory Compliance", "Priority": "High", "Actionable": "Yes",
                },
                {
                    "Section": "1.3", "Language from Directive": "Background information only.",
                    "Obligation": "Informational or contextual text; no standalone implementation obligation is created.", "Obligation Category": "Informational / Context",
                    "Primary Responsible Department": "Legal & Compliance", "Support Function": "Regulatory Compliance", "Priority": "Low", "Actionable": "No",
                },
            ]).to_csv(register, index=False)
            policy = root / "Internal Policy.pdf"
            make_pdf(policy, [
                "1. GOVERNANCE\nThe company must maintain a board-approved outsourcing policy.\n"
                "2. REPORTING\nOperational incidents are reported internally each month."
            ])
            result = review_policy_gaps(register, policy)
            rows = result["tabs"]["gap_assessment"]
            self.assertTrue({row["Coverage Status"] for row in rows}.issubset(VALID_STATUSES))
            self.assertEqual(sum(item["value"] for item in result["kpis"][1:]), result["kpis"][0]["value"])
            for row in rows:
                if row["Coverage Status"] == "Completely Missing":
                    self.assertEqual(row["Corresponding Policy Text"], "")
                if row["Section"] == "1.1" and row["Coverage Status"] == "Completely Covered":
                    self.assertEqual(row["Policy Gap and Recommendations"], "")
            output = Path(__file__).resolve().parents[1] / "storage" / "generated_outputs" / result["output_files"]["excel"]
            with pd.ExcelFile(output) as workbook:
                self.assertEqual(workbook.sheet_names, ["Executive Summary", "Gap Assessment", "Statistics", "Process Log"])
            self.assertIn("Review Rationale", rows[0])

    def test_policy_page_markers_support_native_and_ocr_formats(self) -> None:
        chunks = chunk_policy_text(
            "--- Page 1 | method=native ---\n1. First policy section.\n"
            "--- Page 2 | method=ocr | rotation=90 ---\n2. Second policy section."
        )
        self.assertEqual({chunk["page"] for chunk in chunks}, {"1", "2"})

    def test_foreign_regulator_evidence_cannot_prove_fsca_coverage(self) -> None:
        directive = "The insurer must notify the Registrar before outsourcing this South African insurance function."
        evidence = "The company shall notify the Saudi Arabia Insurance Authority before outsourcing."
        self.assertTrue(_jurisdiction_mismatch(directive, directive, evidence))
        self.assertEqual(coverage_status(0.9, 0.8, evidence, True), "Partially Covered")

    def test_fallback_recommendation_uses_material_gaps_not_random_words(self) -> None:
        directive = "This Directive applies to all aspects of the insurance business that are outsourced, but does not apply to intermediary services."
        obligation = "The South African insurer must apply Directive 159 to the defined outsourcing scope."
        evidence = "The policy discusses general outsourcing risk for Saudi Arabia."
        recommendation = recommendation_for(
            "Partially Covered",
            obligation,
            section="3.2",
            directive_text=directive,
            evidence=evidence,
        )
        self.assertIn("South African", recommendation)
        self.assertIn("intermediary services", recommendation)
        self.assertNotIn("Explicitly address the missing elements", recommendation)
        self.assertNotIn("applicability, scope, provision, applies", recommendation)

    def test_unfinished_parent_stem_is_assessed_through_children(self) -> None:
        register = pd.DataFrame([
            {"Section": "7.2", "Language from Directive": "An outsourcing policy must, at least —"},
            {"Section": "7.2.1", "Language from Directive": "set out the outsourcing governance requirements;"},
        ])
        self.assertTrue(_is_structural_parent(register, 0))

    def test_gemini_assessment_is_grounded_and_jurisdiction_validated(self) -> None:
        evidence = "The policy requires notification to the Saudi Arabia Insurance Authority before outsourcing."
        task = {
            "id": "row-1",
            "section": "8.1",
            "directive_text": "The insurer must notify the Registrar before outsourcing under this Directive.",
            "obligation": "The insurer must notify the South African Registrar before outsourcing.",
            "candidates": [{
                "candidate_id": "candidate-1", "page": "4", "text": evidence,
                "score": 0.8, "keyword_score": 0.7, "hits": ["notify", "outsourcing"],
            }],
        }
        assessment = {
            "coverage_status": "Completely Covered",
            "candidate_id": "candidate-1",
            "evidence_quote": "requires notification to the Saudi Arabia Insurance Authority before outsourcing",
            "rationale": "The policy has a notification control, but it names a foreign regulator.",
            "recommendation": "Add an FSCA notification clause for South African outsourcing arrangements.",
        }
        result = _apply_gemini_assessment(task, assessment)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "Partially Covered")
        self.assertEqual(result["page"], "4")
        self.assertIn("Saudi Arabia Insurance Authority", result["evidence"])

    def test_review_workflow_uses_validated_gemini_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            register = root / "register.csv"
            pd.DataFrame([{
                "Section": "3.2",
                "Language from Directive": "Directive 159 applies to all outsourced aspects of the South African insurance business but excludes intermediary services.",
                "Obligation": "The insurer must define the South African scope of Directive 159 outsourcing requirements.",
                "Obligation Category": "Regulatory Compliance",
                "Primary Responsible Department": "Legal & Compliance",
                "Support Function": "Regulatory Compliance",
                "Priority": "High",
                "Actionable": "Yes",
            }]).to_csv(register, index=False)
            policy = root / "Policy.pdf"
            make_pdf(policy, [
                "SCOPE\nThe policy applies to material outsourcing arrangements in Saudi Arabia. "
                "Business owners must identify material arrangements and document the applicable internal approval before implementation. "
                "The compliance function maintains the outsourcing register and reviews it annually."
            ])
            gemini_response = {
                "assessments": [{
                    "id": "row-0",
                    "coverage_status": "Partially Covered",
                    "candidate_id": "candidate-1",
                    "evidence_quote": "The policy applies to material outsourcing arrangements in Saudi Arabia.",
                    "rationale": "The policy defines outsourcing scope, but only for Saudi Arabia and without the intermediary-services exclusion.",
                    "recommendation": "Add a South African scope clause stating that Directive 159 applies to every outsourced aspect of the insurer's insurance business and expressly excludes intermediary services.",
                }]
            }
            ranked = [{
                "candidate_id": "candidate-1", "page": "1",
                "text": "The policy applies to material outsourcing arrangements in Saudi Arabia.",
                "score": 0.8, "keyword_score": 0.6, "hits": ["outsourcing"],
            }]
            with patch.dict(os.environ, {"ENABLE_LLM_GAP_REVIEW": "true"}), patch(
                "app.services.gap_service.chat_json", return_value=gemini_response
            ), patch("app.services.gap_service.rank_policy_matches", return_value=ranked):
                result = review_policy_gaps(register, policy)
            row = result["tabs"]["gap_assessment"][0]
            self.assertEqual(row["Coverage Status"], "Partially Covered")
            self.assertIn("expressly excludes intermediary services", row["Policy Gap and Recommendations"])
            self.assertIn("Saudi Arabia", row["Corresponding Policy Text"])
            self.assertIn("Gemini produced 1 validated assessment", result["logs"][2]["message"])


if __name__ == "__main__":
    unittest.main()
