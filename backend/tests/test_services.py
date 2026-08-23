from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

import fitz
import pandas as pd

from app.services.breakdown_service import breakdown_regulatory_text
from app.services.benchmark_service import (
    score_extraction_benchmark,
    score_gap_benchmark,
    score_recommendation_benchmark,
)
from app.services.crawler_service import (
    CRAWLER_VERSION,
    CrawlerService,
    DIRECTIVES_LIST_GUID,
    DirectiveRecord,
    EXPECTED_CATEGORY_COUNTS,
    MAX_DOWNLOAD_BATCH,
    _extract_launch_year,
    _valid_pdf_path,
)
from app.services.gap_service import (
    VALID_STATUSES,
    _apply_gemini_assessment,
    _coverage_ledger,
    _draft_policy_clause,
    _fallback_assessment,
    _gap_type,
    _gemini_assessments,
    _implementation_evidence,
    _validated_gemini_assessments,
    _is_structural_parent,
    _jurisdiction_mismatch,
    _needs_llm_adjudication,
    _priority,
    _recommendation_owner,
    _target_timeframe,
    build_policy_evidence_index,
    cached_policy_chunks,
    chunk_policy_text,
    coverage_status,
    load_register,
    rank_policy_matches,
    recommendation_for,
    review_policy_gaps,
)
from app.services.obligation_service import (
    assess_obligation_accuracy,
    extract_obligations_from_pdf,
    generate_obligation,
    is_actionable,
    sanitize_breakdown_sources,
    sanitize_source_wording,
)


def make_pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 545, 790), text, fontsize=10)
    document.save(path)
    document.close()


class BreakdownTests(unittest.TestCase):
    def test_dollar_sign_ocr_marker_is_recovered_as_section_nine(self) -> None:
        raw = """--- Page 9 | method=ocr ---
        9. COMPLIANCE
        $.1 Any outsourcing on or after the effective date must comply.
        9.2 Earlier outsourcing must comply when renewed.
        """
        rows = breakdown_regulatory_text(raw)
        self.assertEqual([row["Section"] for row in rows], ["9", "9.1", "9.2"])

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
        with tempfile.TemporaryDirectory() as folder:
            reference = Path(folder)
            pdf = reference / "Directive 159.A.i.pdf"
            make_pdf(pdf, ["Directive 159.A.i\n1. The insurer must maintain controls."])
            self.service.settings = self.service.settings.model_copy(
                update={"reference_directives_root": reference}
            )
            exact = {"title": "Directive 159.A.i", "filename": pdf.name, "document_no": "159.A.i", "description": ""}
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

    def test_metadata_never_starts_live_crawl(self) -> None:
        with patch(
            "app.services.crawler_service.requests.Session.request",
            side_effect=AssertionError("metadata started a network request"),
        ):
            metadata = self.service.metadata()
        self.assertEqual(metadata["sections"][1:], [
            "Insurer / Micro Insurer",
            "Joint FSCA / PA Directives",
            "Retirement Fund",
        ])
        self.assertEqual(metadata["source_state"], "bundled")
        self.assertEqual(metadata["cache_status"]["files_bundled"], 50)
        self.assertFalse(metadata["network_access"])

    def test_authoritative_guid_is_queried_under_regulatory_frameworks(self) -> None:
        endpoints = self.service._build_list_item_endpoints()
        endpoint, params, label = endpoints[0]
        self.assertIn("/Regulatory%20Frameworks/_api/web/lists", endpoint)
        self.assertIn(DIRECTIVES_LIST_GUID, endpoint)
        self.assertEqual(label, "FSCA Directives web-part list")
        self.assertEqual(params["$top"], "55")
        self.assertNotIn("$expand", params)
        self.assertEqual(len(endpoints), 2)

    def test_new_fsca_page_is_parsed_in_one_bounded_request(self) -> None:
        categories = [
            ("subsubCollapseDRC", "Insurer / Micro Insurer", 40),
            ("subsubCollapseDRC1", "Joint FSCA / PA Directives", 2),
            ("subsubCollapseDRC2", "Retirement Fund", 13),
        ]
        panels = []
        for panel_id, _, count in categories:
            rows = "".join(
                (
                    f"""<tr onclick="window.open('/_api/cr3ad_directives({panel_id}-{index})/cr3ad_document/$value', '_blank')">"""
                    f"<td>Directive {index}.A.i ({panel_id})</td></tr>"
                )
                for index in range(count)
            )
            panels.append(f'<div id="{panel_id}"><table>{rows}</table></div>')
        html = f'<div id="collapseEight">{"".join(panels)}</div>'.encode()

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html", "content-length": str(len(html))}

            def raise_for_status(self):
                return None

            def close(self):
                return None

            def iter_content(self, chunk_size=65536):
                yield html

        self.service._begin_request_budget(2)
        with patch.object(self.service, "_polite_request", return_value=FakeResponse()) as request:
            logs = []
            records, counts = self.service._crawl_public_html(logs)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(records), 55)
        self.assertEqual(counts, {
            "Insurer / Micro Insurer": 40,
            "Joint FSCA / PA Directives": 2,
            "Retirement Fund": 13,
        })
        self.assertTrue(all(record.source_link.startswith("https://www.fsca.co.za/") for record in records))

    def test_refresh_cooldown_uses_cache_without_network(self) -> None:
        self.service.last_records = [
            self.service._make_record(
                title="Directive 159.A.i (LTST)",
                filename="Directive_159.A.i_LTST.pdf",
                source_link="https://www.fsca.co.za/_api/cr3ad_directives(x)/cr3ad_document/$value",
                section="Insurer / Micro Insurer",
                category="Insurer / Micro Insurer",
            ).to_dict()
        ]
        self.service.last_crawl_time = __import__("time").time()
        with patch.object(self.service, "_crawl_public_html", side_effect=AssertionError("network started")):
            result = self.service.search("All", "All", force_refresh=True)
        self.assertTrue(result["refresh_suppressed"])
        self.assertTrue(result["from_cache"])

    def test_cached_topic_switch_never_starts_network_and_returns_all_rows(self) -> None:
        with patch(
            "app.services.crawler_service.requests.Session.request",
            side_effect=AssertionError("topic switch started network"),
        ):
            result = self.service.search(
                "Retirement Fund",
                "All",
                cached_only=True,
            )
        self.assertEqual(len(result["records"]), 8)
        self.assertTrue(result["complete"])
        self.assertEqual(result["selected_category_status"]["indexed"], 8)
        self.assertEqual(result["selected_category_status"]["files_bundled"], 8)
        self.assertEqual(result["network_requests"], 0)

    def test_combined_total_cannot_hide_incomplete_category(self) -> None:
        records = []
        wrong_counts = {
            "Insurer / Micro Insurer": 41,
            "Joint FSCA / PA Directives": 1,
            "Retirement Fund": 8,
        }
        for category, count in wrong_counts.items():
            for index in range(count):
                records.append(
                    self.service._make_record(
                        title=f"{category} Directive {index}",
                        filename=f"{category}_{index}.pdf",
                        section=category,
                        category=category,
                        document_no=f"{2000 + len(records)}.A.i",
                    ).to_dict()
                )
        self.assertEqual(len(records), 50)
        status = self.service._cache_status(records)
        self.assertFalse(status["complete"])
        self.assertFalse(status["category_status"]["Insurer / Micro Insurer"]["complete"])
        self.assertFalse(status["category_status"]["Joint FSCA / PA Directives"]["complete"])

    def test_download_batch_is_hard_limited(self) -> None:
        self.service.last_records = [{"id": str(index)} for index in range(MAX_DOWNLOAD_BATCH + 1)]
        with self.assertRaisesRegex(ValueError, "Select at most"):
            self.service.download_selected([str(index) for index in range(MAX_DOWNLOAD_BATCH + 1)])

    def test_network_host_allowlist_rejects_non_fsca_url_before_request(self) -> None:
        for url in [
            "https://example.com/directives",
            "https://www.fsca.co.za/Supervisory-Information/",
        ]:
            with self.assertRaisesRegex(RuntimeError, "network access is disabled"):
                self.service._polite_request("GET", url)

    def test_persistent_manifest_restores_50_rows_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.service.settings = self.service.settings.model_copy(
                update={"storage_root": root, "reference_directives_root": root / "reference"}
            )
            records = []
            directive_index = 1
            for category, count in EXPECTED_CATEGORY_COUNTS.items():
                for _ in range(count):
                    records.append(
                        self.service._make_record(
                            title=f"Directive {directive_index}.A.i",
                            filename=f"Directive {directive_index}.A.i.pdf",
                            source_link=f"https://example.test/Directive-{directive_index}.A.i.pdf",
                            section=category,
                            category=category,
                            year="2026",
                            document_no=f"{directive_index}.A.i",
                        ).to_dict()
                    )
                    directive_index += 1
            self.service._save_persistent_cache(
                records,
                {"Insurer / Micro Insurer": 40, "Joint FSCA / PA Directives": 2, "Retirement Fund": 8},
            )

            restarted = CrawlerService()
            restarted.settings = restarted.settings.model_copy(
                update={"storage_root": root, "reference_directives_root": root / "reference"}
            )
            with patch.object(restarted, "_crawl_sharepoint_list_items", side_effect=AssertionError("network crawl started")):
                result = restarted.search(section="All", year="All", force_refresh=False)
            self.assertEqual(len(result["records"]), 50)
            self.assertEqual(result["cache_status"]["rows_cached"], 50)
            self.assertEqual(result["cache_status"]["crawler_version"], CRAWLER_VERSION)
            self.assertTrue(result["cache_status"]["complete"])

    def test_invalid_pdf_is_not_exposed_as_reference_or_library_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference = root / "reference"
            downloaded = root / "downloaded"
            reference.mkdir()
            downloaded.mkdir()
            (reference / "Directive 159.A.i.pdf").write_bytes(b"x")
            (downloaded / "Directive 160.A.i.pdf").write_bytes(b"not a pdf")
            self.service.settings = self.service.settings.model_copy(
                update={"reference_directives_root": reference, "storage_root": root}
            )
            logs = []
            self.assertEqual(self.service._reference_directives(logs), [])
            names = {item["name"] for item in self.service.library()}
            self.assertNotIn("Directive 159.A.i.pdf", names)
            self.assertNotIn("Directive 160.A.i.pdf", names)
            self.assertEqual(len(names), 50)
            self.assertFalse(_valid_pdf_path(reference / "Directive 159.A.i.pdf"))

    def test_downloaded_pdf_flows_into_library(self) -> None:
        record = next(
            item
            for item in self.service.last_records
            if item["filename"] == "Directive 159.A.i (LTST).pdf"
        )
        result = self.service.download_selected([record["id"]])
        self.assertEqual(len(result["downloaded"]), 1)
        self.assertEqual(result["network_requests"], 0)
        self.assertTrue(_valid_pdf_path(Path(result["downloaded"][0]["path"])))
        self.assertIn(record["filename"], {item["name"] for item in self.service.library()})

    def test_selected_directives_export_to_zip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "generated_outputs").mkdir()
            self.service.settings = self.service.settings.model_copy(
                update={"storage_root": root}
            )
            records = [
                item
                for item in self.service.last_records
                if item["category"] == "Joint FSCA / PA Directives"
            ]
            archive, result = self.service.export_selected([record["id"] for record in records])
            self.assertEqual(len(result["downloaded"]), 2)
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(
                    sorted(bundle.namelist()),
                    sorted(record["filename"] for record in records),
                )


