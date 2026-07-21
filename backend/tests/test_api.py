from __future__ import annotations

import unittest

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
        self.assertEqual(pipeline["pipeline_version"], "2026-07-21.3")
        self.assertEqual(len(pipeline["source_sha256"]), 64)

    def test_empty_crawler_download_is_rejected(self) -> None:
        response = self.client.post("/api/crawler/download", json={"directive_ids": []})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
