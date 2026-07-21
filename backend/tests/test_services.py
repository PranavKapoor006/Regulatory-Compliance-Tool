from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import fitz
import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text
from app.services.crawler_service import CrawlerService, _extract_launch_year
from app.services.gap_service import (
    VALID_STATUSES,
    _apply_gemini_assessment,
    _fallback_assessment,
    _gemini_assessments,
    _validated_gemini_assessments,
    _is_structural_parent,
    _jurisdiction_mismatch,
    chunk_policy_text,
    coverage_status,
    load_register,
    recommendation_for,
    review_policy_gaps,
)
from app.services.obligation_service import extract_obligations_from_pdf, generate_obligation, is_actionable


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

    def test_embedded_consecutive_child_clauses_are_split(self) -> None:
        raw = (
            "7.7.9 provide for periodic performance reviews; "
            "7.7.10 specify continued access to information; "
            "7.7.11 address confidentiality and privacy."
        )
        rows = breakdown_regulatory_text(raw)
        self.assertEqual([row["Section"] for row in rows], ["7.7.9", "7.7.10", "7.7.11"])
        self.assertNotIn("7.7.10", rows[0]["Language from Directive"])

    def test_parent_marker_with_closing_parenthesis_is_split_from_previous_clause(self) -> None:
        raw = (
            "6.1 The board remains responsible regardless of outsourcing. "
            "6.2) An insurer may not outsource a function if that outsourcing may —\n"
            "6.2.1 materially increase risk to the insurer;"
        )
        rows = breakdown_regulatory_text(raw)
        self.assertEqual([row["Section"] for row in rows], ["6.1", "6.2", "6.2.1"])
        self.assertNotIn("6.2)", rows[0]["Language from Directive"])


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

    def test_year_precedence_ignores_sharepoint_migration_dates(self) -> None:
        values = {
            "Year0": "2012",
            "Created": "2025-04-01T00:00:00Z",
            "Modified": "2026-01-01T00:00:00Z",
        }
        self.assertEqual(_extract_launch_year(values, "Directive 159.A.i.pdf"), "2012")

    def test_true_issue_date_has_priority_over_year_and_filename(self) -> None:
        values = {"IssueDate": "15 March 2014", "Year0": "2013", "Created": "2025-01-01"}
        self.assertEqual(_extract_launch_year(values, "Directive 100.A.i 2012.pdf"), "2014")

    def test_filename_year_precedes_created_date_and_rejects_future_noise(self) -> None:
        values = {"Created": "2025-01-01T00:00:00Z"}
        self.assertEqual(_extract_launch_year(values, "Directive 12.A.i issued 2011.pdf"), "2011")
        self.assertEqual(_extract_launch_year({}, "Directive strategy 2099.pdf"), "Unknown")

    def test_year_filter_is_exact(self) -> None:
        records = [{"year": "2012"}, {"year": "2021"}, {"year": "Unknown"}]
        self.assertEqual(self.service._filter_records(records, None, "2012"), [{"year": "2012"}])


