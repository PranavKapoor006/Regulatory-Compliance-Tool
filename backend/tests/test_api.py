from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        pipeline = response.json()["gap_pipeline"]
        self.assertEqual(pipeline["pipeline_version"], "2026-08-18.2-neutral-recommendations")
        self.assertEqual(pipeline["source_file"], "backend/app/services/gap_service.py")
        self.assertEqual(len(pipeline["source_sha256"]), 64)
        self.assertEqual(response.json()["obligation_extraction"]["pipeline_version"], "2026-08-06.2")
        self.assertTrue(response.json()["crawler"]["enabled"])
        self.assertEqual(response.json()["crawler"]["status"], "offline-ready")
        self.assertFalse(response.json()["crawler"]["network_access"])
        self.assertEqual(response.json()["crawler"]["version"], "2026-08-23-demo.1")
        self.assertTrue(response.json()["crawler"]["safety"]["single_flight"])
        self.assertFalse(response.json()["crawler"]["safety"]["automatic_bulk_download"])
        self.assertEqual(response.json()["crawler"]["safety"]["topic_selection_network_requests"], 0)
        self.assertEqual(response.json()["benchmark"]["version"], "2026-07-27.5")

    def test_crawler_metadata_reports_complete_offline_bundle(self) -> None:
        with patch(
            "app.services.crawler_service.requests.Session.request",
            side_effect=AssertionError("metadata started a network request"),
        ):
            response = self.client.get("/api/crawler/metadata")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "fully-local")
        self.assertFalse(response.json()["network_access"])
        self.assertEqual(response.json()["safety"]["normal_requests_per_crawl"], 0)
        self.assertEqual(response.json()["cache_status"]["files_bundled"], 50)
        self.assertEqual(
            response.json()["expected_category_counts"],
            {
                "Insurer / Micro Insurer": 40,
                "Joint FSCA / PA Directives": 2,
                "Retirement Fund": 8,
            },
        )

    def test_bulk_crawler_actions_are_blocked(self) -> None:
        for path in ["/api/crawler/cache-all", "/api/crawler/export-all"]:
            response = self.client.post(path, json={"refresh": True})
            self.assertEqual(response.status_code, 409, path)
            self.assertIn("disabled", response.json()["detail"].lower())

    def test_local_export_batch_limit_is_enforced_at_api_boundary(self) -> None:
        response = self.client.post(
            "/api/crawler/download",
            json={"directive_ids": [str(index) for index in range(51)]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("at most 50", response.json()["detail"])

    def test_obligation_source_selection_exposes_bundled_pdfs(self) -> None:
        response = self.client.get("/api/obligations/available-directives")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source_mode"], "direct-upload-or-bundled-library")
        self.assertEqual(len(response.json()["documents"]), 50)
        self.assertTrue(all(item["bundled"] for item in response.json()["documents"]))
        missing = self.client.post("/api/obligations/extract", data={"directive_name": "missing.pdf"})
        self.assertEqual(missing.status_code, 404)

    def test_topic_selection_returns_exact_local_population_with_zero_network(self) -> None:
        expected = {
            "Insurer / Micro Insurer": 40,
            "Joint FSCA / PA Directives": 2,
            "Retirement Fund": 8,
        }
        with patch(
            "app.services.crawler_service.requests.Session.request",
            side_effect=AssertionError("topic selection started a network request"),
        ):
            for topic, count in expected.items():
                response = self.client.post(
                    "/api/crawler/search",
                    json={"section": topic, "year": "All", "refresh": True},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["records"]), count)
                self.assertEqual(response.json()["network_requests"], 0)
                self.assertTrue(response.json()["selected_category_status"]["complete"])

    def test_diagnostics_reports_loaded_pipeline(self) -> None:
        response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pipeline"]["pipeline_version"], "2026-08-18.2-neutral-recommendations")
        benchmark = next(check for check in response.json()["checks"] if check["component"] == "Controlled benchmark")
        self.assertEqual(benchmark["status"], "Ready")
        crawler = next(check for check in response.json()["checks"] if check["component"] == "FSCA crawler")
        self.assertEqual(crawler["status"], "Healthy")

    def test_client_facing_release_guards(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        app_source = (project_root / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        logo_source = (project_root / "frontend" / "src" / "assets" / "ey-logo.svg").read_text(encoding="utf-8")
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        vite_config = (project_root / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
        verifier = (project_root / "verify_running_app.ps1").read_text(encoding="utf-8")

        self.assertNotIn("estimated document-grounded accuracy across", app_source)
        self.assertNotIn("Internal evidence-confidence {", app_source)
        self.assertNotIn("Pull Complete Topic", app_source)
        self.assertNotIn("Refresh Selected Topic", app_source)
        self.assertIn("qualified compliance professional", app_source)
        self.assertNotIn("<rect", logo_source)
        self.assertIn('fill="#FFFFFF"', logo_source)
        self.assertNotIn("crawler is online", readme.lower())
        self.assertIn('target: "http://127.0.0.1:8000"', vite_config)
        self.assertIn('[string]$ApiBase = "http://127.0.0.1:8000"', verifier)


if __name__ == "__main__":
    unittest.main()
