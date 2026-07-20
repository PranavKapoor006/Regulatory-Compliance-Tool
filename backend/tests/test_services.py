from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fitz
import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text
from app.services.crawler_service import CrawlerService
from app.services.gap_service import VALID_STATUSES, chunk_policy_text, review_policy_gaps
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

    def test_policy_page_markers_support_native_and_ocr_formats(self) -> None:
        chunks = chunk_policy_text(
            "--- Page 1 | method=native ---\n1. First policy section.\n"
            "--- Page 2 | method=ocr | rotation=90 ---\n2. Second policy section."
        )
        self.assertEqual({chunk["page"] for chunk in chunks}, {"1", "2"})


if __name__ == "__main__":
    unittest.main()