class WorkflowTests(unittest.TestCase):
    def test_live_directive_159_row_shapes_are_repaired_from_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            register = Path(folder) / "legacy_output.xlsx"
            base = {
                "Obligation Category": "Governance",
                "Primary Responsible Department": "Legal & Compliance",
                "Support Function": "Regulatory Compliance",
                "Priority": "Low",
                "Actionable": "No",
            }
            live_rows = [
                {**base, "Section": "6.1", "Language from Directive": "The beard of directors and managing executives of an insurer remain responsible fer the insurance ousiness of the insurer, regardless of any outsourcing. Principies with which any outsourcing must comply 6.2) An insurer may not outsource any function or activity if that outsourcing may —", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "6.2.1", "Language from Directive": "materially increase risk to the insurer;", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "6.2.2", "Language from Directive": "materially impair the quality of the governance framework of the insurer, including the insurer's ability to manage its risks and meet its legal and regulatory obligatians;", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "6.2.3", "Language from Directive": "impair the ability of the Registrar te monitor the insurers compliance with its regulatory obligations; and", "Obligation": "The regulated entity must comply with this requirement: impair the ability of the Registrar te monitor the insurers compliance with its regulatory obligations."},
                {**base, "Section": "6.2.4", "Language from Directive": "compromise the fair treatment of or continuous and satisfactory service to policyholders.", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "7.7", "Language from Directive": "A written contract must, at least, -", "Obligation": "Parent clause only; the actionable requirements are captured in the numbered child clauses that follow."},
                {**base, "Section": "7.7.12", "Language from Directive": "address sub-outsourcing;", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "7.11", "Language from Directive": "An insurer must regularly assess the other person's -", "Obligation": "Parent clause only; the actionable requirements are captured in the numbered child clauses that follow."},
                {**base, "Section": "7.11.2", "Language from Directive": "ability to comply with applicable laws; and", "Obligation": "The regulated entity must comply with this requirement: ability to comply with applicable laws."},
            ]
            with pd.ExcelWriter(register, engine="xlsxwriter") as writer:
                pd.DataFrame({"Summary": ["legacy export"]}).to_excel(writer, sheet_name="Executive Summary", index=False)
                pd.DataFrame(live_rows).to_excel(writer, sheet_name="Gap Assessment", index=False)
            repaired = load_register(register)
            by_section = repaired.set_index("Section")
            self.assertIn("must remain responsible", by_section.loc["6.1", "Obligation"])
            self.assertIn("must not outsource", by_section.loc["6.2.1", "Obligation"])
            self.assertIn("must not outsource", by_section.loc["6.2.2", "Obligation"])
            self.assertIn("must not outsource", by_section.loc["6.2.3", "Obligation"])
            self.assertIn("must not outsource", by_section.loc["6.2.4", "Obligation"])
            self.assertEqual(by_section.loc["7.7.12", "Obligation"], "A written contract must, at least, address sub-outsourcing.")
            self.assertIn("must regularly assess", by_section.loc["7.11.2", "Obligation"])

    def test_missing_gemini_batch_rows_are_retried_individually(self) -> None:
        tasks = [
            {"id": f"row-{index}", "section": str(index), "directive_text": "The insurer must comply.", "obligation": "The insurer must comply.", "category": "Governance", "candidates": []}
            for index in range(3)
        ]
        first = {"assessments": [{"id": "row-0", "coverage_status": "Completely Missing", "candidate_id": "", "evidence_quote": "", "rationale": "Missing", "recommendation": "Add the control."}]}
        retries = [
            {"assessments": [{"coverage_status": "Completely Missing", "candidate_id": "", "evidence_quote": "", "rationale": "Missing", "recommendation": "Add the control."}]},
            {"assessments": [{"id": "wrong-id", "coverage_status": "Completely Missing", "candidate_id": "", "evidence_quote": "", "rationale": "Missing", "recommendation": "Add the control."}]},
        ]
        with patch.dict(os.environ, {"ENABLE_LLM_GAP_REVIEW": "true", "GAP_REVIEW_BATCH_SIZE": "3"}), patch(
            "app.services.gap_service.chat_json", side_effect=[first, *retries]
        ):
            results = _gemini_assessments(tasks)
        self.assertEqual(set(results), {"row-0", "row-1", "row-2"})

    def test_invalid_gemini_row_receives_focused_retry(self) -> None:
        task = {
            "id": "row-1",
            "section": "8.1.1",
            "directive_text": "The insurer must notify the Registrar of the proposed outsourcing.",
            "obligation": "The insurer must notify the Registrar of the proposed outsourcing.",
            "category": "Regulatory reporting and returns",
            "candidates": [{
                "candidate_id": "candidate-1", "page": "3",
                "text": "The business owner records proposed outsourcing for internal review.",
                "score": 0.5, "keyword_score": 0.4, "hits": ["outsourcing"],
            }],
        }
        invalid = {"assessments": [{"id": "row-1", "coverage_status": "maybe", "rationale": "Unclear"}]}
        valid = {"assessments": [{
            "id": "row-1", "coverage_status": "Partially Covered", "candidate_id": "candidate-1",
            "evidence_quote": "records proposed outsourcing for internal review",
            "rationale": "Internal review does not establish Registrar notification.",
            "recommendation": "Add a requirement to notify the South African Registrar before outsourcing.",
        }]}
        with patch.dict(os.environ, {"ENABLE_LLM_GAP_REVIEW": "true", "GAP_REVIEW_BATCH_SIZE": "1"}), patch(
            "app.services.gap_service.chat_json", side_effect=[invalid, valid]
        ):
            results = _validated_gemini_assessments([task])
        self.assertEqual(results["row-1"]["status"], "Partially Covered")

    def test_old_register_is_repaired_before_gap_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            register = Path(folder) / "old_register.csv"
            base = {
                "Obligation Category": "Governance",
                "Primary Responsible Department": "Legal & Compliance",
                "Support Function": "Regulatory Compliance",
                "Priority": "Low",
                "Actionable": "No",
            }
            pd.DataFrame([
                {**base, "Section": "6.1", "Language from Directive": "The board of directors and managing executives of an insurer remain responsible for the insurance business, regardless of outsourcing. Principles with which any outsourcing must comply 6.2) An insurer may not outsource any function if that outsourcing may —", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "6.2.1", "Language from Directive": "materially increase risk to the insurer;", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "6.2.3", "Language from Directive": "impair the ability of the Registrar to monitor compliance;", "Obligation": "Informational or contextual text; no standalone implementation obligation is created."},
                {**base, "Section": "7.7", "Language from Directive": "A written contract must, at least, —", "Obligation": "Parent clause only; the actionable requirements are captured in the numbered child clauses that follow."},
                {**base, "Section": "7.7.8", "Language from Directive": "provide that an insurer must monitor the other person's performance under the contract;", "Obligation": "provide that an insurer must monitor the other person's performance under the contract.", "Actionable": "Yes"},
            ]).to_csv(register, index=False)
            repaired = load_register(register)
            self.assertEqual(repaired["Section"].astype(str).tolist(), ["6.1", "6.2", "6.2.1", "6.2.3", "7.7", "7.7.8"])
            self.assertIn("remain responsible", repaired.iloc[0]["Obligation"])
            self.assertEqual(repaired.iloc[1]["Actionable"], "No")
            self.assertIn("must not outsource", repaired.iloc[2]["Obligation"])
            self.assertIn("must not outsource", repaired.iloc[3]["Obligation"])
            self.assertTrue(repaired.iloc[5]["Obligation"].startswith("A written contract must"))

    def test_gemini_status_and_candidate_format_variants_are_accepted(self) -> None:
        evidence = "The policy requires annual assessment of each service provider."
        task = {
            "id": "row-1", "section": "7.11.2",
            "directive_text": "An insurer must regularly assess the service provider.",
            "obligation": "An insurer must regularly assess the service provider.",
            "candidates": [{"candidate_id": "candidate-1", "page": "8", "text": evidence, "score": 0.8, "keyword_score": 0.7, "hits": ["assess"]}],
        }
        result = _apply_gemini_assessment(task, {
            "coverage_status": "fully covered", "candidate_id": "candidate_1",
            "evidence_quote": "requires annual assessment of each service provider",
            "rationale": "The annual assessment control is explicit.", "recommendation": "",
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "Completely Covered")

    def test_prohibition_recommendation_never_reverses_must_not(self) -> None:
        obligation = generate_obligation(
            "6.2.3",
            "impair the ability of the Registrar to monitor compliance;",
            "An insurer may not outsource any function if that outsourcing may —",
        )
        self.assertIn("must not outsource", obligation)
        recommendation = recommendation_for("Completely Missing", obligation, section="6.2.3", directive_text=obligation)
        self.assertIn("must not outsource", recommendation)

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
            self.assertEqual(result["pipeline"]["pipeline_version"], "2026-07-21.3")
            self.assertTrue(result["pipeline"]["run_id"])
            self.assertEqual(result["logs"][0]["stage"], "Pipeline")
            self.assertTrue(any(log["stage"] == "Quality Control" for log in result["logs"]))
            self.assertIn(result["pipeline"]["run_id"], result["output_files"]["excel"])
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

    def test_applicability_parent_stem_is_assessed_through_children(self) -> None:
        register = pd.DataFrame([
            {"Section": "3.4", "Language from Directive": "This Directive also applies to ~"},
            {"Section": "3.4.1", "Language from Directive": "the outsourcing of insurance business conducted by an overseas branch;"},
        ])
        self.assertTrue(_is_structural_parent(register, 0))

    def test_child_obligation_inherits_parent_notification_action(self) -> None:
        obligation = generate_obligation(
            "8.1.1",
            "the proposed outsourcing (subject to requirements under the Acts);",
            "An insurer must notify the Registrar of —",
        )
        self.assertIn("must notify the Registrar", obligation)
        self.assertIn("proposed outsourcing", obligation)

    def test_short_child_clause_inherits_written_contract_requirement(self) -> None:
        parent = "A written contract must, at least, —"
        self.assertTrue(is_actionable("address sub-outsourcing;", parent))
        obligation = generate_obligation("7.7.12", "address sub-outsourcing;", parent)
        self.assertEqual(obligation, "A written contract must, at least, address sub-outsourcing.")

    def test_retained_board_responsibility_is_actionable(self) -> None:
        wording = "The board of directors and managing executives of an insurer remain responsible for the insurance business regardless of outsourcing."
        self.assertTrue(is_actionable(wording))
        self.assertIn("remain responsible", generate_obligation("6.1", wording))

    def test_child_with_incidental_action_word_inherits_primary_parent_action(self) -> None:
        obligation = generate_obligation(
            "7.11.2",
            "ability to comply with applicable laws; and",
            "An insurer must regularly assess the other person's —",
        )
        self.assertIn("must regularly assess", obligation)
        self.assertIn("ability to comply with applicable laws", obligation)

    def test_child_inheritance_discards_ocr_footnote_after_parent_dash(self) -> None:
        obligation = generate_obligation(
            "8.1.2",
            "the details of the other person to whom the insurer will outsource that function;",
            "An insurer must notify the Registrar of — Bireaive (98 CT 4 61) garbled footnote text",
        )
        self.assertIn("must notify the Registrar", obligation)
        self.assertNotIn("Bireaive", obligation)

    def test_legal_act_number_is_not_truncated(self) -> None:
        obligation = generate_obligation(
            "3.3",
            "This Directive applies to a related party as defined in section 1 of the Companies Act No. 71 of 2008, including a person outside South Africa.",
        )
        self.assertIn("Companies Act No. 71 of 2008", obligation)
        self.assertIn("outside South Africa", obligation)

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

    def test_internal_information_does_not_cover_regulator_notification_child(self) -> None:
        evidence = "The business owner prepares details of the proposed outsourcing for internal board approval."
        task = {
            "id": "row-1",
            "section": "8.1.1",
            "directive_text": "An insurer must notify the Registrar of the proposed outsourcing.",
            "obligation": "An insurer must notify the Registrar of the proposed outsourcing.",
            "candidates": [{
                "candidate_id": "candidate-1", "page": "3", "text": evidence,
                "score": 0.8, "keyword_score": 0.6, "hits": ["proposed", "outsourcing"],
            }],
        }
        assessment = {
            "coverage_status": "Completely Covered",
            "candidate_id": "candidate-1",
            "evidence_quote": evidence,
            "rationale": "The policy captures the proposed outsourcing.",
            "recommendation": "",
        }
        result = _apply_gemini_assessment(task, assessment)
        self.assertEqual(result["status"], "Partially Covered")

    def test_risk_description_does_not_cover_written_contract_requirement(self) -> None:
        evidence = "Compliance risk arises when a third party fails to comply with applicable regulations."
        task = {
            "id": "row-1", "section": "7.7.5",
            "directive_text": "A written contract must require that the other person comply with applicable laws.",
            "obligation": "A written contract must require that the other person comply with applicable laws.",
            "candidates": [{"candidate_id": "candidate-1", "page": "8", "text": evidence, "score": 0.8, "keyword_score": 0.7, "hits": ["comply", "laws"]}],
        }
        assessment = {"coverage_status": "Completely Covered", "candidate_id": "candidate-1", "evidence_quote": evidence, "rationale": "Relevant risk is described.", "recommendation": ""}
        result = _apply_gemini_assessment(task, assessment)
        self.assertEqual(result["status"], "Partially Covered")
        fallback = _fallback_assessment(task)
        self.assertEqual(fallback["status"], "Partially Covered")

    def test_risk_description_does_not_cover_regular_assessment(self) -> None:
        evidence = "Compliance risk may arise when a provider fails to comply with applicable laws."
        task = {
            "id": "row-1", "section": "7.11.2",
            "directive_text": "An insurer must regularly assess the other person's ability to comply with applicable laws.",
            "obligation": "An insurer must regularly assess the other person's ability to comply with applicable laws.",
            "candidates": [{"candidate_id": "candidate-1", "page": "8", "text": evidence, "score": 0.8, "keyword_score": 0.7, "hits": ["comply", "laws"]}],
        }
        assessment = {"coverage_status": "Completely Covered", "candidate_id": "candidate-1", "evidence_quote": evidence, "rationale": "Compliance risk is described.", "recommendation": ""}
        result = _apply_gemini_assessment(task, assessment)
        self.assertEqual(result["status"], "Partially Covered")

    def test_fallback_recommendation_does_not_duplicate_actor_and_must(self) -> None:
        recommendation = recommendation_for(
            "Completely Missing",
            "An insurer must notify the Registrar before outsourcing.",
            section="8.1",
            directive_text="An insurer must notify the Registrar before outsourcing.",
        )
        self.assertIn("requiring the insurer to notify the Registrar", recommendation)
        self.assertNotIn("to An insurer must", recommendation)

    def test_fallback_recommendation_handles_applicability_and_actor_conditions(self) -> None:
        applicability = recommendation_for(
            "Completely Missing",
            "The regulated entity must comply with this applicability and scope provision: This Directive applies to all insurers.",
            section="3.1",
            directive_text="This Directive applies to all insurers.",
        )
        conditional_actor = recommendation_for(
            "Completely Missing",
            "An insurer, in respect of every outsourcing to another person, must determine whether the function is material.",
            section="5.2.1",
            directive_text="An insurer must determine whether outsourcing is material.",
        )
        for recommendation in (applicability, conditional_actor):
            self.assertNotRegex(recommendation, r"\bmust\s+(?:An insurer|This Directive)\b")
            self.assertIn("South African FSCA compliance clause", recommendation)

    def test_historical_deadline_recommendation_requires_legacy_review(self) -> None:
        recommendation = recommendation_for(
            "Completely Missing",
            "Legacy outsourcing contracts had to comply by 1 January 2013.",
            section="9.2",
            directive_text="Contracts must comply when extended, renewed or amended, but no later than 1 January 2013.",
        )
        self.assertIn("legacy-contract review", recommendation)
        self.assertIn("historical exception", recommendation)
        self.assertNotIn("by no later than 1 January 2013", recommendation)

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
            gap_log = next(log for log in result["logs"] if log["stage"] == "Gap Analysis")
            self.assertIn("Gemini produced 1 validated assessment", gap_log["message"])


if __name__ == "__main__":
    unittest.main()
