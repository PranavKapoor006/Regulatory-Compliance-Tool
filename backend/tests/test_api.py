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

    def test_empty_crawler_download_is_rejected(self) -> None:
        response = self.client.post("/api/crawler/download", json={"directive_ids": []})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