class WorkflowTests(unittest.TestCase):
    def test_regulatory_strategy_pdf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "FSCA Regulatory Strategy 2025-2028.pdf"
            make_pdf(pdf, [
                "REGULATORY STRATEGY 2025-2028\n"
                "1. PURPOSE\nThis strategic plan describes the authority's multi-year priorities and outcomes.\n"
                "2. MARKET DEVELOPMENT\nThe report explains supervision themes and an organizational roadmap.\n"
                "3. PERFORMANCE\nThe annual strategy describes intended projects and public-sector goals."
            ])
            with self.assertRaisesRegex(ValueError, "strategy/report-style"):
                extract_obligations_from_pdf(pdf)

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
        self.assertEqual(results["row-1"]["status"], "Completely Missing")

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
        evidence = (
            "The policy requires annual assessment of each service provider's "
            "ability to comply with applicable laws."
        )
        task = {
            "id": "row-1", "section": "7.11.2",
            "directive_text": "An insurer must regularly assess the service provider's ability to comply with applicable laws.",
            "obligation": "An insurer must regularly assess the service provider's ability to comply with applicable laws.",
            "candidates": [{"candidate_id": "candidate-1", "page": "8", "text": evidence, "score": 0.8, "keyword_score": 0.7, "hits": ["assess"]}],
        }
        result = _apply_gemini_assessment(task, {
            "coverage_status": "fully covered", "candidate_id": "candidate_1",
            "evidence_quote": "requires annual assessment of each service provider's ability to comply with applicable laws",
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

    @patch.dict(os.environ, {"EXPORT_INTERNAL_QUALITY_METRICS": "false"})
    def test_native_pdf_extraction_and_output_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "Directive 240.A.i.pdf"
            make_pdf(pdf, [
                "DIRECTIVE 240.A.i\n1. PURPOSE\nThis Directive applies to insurers.\n"
                "2. REQUIREMENTS\n2.1 The insurer must maintain a board-approved policy.\n"
                "2.2 The insurer shall notify the FSCA within 10 business days."
            ])
            result = extract_obligations_from_pdf(pdf)
            self.assertEqual(result["extraction_pipeline"]["pipeline_version"], "2026-08-06.2")
            sections = [row["Section"] for row in result["tabs"]["text_breakdown"]]
            self.assertIn("2.1", sections)
            self.assertGreater(result["kpis"][1]["value"], 0)
            self.assertGreaterEqual(result["accuracy"]["overall_percentage"], 90)
            self.assertTrue(result["tabs"]["accuracy_review"])
            actionable = [row for row in result["tabs"]["accuracy_review"] if row["Actionable"] == "Yes"]
            self.assertTrue(all(row["Answer Completeness %"] == 100 for row in actionable))
            self.assertTrue(all(row["Material Elements %"] >= 85 for row in actionable))
            self.assertNotIn("Actionable Obligation Accuracy", {item["label"] for item in result["kpis"]})
            self.assertIn("Actionable Review Rows", {item["label"] for item in result["kpis"]})
            self.assertIn("High-priority Obligations", {item["label"] for item in result["kpis"]})
            self.assertIn("actionable_manual_review_rows", result["accuracy"])
            self.assertIn("all_manual_review_rows", result["accuracy"])
            output = Path(__file__).resolve().parents[1] / "storage" / "generated_outputs" / result["output_files"]["excel"]
            with pd.ExcelFile(output) as workbook:
                self.assertEqual(workbook.sheet_names, ["Obligations", "Text Breakdown", "Statistics", "Process Log"])
            exported_obligations = pd.read_excel(output, sheet_name="Obligations")
            self.assertNotIn("Document Accuracy %", exported_obligations.columns)
            self.assertNotIn("Accuracy Rating", exported_obligations.columns)
            self.assertNotIn("Accuracy Notes", exported_obligations.columns)
            exported_csv = pd.read_csv(output.with_suffix(".csv"))
            self.assertNotIn("Document Accuracy %", exported_csv.columns)
            self.assertEqual(result["output_profile"], "client-safe")

            with patch.dict(os.environ, {"EXPORT_INTERNAL_QUALITY_METRICS": "true"}):
                internal_result = extract_obligations_from_pdf(pdf)
            internal_output = Path(__file__).resolve().parents[1] / "storage" / "generated_outputs" / internal_result["output_files"]["excel"]
            with pd.ExcelFile(internal_output) as workbook:
                self.assertIn("Accuracy Review", workbook.sheet_names)
            internal_obligations = pd.read_excel(internal_output, sheet_name="Obligations")
            self.assertIn("Document Accuracy %", internal_obligations.columns)
            self.assertEqual(internal_result["output_profile"], "internal-quality")

    def test_obligation_preserves_conditions_deadlines_and_prohibitions(self) -> None:
        source = (
            "If an outsourcing arrangement affects a material function, the insurer may not proceed "
            "unless the board has approved it and must notify the FSCA within 10 business days."
        )
        obligation = generate_obligation("4.2", source)
        self.assertIn("If an outsourcing arrangement", obligation)
        self.assertIn("must not proceed", obligation)
        self.assertIn("unless the board has approved", obligation)
        self.assertIn("within 10 business days", obligation)
        review = assess_obligation_accuracy(
            section="4.2",
            source_text=source,
            obligation=obligation,
            actionable=True,
            source_page="1",
            pages=[{"page": 1, "text": source, "method": "native", "score": 500}],
        )
        self.assertGreaterEqual(review["Document Accuracy %"], 95)
        self.assertEqual(review["Material Elements %"], 100)
        self.assertEqual(review["Answer Completeness %"], 100)

    def test_directive_159_quality_control_regression_sections_are_complete(self) -> None:
        cases = [
            (
                "1",
                (
                    "PURPOSE The purpose of this Directive is to, under sections 4(4) of the "
                    "Long-term Insurance Act No. 52 of 1998 and the Short-term Insurance Act "
                    "No. 53 of 1998, direct long-term and short-term insurers to comply with "
                    "the requirements set out in this Directive when outsourcing an aspect "
                    "of their insurance business to another person."
                ),
                "",
                ["Insurers must comply", "when outsourcing"],
            ),
            (
                "7.5.7",
                (
                    "develop appropriate management and monitoring procedures for the "
                    "proposed outsourcing consistent with that set cut in paragraphs 7.9 to 7.11:"
                ),
                "An insurer must prior to outsourcing any control, management or material function —",
                ["must prior to outsourcing", "management and monitoring procedures", "paragraphs 7.9 to 7.11"],
            ),
            (
                "8.1.1",
                "the proposed outsourcing (subject to any requirements under the Acts);",
                (
                    "An insurer must timeously, but no later than one month, prior to the "
                    "effective date of an outsourcing contract relating to a control, "
                    "management or material function, notify the Registrar of —"
                ),
                ["must timeously", "no later than one month", "notify the Registrar", "proposed outsourcing"],
            ),
            (
                "8.1.2",
                "the details of the other person to whom the insurer will outsource that function; and",
                (
                    "An insurer must timeously, but no later than one month, prior to the "
                    "effective date of an outsourcing contract relating to a control, "
                    "management or material function, notify the Registrar of —"
                ),
                ["notify the Registrar", "details of the other person"],
            ),
            (
                "8.1.3",
                (
                    "the key risks associated with the outsourcing and the risk mitigation "
                    "strategies that will be put in place to address these risks."
                ),
                (
                    "An insurer must timeously, but no later than one month, prior to the "
                    "effective date of an outsourcing contract relating to a control, "
                    "management or material function, notify the Registrar of —"
                ),
                ["notify the Registrar", "key risks", "risk mitigation strategies"],
            ),
            (
                "8.2",
                (
                    "An insurer must immediately notify the Registrar of any material "
                    "developments (such as pending termination and material non-performance) "
                    "with respect to the outsourcing referred to in paragraph 8.1 during the "
                    "duration of the outsourcing contract."
                ),
                "",
                ["must immediately notify the Registrar", "material developments", "paragraph 8.1"],
            ),
            (
                "10",
                (
                    "AVAILABILITY AND INFORMATION SHARING This Directive is available on the "
                    "website of the Financial Services Board. Insurers must bring this Directive "
                    "to the attention of their appointed auditors and statutory actuary (where "
                    "one has been appointed). REGIST S OF LONG-TERM AND SHORT-TERM INSURANCE "
                    "Directive 19,4 garbled footer text"
                ),
                "",
                ["Insurers must bring this Directive", "appointed auditors", "statutory actuary"],
            ),
        ]
        for section, source, parent, expected_fragments in cases:
            with self.subTest(section=section):
                obligation = generate_obligation(section, source, parent)
                for fragment in expected_fragments:
                    self.assertIn(fragment, obligation)
                self.assertNotIn(":.", obligation)
                if section == "10":
                    self.assertNotIn("REGIST S", obligation)
                    self.assertNotIn("garbled footer", obligation)
                review = assess_obligation_accuracy(
                    section=section,
                    source_text=source,
                    obligation=obligation,
                    actionable=True,
                    source_page="1",
                    pages=[{"page": 1, "text": source, "method": "native", "score": 500}],
                    parent_context=parent,
                )
                self.assertEqual(review["Answer Completeness %"], 100)
                self.assertEqual(review["Missing Material Elements"], 0)
                self.assertEqual(review["Material Elements %"], 100)
                self.assertGreaterEqual(review["Document Accuracy %"], 85)

    def test_directive_159_actual_ocr_rows_ignore_nonoperative_context(self) -> None:
        cases = [
            (
                "8.2",
                (
                    "An insurer must immediately notify the Registrar of any material "
                    "developments (such as pending termination, material non-performance "
                    "and the like) with respect to the outsourcing referred to in paragraph "
                    "8.1, during the duration of the outsourcing contract."
                ),
                "REPORTING Notification of outsourcing of control, management or material functions",
                [
                    "must immediately notify the Registrar",
                    "material developments",
                    "paragraph 8.1",
                ],
            ),
            (
                "10",
                (
                    "AVAILABILITY AND INFORMATION SHARING This Directive is available on "
                    "the website (www.fsb.co.za} of the Financial Services Board. Insurers "
                    "must bring this Directive to the attention of their appointed auditors "
                    "and statutory actuary (where one has been appointed). REGIST S OF "
                    "LONG-TERM AND SHORT-TERM INSURANCE Directive 19,4, (LT & ST) = "
                    "Coinplanee with sections S(O (DK) rend with sections 12(1X6) of the "
                    "Longo Wninanes Arad Shor om Weieanee AGL feepeoivel"
                ),
                "",
                [
                    "Insurers must bring this Directive",
                    "appointed auditors",
                    "statutory actuary",
                    "where one has been appointed",
                ],
            ),
        ]
        for section, source, parent, expected_fragments in cases:
            with self.subTest(section=section):
                obligation = generate_obligation(section, source, parent)
                for fragment in expected_fragments:
                    self.assertIn(fragment, obligation)
                review = assess_obligation_accuracy(
                    section=section,
                    source_text=source,
                    obligation=obligation,
                    actionable=True,
                    source_page="1",
                    pages=[{"page": 1, "text": source, "method": "ocr", "score": 500}],
                    parent_context=parent,
                )
                self.assertEqual(review["Answer Completeness %"], 100)
                self.assertEqual(review["Missing Material Elements"], 0)
                self.assertEqual(review["Material Elements %"], 100)
                if section == "10":
                    self.assertLessEqual(review["Document Accuracy %"], 84)
                    self.assertEqual(review["Manual Review Required"], "Yes")
                    self.assertIn("verify the cleaned obligation against the original PDF", review["Accuracy Notes"])
                else:
                    self.assertGreaterEqual(review["Document Accuracy %"], 90)

    def test_clause_aware_validation_still_rejects_material_omissions(self) -> None:
        source = (
            "If an outsourcing arrangement affects a material function, the insurer "
            "may not proceed unless the board has approved it and must notify the FSCA "
            "within 10 business days."
        )
        incomplete = "The insurer must not proceed."
        review = assess_obligation_accuracy(
            section="4.2",
            source_text=source,
            obligation=incomplete,
            actionable=True,
            source_page="1",
            pages=[{"page": 1, "text": source, "method": "native", "score": 500}],
        )
        self.assertGreater(review["Missing Material Elements"], 0)
        self.assertLess(review["Material Elements %"], 85)
        self.assertIn("Missing material element", review["Accuracy Notes"])

    def test_directive_159_remaining_ocr_intrusions_are_removed(self) -> None:
        cases = [
            (
                "5.2.2",
                (
                    "the ability of the insurer to maintain appropriate internal controls and "
                    "meet pecyeal el NE ol Sortert 5 INSU a Sarees Paige us Sheseee se Long-te "
                    "Eee Ae are ee ene Ech spaniel: Ouleoure! ai See regulatory requirements; and"
                ),
                (
                    "An insurer, in respect of every outsourcing to another person, must determine "
                    "if the outsourcing constitutes the outsourcing of a control, management or "
                    "material function. In making that determination, an insurer must consider —"
                ),
                ["meet regulatory requirements"],
                ["pecyeal", "Ouleoure", "Sheseee"],
            ),
            (
                "7.7.2",
                (
                    "specify the type and frequency of the function or activity to be performed: "
                    "- * The binder functions referred to in section 49A(1)(a) to (e). "
                    "Disclve Compliarice with scotons SGD)"
                ),
                "A written contract must, at least, —",
                ["specify the type and frequency of the function or activity to be performed"],
                ["binder functions", "Disclve", "scotons"],
            ),
            (
                "7.7.15",
                (
                    "specify that the other person will take the necessary steps to allow the "
                    "Registrar access to its business and information in respect of the outsourced "
                    "function or activity; Piste SOA 8 BT COUGH as wih aeciore"
                ),
                "A written contract must, at least, —",
                ["allow the Registrar access", "outsourced function or activity"],
                ["Piste", "COUGH", "aeciore"],
            ),
        ]
        for section, source, parent, expected, rejected in cases:
            with self.subTest(section=section):
                obligation = generate_obligation(section, source, parent)
                for fragment in expected:
                    self.assertIn(fragment, obligation)
                for fragment in rejected:
                    self.assertNotIn(fragment, obligation)
                self.assertNotIn("{to", obligation)

                clean_source = sanitize_source_wording(source, parent)
                for fragment in expected:
                    self.assertIn(fragment, clean_source)
                for fragment in rejected:
                    self.assertNotIn(fragment, clean_source)
                self.assertNotIn("{to", clean_source)

    def test_exported_breakdown_removes_cross_page_headers_and_footnotes(self) -> None:
        rows = [
            {
                "Sequence": 1,
                "Section": "4.1",
                "Language from Directive": (
                    "Sections &3\\\\b){i) of the Acts provide that an application may not be "
                    "granted by the Registrar. 2 This includes unrelated explanatory text."
                ),
                "Page": "2",
            },
            {
                "Sequence": 2,
                "Section": "6.4.4",
                "Language from Directive": (
                    "not be linked to the monetary value of insurance claims repudiated, paid, "
                    "not paid or partially paid. ons ie aco ea poteenal uae Oy outa"
                ),
                "Page": "4",
            },
            {
                "Sequence": 3,
                "Section": "8.1",
                "Language from Directive": (
                    "An insurer must notify the Registrar of — Bireaive (98 A (CT 4 81) "
                    "Complanes wih seqmjons O2)(0\\\\()"
                ),
                "Page": "8",
            },
            {
                "Sequence": 4,
                "Section": "8.1.1",
                "Language from Directive": "the proposed outsourcing;",
                "Page": "9",
            },
            {
                "Sequence": 5,
                "Section": "10",
                "Language from Directive": (
                    "Insurers must bring this Directive to the attention of their appointed "
                    "auditors and statutory actuary. REGIST S OF LONG-TERM AND SHORT-TERM "
                    "INSURANCE Coinplanee with sections"
                ),
                "Page": "9",
            },
        ]
        cleaned = sanitize_breakdown_sources(rows)
        by_section = {row["Section"]: row["Language from Directive"] for row in cleaned}
        self.assertEqual(
            by_section["4.1"],
            "Sections 9(3)(b)(i) of the Acts provide that an application may not be granted by the Registrar.",
        )
        self.assertTrue(by_section["6.4.4"].endswith("partially paid."))
        self.assertEqual(by_section["8.1"], "An insurer must notify the Registrar of —")
        self.assertNotIn("Bireaive", by_section["8.1"])
        self.assertNotIn("REGIST", by_section["10"])
        self.assertNotIn("Coinplanee", by_section["10"])

    def test_source_sanitizer_preserves_multi_sentence_structural_parent(self) -> None:
        source = (
            "An insurer, in respect of every outsourcing to another person, must determine if the "
            "outsourcing constitutes the outsourcing of a control, management or material function "
            "for purposes of this Directive. In making the determination as to whether a function "
            "is a material function, an insurer must, amongst others, consider —"
        )
        cleaned = sanitize_source_wording(source)
        self.assertIn("must determine", cleaned)
        self.assertIn("must, amongst others, consider —", cleaned)

    def test_authority_powers_are_not_invented_as_entity_obligations(self) -> None:
        registrar_registration_power = (
            "Sections 9(3)(b)(i) of the Acts provide that an application for registration "
            "as an insurer may not be granted by the Registrar if the applicant does not "
            "have adequate organisation or management."
        )
        registrar_enforcement_power = (
            "Sections 12(1)(c) of the Acts provide that the Registrar may prohibit an insurer "
            "from carrying on insurance business if it cannot satisfy the Registrar."
        )
        for section, source in [
            ("4.1", registrar_registration_power),
            ("4.2", registrar_enforcement_power),
        ]:
            with self.subTest(section=section):
                self.assertFalse(is_actionable(source))
                self.assertIn("no standalone implementation obligation", generate_obligation(section, source))

    def test_guidance_and_scope_exclusion_are_not_invented_as_mandatory_duties(self) -> None:
        guidance = (
            "GUIDANCE ON RISKS An insurer should assess, monitor and manage contractual "
            "and operational risks in respect of outsourcing."
        )
        scope_exclusion = (
            "This Directive does not apply to functions or activities performed by an "
            "insurer on behalf of another person."
        )
        for section, source in [("ANNEXURE A", guidance), ("3.5", scope_exclusion)]:
            with self.subTest(section=section):
                self.assertFalse(is_actionable(source))
                self.assertIn("no standalone implementation obligation", generate_obligation(section, source))

    def test_scope_obligation_drops_appended_reference_footnote(self) -> None:
        source = (
            "This Directive applies to all insurers (including, subject to paragraph 3.6, "
            "reinsurers). Insurance core principles (October 2011), specifically ICP 8.7, "
            "of the International Association of Insurance Supervision."
        )
        obligation = generate_obligation("3.1", source)
        self.assertIn("This Directive applies to all insurers", obligation)
        self.assertNotIn("Insurance core principles", obligation)

    def test_accuracy_flags_ocr_contamination_instead_of_claiming_full_completeness(self) -> None:
        source = (
            "A written contract must specify that the other person will allow the Registrar "
            "access to its business and information."
        )
        contaminated = (
            "A written contract must specify that the other person will allow the Registrar "
            "access Piste SOA COUGH Oumoureng to its business and information."
        )
        review = assess_obligation_accuracy(
            section="7.7.15",
            source_text=source,
            obligation=contaminated,
            actionable=True,
            source_page="1",
            pages=[{"page": 1, "text": source, "method": "ocr", "score": 500}],
        )
        self.assertLess(review["Text Cleanliness %"], 85)
        self.assertEqual(review["Manual Review Required"], "Yes")
        self.assertIn("requires manual review", review["Accuracy Notes"])

    def test_cleaned_answer_does_not_hide_dirty_ocr_source_confidence(self) -> None:
        source = (
            "specify that the other person will take the necessary steps to allow the "
            "Registrar access {to its business and information in respect of the outsourced "
            "function or activity; Piste SOA COUGH Oumoureng"
        )
        parent = "A written contract must, at least, —"
        obligation = generate_obligation("7.7.15", source, parent)
        review = assess_obligation_accuracy(
            section="7.7.15",
            source_text=source,
            obligation=obligation,
            actionable=True,
            source_page="1",
            pages=[{"page": 1, "text": source, "method": "ocr", "score": 500}],
            parent_context=parent,
        )
        self.assertIn("allow the Registrar access to its business", obligation)
        self.assertNotIn("{to", obligation)
        self.assertLess(review["Source Cleanliness %"], 85)
        self.assertLessEqual(review["Document Accuracy %"], 84)
        self.assertEqual(review["Accuracy Rating"], "Medium")
        self.assertEqual(review["Manual Review Required"], "Yes")
        self.assertIn("verify the cleaned obligation against the original PDF", review["Accuracy Notes"])

    def test_ocr_rows_are_never_reported_as_character_perfect(self) -> None:
        source = "The insurer must notify the Registrar within 10 business days."
        obligation = generate_obligation("2.1", source)
        review = assess_obligation_accuracy(
            section="2.1",
            source_text=source,
            obligation=obligation,
            actionable=True,
            source_page="1",
            pages=[{"page": 1, "text": source, "method": "ocr", "score": 900}],
        )
        self.assertEqual(review["Document Accuracy %"], 95)
        self.assertIn("capped at 95%", review["Accuracy Notes"])

    def test_page_traceability_uses_the_same_ocr_repairs_as_section_breakdown(self) -> None:
        cases = [
            (
                "7.7.16",
                "specify the circumstances under which the insurer may terminate the contract;",
                "specify the circumstances under which the insurars may terminate the contract;",
            ),
            (
                "7.7.17",
                "include indemnity and liability provisions;",
                "include indemnity and iiability provisions;",
            ),
            (
                "7.7.18",
                (
                    "set out any warranties or guarantees to be furnished and insurance to be "
                    "secured by the other person in respect of its ability to fulfill its "
                    "contractual obligations;"
                ),
                (
                    "set out any warranties or guarantees to be furnished and insurance to be "
                    "seoured by the other person in respect of its ability to fulfill its "
                    "contractual obligations;"
                ),
            ),
            (
                "7.7.19",
                "provide for a dispute resolution process; and",
                "provide for a dispute resolution process; and",
            ),
            (
                "7.7.20",
                (
                    "provide for a reasonable termination period, irrespective of the "
                    "circumstances under which the agreement is terminated that will allow "
                    "the insurer's contingency plans to be implemented."
                ),
                (
                    "provide for a reasonable termination period, irrespective of the "
                    "circumstances under which the agreement is terminated that will allow "
                    "the insurer's contingency plans to be implemented."
                ),
            ),
        ]
        parent = "A written contract must, at least, —"
        for section, source, raw_ocr_page in cases:
            with self.subTest(section=section):
                obligation = generate_obligation(section, source, parent)
                review = assess_obligation_accuracy(
                    section=section,
                    source_text=source,
                    obligation=obligation,
                    actionable=True,
                    source_page="7",
                    pages=[{
                        "page": 7,
                        "text": raw_ocr_page,
                        "method": "ocr",
                        "score": 1103,
                    }],
                    parent_context=parent,
                )
                self.assertGreaterEqual(review["Source Fidelity %"], 98)
                self.assertEqual(review["Document Accuracy %"], 95)
                self.assertEqual(review["Accuracy Rating"], "High")
                self.assertEqual(review["Manual Review Required"], "No")

    def test_clean_child_traceability_survives_rotated_page_reading_order(self) -> None:
        cases = [
            (
                "7.7.16",
                "specify the circumstances under which the insurer may terminate the contract;",
            ),
            (
                "7.7.17",
                "include indemnity and liability provisions;",
            ),
            (
                "7.7.18",
                (
                    "set out any warranties or guarantees to be furnished and insurance to be "
                    "secured by the other person in respect of its ability to fulfill its "
                    "contractual obligations;"
                ),
            ),
            (
                "7.7.19",
                "provide for a dispute resolution process; and",
            ),
            (
                "7.7.20",
                (
                    "provide for a reasonable termination period, irrespective of the "
                    "circumstances under which the agreement is terminated (including the "
                    "lapsing or non-renewal of the agreement) that will allow the insurer's "
                    "contingency plans to be implemented."
                ),
            ),
        ]
        parent = "A written contract must, at least, —"
        for section, source in cases:
            with self.subTest(section=section):
                # OCR on the rotated source page can preserve every word while
                # returning them in column order rather than clause order.
                rotated_page_text = " ".join(reversed(source.replace(";", "").split()))
                obligation = generate_obligation(section, source, parent)
                review = assess_obligation_accuracy(
                    section=section,
                    source_text=source,
                    obligation=obligation,
                    actionable=True,
                    source_page="7",
                    pages=[{
                        "page": 7,
                        "text": rotated_page_text,
                        "method": "ocr",
                        "score": 1103,
                    }],
                    parent_context=parent,
                )
                self.assertEqual(review["Source Cleanliness %"], 100)
                self.assertEqual(review["Source Fidelity %"], 100)
                self.assertEqual(review["Document Accuracy %"], 95)
                self.assertEqual(review["Accuracy Rating"], "High")
                self.assertEqual(review["Manual Review Required"], "No")

    def test_section_headings_are_not_appended_to_operative_answers(self) -> None:
        cases = [
            (
                "6.1",
                (
                    "The board of directors and managing executives of an insurer remain "
                    "responsible for the insurance business of the insurer, regardless of "
                    "any outsourcing. Principles with which any outsourcing must comply"
                ),
                "",
                "Principles with which",
            ),
            (
                "7.5.9",
                (
                    "secure the necessary approvals for the outsourcing in accordance with "
                    "the outsourcing policy. Written contracts"
                ),
                "An insurer must prior to outsourcing any control, management or material function —",
                "Written contracts",
            ),
            (
                "7.8",
                (
                    "Where an outsourcing contract allows another person to sub-outsource any "
                    "part of the function, that sub-outsourcing must comply with paragraphs 7.6 "
                    "and 7.7. Management and regular review"
                ),
                "",
                "Management and regular review",
            ),
        ]
        for section, source, parent, heading in cases:
            with self.subTest(section=section):
                obligation = generate_obligation(section, source, parent)
                self.assertNotIn(heading, obligation)
                self.assertTrue(obligation.endswith("."))

    @patch.dict(os.environ, {"EXPORT_INTERNAL_QUALITY_METRICS": "false"})
    def test_policy_review_uses_required_statuses_and_reconciles_kpis(self) -> None:
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
            self.assertEqual(result["pipeline"]["pipeline_version"], "2026-08-18.2-neutral-recommendations")
            self.assertTrue(result["pipeline"]["run_id"])
            self.assertEqual(result["logs"][0]["stage"], "Pipeline")
            self.assertTrue(any(log["stage"] == "Quality Control" for log in result["logs"]))
            self.assertIn(result["pipeline"]["run_id"], result["output_files"]["excel"])
            self.assertTrue({row["Coverage Status"] for row in rows}.issubset(VALID_STATUSES))
            informational = next(row for row in rows if row["Section"] == "1.3")
            self.assertEqual(informational["Coverage Status"], "Not Applicable / Informational")
            self.assertEqual(sum(item["value"] for item in result["kpis"][1:]), result["kpis"][0]["value"])
            for row in rows:
                if row["Coverage Status"] == "Completely Missing":
                    self.assertEqual(row["Corresponding Policy Text"], "")
                if row["Section"] == "1.1" and row["Coverage Status"] == "Completely Covered":
                    self.assertEqual(row["Policy Gap and Recommendations"], "")
            output = Path(__file__).resolve().parents[1] / "storage" / "generated_outputs" / result["output_files"]["excel"]
            with pd.ExcelFile(output) as workbook:
                self.assertEqual(workbook.sheet_names, ["Executive Summary", "Gap Assessment", "Statistics", "Process Log"])
            exported_assessment = pd.read_excel(output, sheet_name="Gap Assessment")
            self.assertNotIn("Gap Coverage %", exported_assessment.columns)
            self.assertNotIn("Assessment Confidence %", exported_assessment.columns)
            exported_csv = pd.read_csv(output.with_suffix(".csv"))
            self.assertNotIn("Gap Coverage %", exported_csv.columns)
            self.assertNotIn("Assessment Confidence %", exported_csv.columns)
            self.assertEqual(result["output_profile"], "client-safe")
            summary = pd.read_excel(output, sheet_name="Executive Summary", header=None)
            self.assertEqual(summary.iat[0, 0], "Policy Gap Assessment - register")
            self.assertNotIn("Directive 159", str(summary.iat[0, 0]))
            self.assertIn("Review Rationale", rows[0])
            self.assertIn("Gap Coverage %", rows[0])
            self.assertIn("Assessment Confidence %", rows[0])
            self.assertIn("Missing Elements", rows[0])
            self.assertIn("Draft Policy Clause", rows[0])
            self.assertIn("Recommendation Owner", rows[0])
            self.assertIn("Target Timeframe", rows[0])
            self.assertIn("Implementation Evidence", rows[0])
            self.assertIn("Manual Review Required", rows[0])
            self.assertEqual(result["gap_quality"]["evidence_grounding_percentage"], 100)
            self.assertEqual(result["gap_quality"]["recommendation_completeness_percentage"], 100)

            with patch.dict(os.environ, {"EXPORT_INTERNAL_QUALITY_METRICS": "true"}):
                internal_result = review_policy_gaps(register, policy)
            internal_output = Path(__file__).resolve().parents[1] / "storage" / "generated_outputs" / internal_result["output_files"]["excel"]
            internal_assessment = pd.read_excel(internal_output, sheet_name="Gap Assessment")
            self.assertIn("Gap Coverage %", internal_assessment.columns)
            self.assertIn("Assessment Confidence %", internal_assessment.columns)
            self.assertEqual(internal_result["output_profile"], "internal-quality")

    def test_policy_page_markers_support_native_and_ocr_formats(self) -> None:
        chunks = chunk_policy_text(
            "--- Page 1 | method=native ---\n1. First policy section.\n"
            "--- Page 2 | method=ocr | rotation=90 ---\n2. Second policy section."
        )
        self.assertEqual({chunk["page"] for chunk in chunks}, {"1", "2"})

    def test_foreign_regulator_name_does_not_change_substantive_coverage(self) -> None:
        directive = "The insurer must notify the Registrar before outsourcing this South African insurance function."
        evidence = "The company shall notify the Saudi Arabia Insurance Authority before outsourcing."
        self.assertFalse(_jurisdiction_mismatch(directive, directive, evidence))
        self.assertEqual(coverage_status(0.9, 0.8, evidence, False), "Completely Covered")

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
        self.assertNotIn("South African", recommendation)
        self.assertIn("applicable directive", recommendation)
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
        self.assertEqual(result["status"], "Completely Covered")
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
        self.assertEqual(result["status"], "Completely Missing")

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
        self.assertEqual(result["status"], "Completely Missing")
        fallback = _fallback_assessment(task)
        self.assertEqual(fallback["status"], "Completely Missing")

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
        self.assertEqual(result["status"], "Completely Missing")

    def test_advisory_wording_cannot_be_completely_covered(self) -> None:
        ledger = _coverage_ledger(
            "The insurer must annually assess each service provider's ability to comply with applicable laws.",
            "The insurer must annually assess each service provider's ability to comply with applicable laws.",
            "The policy states that business owners should annually assess each service provider's ability to comply with applicable laws.",
            "7.11.2",
            candidate_score=0.9,
            source_method="native",
        )
        self.assertEqual(ledger["status"], "Partially Covered")
        self.assertIn("mandatory policy requirement", ledger["missing"])

    def test_wrong_deadline_is_reported_as_a_material_gap(self) -> None:
        ledger = _coverage_ledger(
            "The insurer must notify the FSCA within 10 business days.",
            "The insurer must notify the FSCA within 10 business days.",
            "The policy requires the insurer to notify the FSCA within 30 business days.",
            "8.2",
            candidate_score=0.95,
            source_method="native",
        )
        self.assertEqual(ledger["status"], "Partially Covered")
        self.assertIn("specified timing or frequency", ledger["missing"])
        self.assertLess(ledger["coverage_percentage"], 100)

    def test_generic_sla_does_not_cover_type_and_frequency_contract_clause(self) -> None:
        ledger = _coverage_ledger(
            "A written contract must specify the type and frequency of the function or activity to be performed.",
            "A written contract must specify the type and frequency of the function or activity to be performed.",
            "Ensure that a contract with appropriate Service Level Agreements (SLAs) is in place.",
            "7.7.2",
            candidate_score=0.9,
            source_method="native",
        )
        self.assertNotEqual(ledger["status"], "Completely Covered")
        self.assertIn("contract specifies function type", ledger["missing"])
        self.assertIn("contract specifies performance frequency", ledger["missing"])

    def test_generic_due_diligence_does_not_cover_fit_and_proper_test(self) -> None:
        ledger = _coverage_ledger(
            "The insurer must assess whether the provider is fit and proper in terms of competence and integrity.",
            "The insurer must assess whether the provider is fit and proper in terms of competence and integrity.",
            "Due diligence shall be conducted to evaluate the prospective third party prior to engaging.",
            "7.5.4",
            candidate_score=0.9,
            source_method="native",
        )
        self.assertNotEqual(ledger["status"], "Completely Covered")
        self.assertIn("fit-and-proper competence and integrity", ledger["missing"])

    def test_conflict_topic_mention_does_not_cover_avoid_or_mitigate_duty(self) -> None:
        ledger = _coverage_ledger(
            "The insurer must avoid, or where that is not possible mitigate, conflicts of interest between the insurer, policyholders and the service provider.",
            "The insurer must avoid, or where that is not possible mitigate, conflicts of interest between the insurer, policyholders and the service provider.",
            "Due diligence should address potential conflict of interest where the third party is related to the company.",
            "6.3",
            candidate_score=0.9,
            source_method="native",
        )
        self.assertNotEqual(ledger["status"], "Completely Covered")
        self.assertIn("avoid or mitigate conflicts of interest", ledger["missing"])
        self.assertIn("policyholder, insurer and service-provider interests", ledger["missing"])

    def test_due_diligence_insurance_topic_does_not_cover_contractual_warranties(self) -> None:
        evidence = (
            "Due diligence should address insurance coverage and potential conflicts of interest. "
            "A separate policy section states that contracts must be approved."
        )
        task = {
            "id": "row-1",
            "section": "7.7.18",
            "directive_text": "A written contract must set out warranties or guarantees and insurance to be secured by the service provider.",
            "obligation": "A written contract must set out warranties or guarantees and insurance to be secured by the service provider.",
            "candidates": [{
                "candidate_id": "candidate-1",
                "page": "11",
                "text": evidence,
                "score": 0.9,
                "keyword_score": 0.8,
                "hits": ["insurance", "contract"],
                "method": "native",
            }],
        }
        result = _apply_gemini_assessment(task, {
            "coverage_status": "Completely Covered",
            "candidate_id": "candidate-1",
            "evidence_quote": "Due diligence should address insurance coverage and potential conflicts of interest.",
            "rationale": "Insurance is mentioned.",
        })
        self.assertIsNotNone(result)
        self.assertNotEqual(result["status"], "Completely Covered")
        self.assertIn("contractual warranties or guarantees", result["ledger"]["missing"])
        self.assertIn("service-provider insurance requirement", result["ledger"]["missing"])

    def test_clause_specific_controls_block_broad_false_complete_results(self) -> None:
        cases = [
            (
                "6.1",
                "The board of directors and managing executives must remain responsible for the insurance business regardless of outsourcing.",
                "The policy requires outsourcing risks to be reviewed by management.",
                "retained board and executive responsibility",
            ),
            (
                "6.4.1",
                "Remuneration paid for outsourcing must be reasonable and commensurate with the actual function outsourced.",
                "The business case must document the cost of the outsourcing arrangement.",
                "reasonable and commensurate outsourcing remuneration",
            ),
            (
                "6.4.4",
                "Remuneration must not be linked to the monetary value of insurance claims repudiated, paid or partially paid.",
                "The third party may not expose the insurer to material financial risk.",
                "remuneration not linked to insurance-claim outcomes",
            ),
            (
                "7.2.2",
                "The outsourcing policy must set limits on types, overall levels and concentration with the same service provider.",
                "The policy requires general outsourcing risk limits.",
                "outsourcing type, level and concentration limits",
            ),
            (
                "7.6",
                "Written contracts must describe all material aspects, rights, responsibilities and service levels.",
                "All outsourcing records must be retained for ten years.",
                "contract covers material aspects, rights and responsibilities",
            ),
            (
                "7.7.17",
                "A written contract must include indemnity and liability provisions.",
                "The terms and conditions must be defined in a written contract reviewed by Legal.",
                "contractual indemnity and liability provisions",
            ),
        ]
        for section, obligation, evidence, expected_missing in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    obligation,
                    obligation,
                    evidence,
                    section,
                    candidate_score=0.9,
                    source_method="native",
                )
                self.assertNotEqual(ledger["status"], "Completely Covered")
                self.assertIn(expected_missing, ledger["missing"])

    def test_full_demo_false_complete_regressions_require_exact_clause_payload(self) -> None:
        cases = [
            (
                "1",
                "Insurers must comply with all Directive 159 requirements when outsourcing insurance business.",
                "Under FSCA Directive 159, the insurer must assess whether outsourcing concerns a material function.",
                "complete Directive 159 compliance for outsourcing",
            ),
            (
                "3.1",
                "This Directive applies to all insurers, including qualifying reinsurers.",
                "For South African operations under FSCA Directive 159, this policy applies to all aspects of the insurer's insurance business that may be outsourced.",
                "applicability to all insurers including qualifying reinsurers",
            ),
            (
                "3.4.2",
                "Directive 159 applies to subsidiary insurance-business outsourcing inside or outside South Africa.",
                "This South African FSCA policy applies to all outsourced aspects of the insurer's insurance business.",
                "subsidiary insurance-business outsourcing inside or outside South Africa",
            ),
            (
                "3.7",
                "The insurer must comply with Directive 159 in addition to the existing regulatory framework and its specific requirements.",
                "This policy applies under FSCA Directive 159 to South African outsourcing operations.",
                "additional existing regulatory-framework compliance",
            ),
            (
                "5.2.3",
                "The insurer must consider the difficulty and time associated with replacing the provider or performing the function in-house.",
                "The insurer must determine materiality by considering impacts on policyholders, finances, reputation and operations.",
                "replacement difficulty, replacement time and in-house alternative",
            ),
            (
                "6.2.1",
                "The insurer must not outsource a function if doing so may materially increase risk to the insurer.",
                "This benchmark policy must not be used as an operational legal policy.",
                "prohibition on outsourcing that materially increases insurer risk",
            ),
            (
                "6.2.4",
                "The insurer must not outsource if doing so may compromise fair treatment or continuous satisfactory service to policyholders.",
                "This benchmark policy must not be used as an operational legal policy.",
                "prohibition protecting fair treatment and continuous satisfactory service",
            ),
            (
                "6.4.2",
                "Outsourcing remuneration must not remunerate again a function for which commission or a binder fee is payable.",
                "This benchmark policy must not be used as an operational legal policy.",
                "prohibition on duplicate commission or binder-fee remuneration",
            ),
            (
                "6.4.3",
                "Outsourcing remuneration must not increase the risk of unfair treatment of policyholders.",
                "This benchmark policy must not be used as an operational legal policy.",
                "remuneration must not increase unfair-treatment risk",
            ),
            (
                "6.5",
                "The paragraph 6 principles must apply to authorised sub-outsourcing under the outsourcing contract.",
                "This policy applies to all aspects of the insurer's insurance business outsourced to another person.",
                "paragraph 6 principles applied to authorised sub-outsourcing",
            ),
            (
                "7.2.3",
                "The outsourcing policy must provide guidance on contractual and other risks to be assessed, monitored and managed.",
                "The insurer must assess whether the provider is fit and proper by evaluating competence and integrity.",
                "guidance on contractual and other outsourcing risks",
            ),
            (
                "7.4",
                "The insurer must ensure affected business units and staff are aware of and comply with the outsourcing policy.",
                "Before outsourcing, the insurer must assess provider competence and integrity.",
                "affected business units and staff awareness and compliance",
            ),
            (
                "7.5.1",
                "Before outsourcing, the insurer must assess costs, benefits and potential risk to its insurance business.",
                "Before outsourcing, the insurer must assess the provider's operational capability.",
                "cost-benefit and insurance-business risk assessment",
            ),
            (
                "7.5.2",
                "Before outsourcing, the insurer must identify providers through objective procurement and selection procedures.",
                "Before outsourcing, the insurer must assess the provider's operational capability.",
                "objective procurement and provider-selection procedures",
            ),
            (
                "7.5.3",
                "Before outsourcing, the insurer must assess the impact of multiple outsourcing arrangements and cross-insurer concentration.",
                "Before outsourcing, the insurer must assess the provider's operational capability.",
                "multiple-outsourcing and cross-insurer concentration assessment",
            ),
            (
                "7.5.5",
                "Before outsourcing, the insurer must assess the provider's governance, risk management, internal controls and ability to comply with applicable laws.",
                "Before outsourcing, the insurer must assess the provider's competence and integrity.",
                "provider governance, risk, controls and legal-compliance assessment",
            ),
            (
                "7.7.1",
                "A written outsourcing contract must specify its duration.",
                "Every written outsourcing contract must specify the type of function performed.",
                "contract duration",
            ),
            (
                "7.7.4",
                "A written outsourcing contract must require the provider to maintain appropriate governance, risk management and internal controls.",
                "Every written outsourcing contract must provide for business-contingency processes.",
                "contract requires provider governance, risk management and controls",
            ),
            (
                "7.7.5",
                "A written outsourcing contract must require the provider to comply with applicable laws.",
                "Every written outsourcing contract must provide for business-contingency processes.",
                "contract requires provider compliance with applicable laws",
            ),
            (
                "7.7.7",
                "A written outsourcing contract must specify the type and frequency of reporting by the provider.",
                "Every written outsourcing contract must specify the type of function performed.",
                "contract specifies reporting type and frequency",
            ),
            (
                "7.7.12",
                "A written outsourcing contract must address sub-outsourcing.",
                "Every written outsourcing contract must address confidentiality, privacy and information security.",
                "contract addresses sub-outsourcing",
            ),
            (
                "9.1",
                "Any outsourcing on or after the date Directive 159 takes effect must comply with the Directive.",
                "A South African outsourcing arrangement entered into before Directive 159 took effect must comply when the contract is extended, renewed or amended.",
                "post-effective-date outsourcing compliance",
            ),
        ]
        for section, obligation, evidence, expected_missing in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    obligation,
                    obligation,
                    evidence,
                    section,
                    candidate_score=0.95,
                    source_method="native",
                )
                self.assertNotEqual(ledger["status"], "Completely Covered")
                self.assertIn(expected_missing, ledger["missing"])

    def test_exact_clause_payloads_can_still_be_completely_covered(self) -> None:
        cases = [
            ("1", "Every insurer must comply with all requirements of FSCA Directive 159 whenever outsourcing insurance business."),
            ("3.1", "This South African FSCA policy must apply to all insurers, including qualifying reinsurers."),
            ("3.4.2", "This policy must apply to the outsourcing of subsidiary insurance business conducted inside or outside South Africa."),
            ("3.7", "In addition to Directive 159, the insurer must comply with the existing regulatory framework and specific regulatory requirements for binder business."),
            ("5.2.3", "The insurer must consider the difficulty and time required to replace the provider or perform the function in-house."),
            ("6.2.1", "The insurer must not outsource any function if the outsourcing may materially increase risk to the insurer."),
            ("6.2.4", "The insurer must not outsource if doing so may compromise fair treatment or continuous and satisfactory service to policyholders."),
            ("6.4.2", "Outsourcing remuneration must not remunerate again any function for which commission or a binder fee is payable."),
            ("6.4.3", "Outsourcing remuneration must not be structured to increase the risk of unfair treatment of policyholders."),
            ("6.5", "Under South African FSCA Directive 159, the paragraph 6.1 to 6.4 principles must apply to authorised sub-outsourcing under the outsourcing contract."),
            ("7.2.3", "The outsourcing policy must provide guidance on contractual risks and other risks to be assessed, monitored and managed."),
            ("7.4", "The insurer must ensure all affected business units and staff are aware of and comply with the outsourcing policy."),
            ("7.5.1", "Before outsourcing, the insurer must assess the costs and benefits and potential risk to its insurance business."),
            ("7.5.2", "Before outsourcing, the insurer must identify service providers through objective procurement and selection procedures."),
            ("7.5.3", "Before outsourcing, the insurer must assess the impact of multiple outsourcing arrangements and concentration across a number of insurers."),
            ("7.5.5", "Before outsourcing, the insurer must assess the provider's governance, risk management, internal controls and ability to comply with applicable laws."),
            ("7.7.1", "Every written outsourcing contract must specify the duration of the contract."),
            ("7.7.4", "Every written outsourcing contract must require the service provider to maintain appropriate governance, risk management and internal controls."),
            ("7.7.5", "Every written outsourcing contract must require the service provider to comply with applicable laws."),
            ("7.7.7", "Every written outsourcing contract must specify the type and frequency of reporting by the service provider."),
            ("7.7.12", "Every written outsourcing contract must address sub-outsourcing."),
            ("9.1", "Every new South African outsourcing arrangement on or after Directive 159 takes effect must comply with Directive 159."),
        ]
        for section, wording in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    wording,
                    wording,
                    wording,
                    section,
                    candidate_score=0.95,
                    source_method="native",
                )
                self.assertEqual(ledger["status"], "Completely Covered")

    def test_gemini_cannot_downgrade_stronger_deterministic_evidence(self) -> None:
        evidence = "The insurer must maintain appropriate internal controls."
        task = {
            "id": "row-5.2.2",
            "section": "5.2.2",
            "directive_text": "The insurer must maintain appropriate internal controls and meet regulatory requirements.",
            "obligation": "The insurer must maintain appropriate internal controls and meet regulatory requirements.",
            "candidates": [{
                "candidate_id": "candidate-1",
                "page": "1",
                "text": evidence,
                "score": 0.9,
                "keyword_score": 0.7,
                "hits": ["maintain", "internal", "controls"],
                "method": "native",
            }],
        }
        fallback = _fallback_assessment(task)
        self.assertEqual(fallback["status"], "Partially Covered")
        task["fallback_assessment"] = fallback
        self.assertIsNone(_apply_gemini_assessment(task, {
            "coverage_status": "Completely Missing",
            "candidate_id": "",
            "evidence_quote": "",
            "rationale": "No coverage.",
        }))

    def test_section_1_prefers_scope_evidence_over_legacy_contract(self) -> None:
        directive = (
            "Insurers must comply with the requirements set out in Directive 159 "
            "when outsourcing an aspect of their insurance business."
        )
        chunks = [
            {
                "page": "1",
                "text": (
                    "2.1 For South African operations under FSCA Directive 159, "
                    "this policy applies to all aspects of the insurer's insurance "
                    "business that are or may be outsourced to another person."
                ),
                "method": "native",
            },
            {
                "page": "2",
                "text": (
                    "8.1 A South African outsourcing arrangement entered into "
                    "before Directive 159 took effect must comply with the Directive "
                    "when the contract is extended, renewed or amended."
                ),
                "method": "native",
            },
        ]
        ranked = rank_policy_matches(
            directive,
            directive,
            chunks,
            limit=2,
            evidence_index=build_policy_evidence_index(chunks),
            section="1",
        )
        self.assertEqual(ranked[0]["page"], "1")
        self.assertIn("all aspects", ranked[0]["text"])
        self.assertEqual(ranked[0]["preferred_evidence_score"], 1.0)
        self.assertEqual(ranked[1]["preferred_evidence_score"], 0.0)

    def test_section_3_1_prefers_south_african_scope_over_related_party_clause(self) -> None:
        directive = "This Directive applies to all insurers, including qualifying reinsurers."
        chunks = [
            {
                "page": "1",
                "text": (
                    "2.1 For South African operations under FSCA Directive 159, this policy "
                    "applies to all aspects of the insurer's insurance business that are or may "
                    "be outsourced to another person."
                ),
                "method": "native",
            },
            {
                "page": "2",
                "text": (
                    "2.2 Under FSCA Directive 159, this policy applies when the service provider "
                    "is a related party or an inter-related party of the insurer."
                ),
                "method": "native",
            },
        ]
        ranked = rank_policy_matches(
            directive,
            directive,
            chunks,
            limit=2,
            evidence_index=build_policy_evidence_index(chunks),
            section="3.1",
        )
        self.assertEqual(ranked[0]["page"], "1")
        self.assertEqual(ranked[0]["preferred_evidence_score"], 1.0)
        self.assertEqual(ranked[1]["preferred_evidence_score"], 0.0)

    def test_gemini_cannot_replace_section_1_scope_evidence_with_legacy_clause(self) -> None:
        directive = (
            "Insurers must comply with the requirements set out in Directive 159 "
            "when outsourcing an aspect of their insurance business."
        )
        scope_evidence = (
            "For South African operations under FSCA Directive 159, this policy "
            "applies to all aspects of the insurer's insurance business that are "
            "or may be outsourced to another person."
        )
        legacy_evidence = (
            "A South African outsourcing arrangement entered into before "
            "Directive 159 took effect must comply with the Directive whenever "
            "the related contract is extended, renewed or amended."
        )
        task = {
            "id": "row-1",
            "section": "1",
            "directive_text": directive,
            "obligation": directive,
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "page": "1",
                    "text": scope_evidence,
                    "score": 0.70,
                    "keyword_score": 0.60,
                    "hits": ["directive", "outsourcing"],
                    "method": "native",
                },
                {
                    "candidate_id": "candidate-2",
                    "page": "2",
                    "text": legacy_evidence,
                    "score": 0.69,
                    "keyword_score": 0.60,
                    "hits": ["directive", "outsourcing"],
                    "method": "native",
                },
            ],
        }
        fallback = _fallback_assessment(task)
        self.assertEqual(fallback["status"], "Partially Covered")
        self.assertIn("all aspects", fallback["evidence"])
        task["fallback_assessment"] = fallback
        self.assertIsNone(_apply_gemini_assessment(task, {
            "coverage_status": "Partially Covered",
            "candidate_id": "candidate-2",
            "evidence_quote": legacy_evidence,
            "rationale": "The legacy clause is relevant.",
        }))

    def test_board_approved_outsourcing_policy_can_still_be_complete(self) -> None:
        obligation = "The insurer must have an outsourcing policy approved by its board of directors."
        evidence = "ABC shall maintain this Board-approved outsourcing policy for all material outsourcing arrangements."
        ledger = _coverage_ledger(
            obligation,
            obligation,
            evidence,
            "7.1",
            candidate_score=0.95,
            source_method="native",
        )
        self.assertEqual(ledger["status"], "Completely Covered")

    def test_board_accountability_evidence_cannot_leak_into_policy_approval(self) -> None:
        obligation = "The insurer must have an outsourcing policy approved by its board of directors."
        transfer_clause = (
            "Once the Board Risk and Capital Committee has approved an outsourcing proposal, "
            "responsibility for the outsourced activity transfers to the Chief Operating Officer "
            "and the service provider."
        )
        policy_clause = (
            "The Board Risk and Capital Committee approves this outsourcing policy and the "
            "outsourcing risk appetite."
        )
        transfer_ledger = _coverage_ledger(
            obligation,
            obligation,
            transfer_clause,
            "7.1",
            candidate_score=0.99,
            source_method="native",
        )
        self.assertEqual(transfer_ledger["status"], "Completely Missing")

        task = {
            "id": "row-1",
            "section": "7.1",
            "directive_text": obligation,
            "obligation": obligation,
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "page": "3",
                    "text": transfer_clause,
                    "score": 0.99,
                    "method": "native",
                },
                {
                    "candidate_id": "candidate-2",
                    "page": "2",
                    "text": policy_clause,
                    "score": 0.60,
                    "method": "native",
                },
            ],
        }
        fallback = _fallback_assessment(task)
        self.assertEqual(fallback["status"], "Completely Covered")
        self.assertEqual(fallback["page"], "2")
        self.assertIn("approves this outsourcing policy", fallback["evidence"])

    def test_draft_clause_cleans_duplicate_actor_and_known_ocr_noise(self) -> None:
        board_clause = _draft_policy_clause(
            "6.1",
            "The board remains responsible.",
            "The board of directors and managing executives of an insurer must remain responsible for the insurance business regardless of outsourcing.",
        )
        sub_outsourcing_clause = _draft_policy_clause(
            "6.5",
            "The principles also apply to sub-outsourcing.",
            "The principles referred to under paragraphs 6.1 to 6.4 also apply to any sub- outsourcing.",
        )
        ocr_clause = _draft_policy_clause(
            "3.6",
            "Pricing and actuarial services.",
            "This Directive applies to outsourcing such as pricing and actuarial services} by an insurer.",
        )
        self.assertNotIn("must The board", board_clause)
        self.assertIn("The board of directors", board_clause)
        self.assertIn("the insurer must require the sub-outsourcing", sub_outsourcing_clause)
        self.assertIn("applicable directive sections 6.1 to 6.4", sub_outsourcing_clause)
        self.assertNotIn("sub- outsourcing", sub_outsourcing_clause)
        self.assertNotIn("}", ocr_clause)

    def test_priority_and_timeframe_distinguish_high_impact_from_ordinary_gap(self) -> None:
        high = _priority(
            "Partially Covered",
            "High",
            ["external regulatory notification or reporting"],
            section="8.2",
            obligation="The insurer must immediately notify the Registrar.",
        )
        medium = _priority(
            "Completely Missing",
            "High",
            ["mandatory policy requirement", "core action and subject matter"],
            section="7.5.2",
            obligation="The insurer must use objective selection procedures.",
        )
        self.assertEqual(high, "High")
        self.assertEqual(medium, "Medium")
        self.assertIn("Immediate interim control", _target_timeframe("Partially Covered", high))
        self.assertIn("Within 60 calendar days", _target_timeframe("Completely Missing", medium))

    def test_unresolvable_llm_evidence_is_rejected(self) -> None:
        task = {
            "id": "row-1",
            "section": "8.2",
            "directive_text": "The insurer must notify the FSCA immediately.",
            "obligation": "The insurer must notify the FSCA immediately.",
            "candidates": [{
                "candidate_id": "candidate-1",
                "page": "2",
                "text": "The policy requires immediate notification to the FSCA.",
                "score": 0.9,
                "keyword_score": 0.8,
                "hits": ["notification", "FSCA"],
            }],
        }
        result = _apply_gemini_assessment(task, {
            "coverage_status": "Completely Covered",
            "candidate_id": "candidate-99",
            "evidence_quote": "invented evidence that does not occur in the policy",
            "rationale": "Covered.",
        })
        self.assertIsNone(result)

    def test_fallback_recommendation_does_not_duplicate_actor_and_must(self) -> None:
        recommendation = recommendation_for(
            "Completely Missing",
            "An insurer must notify the Registrar before outsourcing.",
            section="8.1",
            directive_text="An insurer must notify the Registrar before outsourcing.",
        )
        self.assertIn("The insurer must notify the regulator", recommendation)
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
            self.assertIn("outsourcing policy", recommendation)
            self.assertNotIn("South African outsourcing policy", recommendation)

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
            self.assertIn("intermediary services", row["Policy Gap and Recommendations"])
            self.assertIn("Saudi Arabia", row["Corresponding Policy Text"])
            gap_log = next(log for log in result["logs"] if log["stage"] == "Gap Analysis")
            self.assertIn("Gemini produced 1 validated assessment", gap_log["message"])


