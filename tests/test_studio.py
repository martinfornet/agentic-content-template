import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from creator.studio import create_app
from creator.workflow import CreatorWorkflow


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        flow = CreatorWorkflow(root)
        self.p = flow.create_proposal(idea="Projet de test", shooting_brief="Deux plans", script="Bonjour.")
        self.url = f"/api/projects/{self.p['id']}"
        self.client = TestClient(create_app(root), base_url="http://localhost")
        self.addCleanup(self.client.close)
        self.headers = {"X-Creator-Studio": "1", "Origin": "http://localhost"}

    def test_reopen_project_after_reload(self):
        self.assertEqual(self.client.get("/api/projects").json()[0]["id"], self.p["id"])
        self.assertIn("Studio créateur", self.client.get("/").text)

    def test_approval_order_and_stale_review(self):
        for version, status in [(self.p["updated_at"], 400), ("ancienne-version", 409)]:
            response = self.client.post(self.url + "/export-ok", headers=self.headers,
                                        data={"version": version, "actor": "test"})
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.client.get("/api/projects").json()[0]["stage"], "proposed")

    def test_local_origin_and_unavailable_media(self):
        self.assertEqual(self.client.post(self.url + "/retake").status_code, 403)
        url = f"/media/{self.p['id']}/video?download=true"
        self.assertEqual(self.client.get(url).status_code, 404)