class ConsolidatedBenchmarkTests(unittest.TestCase):
    def test_strict_equivalent_controls_cover_scope_risk_and_contract_clauses(self) -> None:
        cases = [
            (
                "3.1",
                "Directive 159 applies to all insurers and, subject to paragraph 3.6, reinsurers.",
                "Directive 159 applies to every Aegis insurer and, subject to paragraph 3.6 of that Directive, to every Aegis reinsurer.",
            ),
            (
                "3.2",
                "The Directive applies to all aspects of insurance business that are or may be outsourced to another person, but excludes intermediary services.",
                "This policy applies to every aspect of insurance business that is or may be outsourced to another person, other than intermediary services.",
            ),
            (
                "3.6",
                "Pricing or actuarial services between an insurer and reinsurer are in scope whether under a reinsurance contract or not, except the actual insurance.",
                "This includes pricing and actuarial services outsourced by an insurer to a reinsurer or by a reinsurer to an insurer, whether under a reinsurance contract or not, but excludes the actual insurance.",
            ),
            (
                "7.2.3",
                "The outsourcing policy must provide guidance on contractual and other outsourcing risks to be assessed, monitored and managed.",
                "The policy and procedure must provide guidance on contractual risks and every other outsourcing risk to be assessed, monitored and managed.",
            ),
            (
                "7.7.6",
                "The contract must specify the Rand remuneration or consideration, or its calculation basis if the value is not fixed.",
                "The contract must specify the Rand value of remuneration or consideration payable or, where it is not fixed or determined, the basis on which it will be calculated.",
            ),
            (
                "7.7.18",
                "The contract must set out warranties, guarantees and insurance secured by the provider for contractual performance.",
                "The contract must set out warranties and guarantees and insurance secured by the service provider for its ability to fulfil contractual obligations.",
            ),
        ]
        for section, requirement, evidence in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    requirement,
                    requirement,
                    evidence,
                    section=section,
                    candidate_score=0.8,
                    source_method="native",
                )
                self.assertEqual(ledger["status"], "Completely Covered")
                self.assertEqual(ledger["coverage_percentage"], 100)

    def test_fallback_selects_validated_operative_clause_not_top_lexical_hit(self) -> None:
        obligation = (
            "Any outsourcing of a control, management or material function must be "
            "governed by a written contract describing all material aspects, rights, "
            "responsibilities and service-level requirements."
        )
        task = {
            "id": "row-7.6",
            "section": "7.6",
            "directive_text": obligation,
            "obligation": obligation,
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "page": "2",
                    "text": "The Board must approve material outsourcing proposals.",
                    "score": 0.90,
                    "specific_material_score": 0.0,
                    "method": "native",
                },
                {
                    "candidate_id": "candidate-2",
                    "page": "3",
                    "text": (
                        "Every outsourcing of a control, management or material function "
                        "must be governed by a written contract that clearly describes all "
                        "material aspects, rights, responsibilities and service-level requirements."
                    ),
                    "score": 0.72,
                    "specific_material_score": 1.0,
                    "method": "native",
                },
            ],
        }
        result = _fallback_assessment(task)
        self.assertEqual(result["status"], "Completely Covered")
        self.assertEqual(result["page"], "3")
        self.assertIn("service-level requirements", result["evidence"])

    def test_equivalent_contract_wording_handles_hyphens_and_plurals(self) -> None:
        cases = [
            (
                "7.7.13",
                "A written contract must address ownership of intellectual property.",
                "The contract must specify intellectual-property ownership.",
            ),
            (
                "7.7.17",
                "A written contract must include indemnity and liability provisions.",
                "The contract must include indemnities and liabilities.",
            ),
            (
                "7.7.3",
                "The contract must specify service levels and standards for policyholders and the insurer.",
                "The contract must specify service-level requirements for the bank and its customers.",
            ),
        ]
        for section, obligation, evidence in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    obligation,
                    obligation,
                    evidence,
                    section,
                    candidate_score=0.95,
                    source_method="native",
                )
                self.assertEqual(ledger["status"], "Completely Covered")

    def test_internal_policy_numbering_can_prove_equivalent_directive_payload(self) -> None:
        cases = [
            (
                "7.2.2",
                "The policy must set limits on types, overall level and concentration with one provider.",
                (
                    "The outsourcing risk appetite must set documented limits on the types and "
                    "overall level of outsourced functions and the extent to which activities "
                    "may be outsourced to the same service provider."
                ),
            ),
            (
                "7.4",
                "Affected business units and staff must be aware of and comply with the outsourcing policy.",
                (
                    "All affected business units and staff must be made aware of, and comply "
                    "with, this outsourcing policy."
                ),
            ),
            (
                "7.8",
                "Permitted sub-outsourcing must comply with Directive 159 paragraphs 7.6 and 7.7.",
                (
                    "Every written outsourcing contract must address permitted sub-outsourcing "
                    "and require compliance with paragraphs 7.6 and 7.7."
                ),
            ),
        ]
        for section, obligation, evidence in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    obligation,
                    obligation,
                    evidence,
                    section,
                    candidate_score=0.95,
                    source_method="native",
                )
                self.assertEqual(ledger["status"], "Completely Covered")

    def test_no_function_may_be_outsourced_is_mandatory_language(self) -> None:
        obligation = (
            "No control, management or material function may be outsourced before "
            "internal review and approval."
        )
        evidence = (
            "No control, management or material function may be outsourced before "
            "due diligence, second-line review and Board approval."
        )
        ledger = _coverage_ledger(
            obligation,
            obligation,
            evidence,
            "7.2.4",
            candidate_score=0.95,
            source_method="native",
        )
        self.assertEqual(ledger["language_strength"], "mandatory")

    def test_adverse_zero_positive_coverage_exports_consistent_percentage(self) -> None:
        obligation = (
            "The materiality assessment must consider replacement difficulty, "
            "replacement time and the in-house alternative."
        )
        evidence = (
            "Assessment of replacement difficulty, time and returning the activity "
            "in-house is optional and may be omitted."
        )
        ledger = _coverage_ledger(
            obligation,
            obligation,
            evidence,
            "5.2.3",
            candidate_score=0.95,
            source_method="native",
        )
        self.assertEqual(ledger["status"], "Partially Covered")
        self.assertGreater(ledger["coverage_percentage"], 0)

    def test_adverse_policy_clauses_are_cited_as_partial_not_discarded(self) -> None:
        cases = [
            (
                "6.1",
                "The board and managing executives must remain responsible regardless of outsourcing.",
                (
                    "Once approved, responsibility transfers to the chief operating officer and the "
                    "service provider. The board and managing executives are thereafter not responsible."
                ),
            ),
            (
                "6.3",
                "The insurer must avoid or mitigate conflicts of interest.",
                (
                    "Employees and service providers are encouraged to disclose actual conflicts of "
                    "interest when convenient; minor conflicts may be managed informally."
                ),
            ),
            (
                "6.4.4",
                "Remuneration must not be linked to claims repudiated, paid, not paid or partially paid.",
                (
                    "A performance fee of 0.5% of the monetary value of claims repudiated, not paid "
                    "or partially paid may be approved."
                ),
            ),
            (
                "7.7.15",
                "The written contract must allow the Registrar access to the provider's business and information.",
                (
                    "Regulator access to records is subject to the provider's prior written consent, "
                    "and the provider may withhold commercially sensitive information."
                ),
            ),
        ]
        for section, obligation, evidence in cases:
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    obligation,
                    obligation,
                    evidence,
                    section,
                    candidate_score=0.95,
                    source_method="native",
                )
                self.assertEqual(ledger["status"], "Partially Covered")
                self.assertTrue(ledger["adverse_evidence"])

    def test_adverse_policy_clause_outranks_unrelated_positive_wording(self) -> None:
        obligation = "The outsourcing policy must be reviewed and adapted at least annually."
        chunks = [
            {
                "page": "4",
                "text": (
                    "9.4 This outsourcing policy must be reviewed once every 24 months "
                    "and may be reviewed earlier after a significant regulatory change."
                ),
                "method": "native",
            },
            {
                "page": "5",
                "text": (
                    "12.2 Relevant employees must complete annual outsourcing and "
                    "third-party risk training."
                ),
                "method": "native",
            },
        ]
        ranked = rank_policy_matches(
            obligation,
            obligation,
            chunks,
            limit=2,
            evidence_index=build_policy_evidence_index(chunks),
            section="7.3",
        )
        self.assertEqual(ranked[0]["page"], "4")
        self.assertEqual(ranked[0]["adverse_evidence_score"], 1.0)
        ledger = _coverage_ledger(
            obligation,
            obligation,
            ranked[0]["text"],
            "7.3",
            candidate_score=ranked[0]["score"],
            source_method="native",
        )
        self.assertEqual(ledger["status"], "Partially Covered")
        self.assertIn("specified timing or frequency", ledger["missing"])

    def test_notification_payload_is_not_reported_missing_when_only_timing_is_wrong(self) -> None:
        evidence = (
            "Regulatory Compliance must notify the South African regulator within 30 calendar days "
            "after the contract becomes effective. The notice must identify the service provider and "
            "summarise key risks and mitigation."
        )
        cases = [
            ("8.1.2", "identify the service provider", "notification includes service-provider details"),
            ("8.1.3", "describe key risks and mitigation strategies", "notification includes key risks and mitigation strategies"),
        ]
        for section, payload, label in cases:
            obligation = (
                "The insurer must notify the Registrar no later than one month prior to the effective "
                f"date and {payload}."
            )
            with self.subTest(section=section):
                ledger = _coverage_ledger(
                    obligation,
                    obligation,
                    evidence,
                    section,
                    candidate_score=0.95,
                    source_method="native",
                )
                self.assertEqual(ledger["status"], "Partially Covered")
                self.assertIn(label, ledger["matched"])
                self.assertIn("specified timing or frequency", ledger["missing"])

    def test_gemini_cannot_hide_adverse_policy_evidence(self) -> None:
        obligation = "The outsourcing policy must be reviewed and adapted at least annually."
        adverse = "This outsourcing policy must be reviewed once every 24 months."
        training = "Employees must complete annual outsourcing training."
        task = {
            "id": "row-7.3",
            "section": "7.3",
            "directive_text": obligation,
            "obligation": obligation,
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "page": "4",
                    "text": adverse,
                    "score": 1.0,
                    "method": "native",
                },
                {
                    "candidate_id": "candidate-2",
                    "page": "5",
                    "text": training,
                    "score": 0.5,
                    "method": "native",
                },
            ],
        }
        fallback = _fallback_assessment(task)
        self.assertTrue(fallback["ledger"]["adverse_evidence"])
        task["fallback_assessment"] = fallback
        self.assertIsNone(_apply_gemini_assessment(task, {
            "coverage_status": "Completely Covered",
            "candidate_id": "candidate-2",
            "evidence_quote": training,
            "rationale": "Annual wording appears relevant.",
        }))

    def test_non_operational_benchmark_notice_cannot_create_partial_coverage(self) -> None:
        obligation = (
            "An insurer must not outsource any function or activity if that outsourcing "
            "may impair the ability of the Registrar to monitor regulatory compliance."
        )
        ledger = _coverage_ledger(
            obligation,
            obligation,
            (
                "Benchmark notice: This policy is intentionally incomplete and is not an "
                "operational legal policy."
            ),
            "6.2.3",
        )
        self.assertEqual(ledger["status"], "Completely Missing")

    def test_clause_specific_relevance_rejects_unrelated_controls(self) -> None:
        cases = [
            (
                "6.3",
                "The insurer must avoid or mitigate conflicts of interest involving policyholders and providers.",
                "The insurer must assess material impact on policyholders and finances.",
            ),
            (
                "7.5.8",
                "Before outsourcing, the insurer must maintain contingency plans for termination or failure.",
                "Before outsourcing, the insurer must assess provider competence and integrity.",
            ),
            (
                "7.7.8",
                "The contract must provide for monitoring provider performance and compliance.",
                "The contract must contain business-continuity provisions.",
            ),
            (
                "7.11.2",
                "The insurer must regularly assess the provider's ability to comply with applicable laws.",
                "The insurer must assess the provider's operational capability.",
            ),
        ]
        for section, obligation, evidence in cases:
            with self.subTest(section=section):
                self.assertEqual(
                    _coverage_ledger(obligation, obligation, evidence, section)["status"],
                    "Completely Missing",
                )

    def test_contaminated_headings_do_not_create_false_missing_elements(self) -> None:
        approval = _coverage_ledger(
            "secure the necessary approvals in accordance with the outsourcing policy. Written contracts",
            "Before outsourcing, the insurer must secure the necessary approvals.",
            "",
            "7.5.9",
        )
        self.assertIn("documented approval before outsourcing", approval["required"])
        self.assertNotIn("confidentiality and data protection", approval["required"])
        self.assertNotIn(
            "continuous adequacy of organisation or management",
            approval["required"],
        )
        sub_outsourcing = _coverage_ledger(
            "Sub-outsourcing must comply with sections 7.6 and 7.7. Management and regular review",
            "Permitted sub-outsourcing must comply with sections 7.6 and 7.7.",
            "",
            "7.8",
        )
        self.assertNotIn(
            "ongoing monitoring, assessment or review",
            sub_outsourcing["required"],
        )
        self.assertNotIn("specified timing or frequency", sub_outsourcing["required"])

    def test_recommendation_packages_fix_known_substantive_defects(self) -> None:
        sub_outsourcing = _draft_policy_clause(
            "7.8",
            "Where a contract permits sub-outsourcing, it must comply with sections 7.6 and 7.7.",
            "Where a contract permits sub-outsourcing, it must comply with sections 7.6 and 7.7.",
        )
        self.assertNotIn("must Where", sub_outsourcing)
        self.assertIn("applicable directive sections 7.6 and 7.7", sub_outsourcing)
        self.assertNotIn("paragraph 7.5 below", _draft_policy_clause(
            "7.2.4",
            "Provide for internal review and approval consistent with paragraph 7.5 below.",
            "An outsourcing policy must provide for internal review and approval.",
        ))
        owner = _recommendation_owner(
            pd.Series({
                "Primary Responsible Department": "Operations",
                "Support Function": "Outsourcing Management",
            }),
            "Partially Covered",
            ["external regulatory notification or reporting"],
            section="8.1.2",
        )
        self.assertIn("Regulatory Compliance (accountable)", owner)
        self.assertIn("Operations / Outsourcing Management (responsible)", owner)
        self.assertIn(
            "5 business days",
            _target_timeframe("Partially Covered", "Medium", section="9.2"),
        )
        self.assertIn(
            "historical-exception log",
            _implementation_evidence(
                "Partially Covered",
                "Legacy outsourcing must comply when renewed.",
                "Legacy outsourcing must comply when renewed.",
                section="9.2",
                missing_elements=["historical deadline treatment"],
            ),
        )
        self.assertEqual(
            _gap_type("7.8", "", "contract sub-outsourcing", []),
            "Legal / Contractual",
        )
        self.assertEqual(
            _gap_type("5.2.2", "", "maintain controls and meet regulatory requirements", []),
            "Operational",
        )

    def test_policy_index_prefilters_without_changing_the_best_match(self) -> None:
        chunks = [
            {"page": "1", "text": "The insurer must maintain a board-approved outsourcing policy."},
            {"page": "2", "text": "The cafeteria menu is reviewed monthly."},
            {"page": "3", "text": "The insurer must notify the FSCA immediately of material outsourcing developments."},
        ]
        obligation = "The insurer must immediately notify the FSCA of material outsourcing developments."
        index = build_policy_evidence_index(chunks)
        indexed = rank_policy_matches(obligation, obligation, chunks, 1, evidence_index=index)
        exhaustive = rank_policy_matches(obligation, obligation, chunks, 1)
        self.assertEqual(indexed[0]["page"], "3")
        self.assertEqual(indexed[0]["page"], exhaustive[0]["page"])

    def test_policy_chunk_cache_returns_isolated_mutable_rows(self) -> None:
        raw = "--- Page 1 | method=native ---\n1. The insurer must maintain an outsourcing policy."
        first = cached_policy_chunks(raw)
        first[0]["text"] = "changed"
        second = cached_policy_chunks(raw)
        self.assertNotEqual(second[0]["text"], "changed")

    def test_llm_is_reserved_for_ambiguous_evidence_selection(self) -> None:
        task = {
            "candidates": [{"score": 0.55, "text": "Relevant policy evidence."}],
        }
        partial = {"status": "Partially Covered", "ledger": {"manual_review": "No"}}
        clear_missing = {"status": "Completely Missing", "ledger": {"manual_review": "Yes"}}
        complete = {"status": "Completely Covered", "ledger": {"manual_review": "No"}}
        self.assertTrue(_needs_llm_adjudication(task, partial))
        self.assertTrue(_needs_llm_adjudication(task, clear_missing))
        self.assertFalse(_needs_llm_adjudication(task, complete))
        self.assertFalse(_needs_llm_adjudication({"candidates": []}, partial))

    def test_gap_benchmark_reports_false_complete_and_recommendation_accuracy(self) -> None:
        expected = pd.DataFrame([
            {
                "Section": "1",
                "Expected Status": "Completely Covered",
                "Expected Missing Elements": "",
                "Expected Evidence Phrase": "board-approved outsourcing policy",
                "Recommendation Must Mention": "",
            },
            {
                "Section": "2",
                "Expected Status": "Partially Covered",
                "Expected Missing Elements": "specified timing or frequency",
                "Expected Evidence Phrase": "notify the FSCA",
                "Recommendation Must Mention": "frequency",
            },
        ])
        actual = [
            {
                "Section": "1",
                "Coverage Status": "Completely Covered",
                "Missing Elements": "",
                "Policy Page": "1",
                "Corresponding Policy Text": "The insurer must maintain a board-approved outsourcing policy.",
                "Policy Gap and Recommendations": "",
                "Draft Policy Clause": "",
                "Recommendation Owner": "N/A",
                "Target Timeframe": "N/A",
                "Implementation Evidence": "N/A",
            },
            {
                "Section": "2",
                "Coverage Status": "Completely Covered",
                "Missing Elements": "",
                "Policy Page": "2",
                "Corresponding Policy Text": "The insurer must notify the FSCA.",
                "Policy Gap and Recommendations": "",
                "Draft Policy Clause": "",
                "Recommendation Owner": "N/A",
                "Target Timeframe": "N/A",
                "Implementation Evidence": "N/A",
            },
        ]
        report = score_gap_benchmark(expected, actual)
        self.assertEqual(report["coverage_status_accuracy_percentage"], 50.0)
        self.assertEqual(report["false_complete_count"], 1)
        self.assertEqual(report["recommendation_accuracy_percentage"], 50.0)

    def test_recommendation_benchmark_requires_every_component(self) -> None:
        expected = pd.DataFrame([{
            "Section": "8.1.2",
            "Expected Gap Type": "Legal / Regulatory",
            "Recommendation Must Mention": "provider details;one month",
            "Draft Must Mention": "notify the Registrar",
            "Owner Must Mention": "Regulatory Compliance;accountable",
            "Timeframe Must Mention": "15 business days",
            "Evidence Must Mention": "submission log;regulator receipt",
            "Forbidden Pattern": r"must\s+Where",
        }])
        actual = [{
            "Section": "8.1.2",
            "Coverage Status": "Partially Covered",
            "Gap Type": "Legal / Regulatory",
            "Policy Gap and Recommendations": "Add provider details within one month.",
            "Draft Policy Clause": "The insurer must notify the Registrar.",
            "Recommendation Owner": "Regulatory Compliance (accountable)",
            "Target Timeframe": "Within 15 business days.",
            "Implementation Evidence": "Submission log and regulator receipt.",
        }]
        passed = score_recommendation_benchmark(expected, actual)
        self.assertEqual(passed["accuracy_percentage"], 100.0)
        actual[0]["Draft Policy Clause"] = "The insurer must Where required notify."
        failed = score_recommendation_benchmark(expected, actual)
        self.assertEqual(failed["accuracy_percentage"], 0.0)
        self.assertIn(
            "forbidden_wording_absent",
            failed["rows"][0]["failed_checks"],
        )

    def test_extraction_benchmark_counts_actor_and_ocr_failures(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            register = Path(folder) / "register.xlsx"
            pd.DataFrame([
                {
                    "Section": "4.1",
                    "Actionable": "Yes",
                    "Language from Directive": "Registrar may refuse registration.",
                    "Obligation": "The insurer must maintain controls.",
                },
                {
                    "Section": "7.7.15",
                    "Actionable": "Yes",
                    "Language from Directive": "allow the Registrar access {to information",
                    "Obligation": "allow the Registrar access {to information",
                },
            ]).to_excel(register, sheet_name="Obligations", index=False)
            expected = pd.DataFrame([
                {
                    "Section": "4.1",
                    "Check Type": "Actionable",
                    "Expected Actionable": "No",
                    "Required Clean Phrase": "",
                    "Forbidden OCR Pattern": "",
                },
                {
                    "Section": "7.7.15",
                    "Check Type": "Text Cleanliness",
                    "Expected Actionable": "Yes",
                    "Required Clean Phrase": "access to information",
                    "Forbidden OCR Pattern": r"\{",
                },
            ])
            report = score_extraction_benchmark(expected, register)
        self.assertEqual(report["splitting_errors"], 1)
        self.assertEqual(report["ocr_cleaning_errors"], 1)
        self.assertEqual(report["accuracy_percentage"], 0.0)


if __name__ == "__main__":
    unittest.main()
