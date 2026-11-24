import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import app

class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"SCARLINE_DB":str(Path(directory)/"test.sqlite"),"DATABASE_URL":"","MEKO_API_KEY":""}))
        self.client = self.enterContext(TestClient(app))

    def test_incident_lifecycle(self):
        c = self.client
        proposal = {"service":"payments-api","action":"Try staggered restart","symptom":"Timeouts"}
        self.assertEqual(c.get("/api/health").json()["status"], "ok")
        self.assertEqual(c.post("/api/decisions",json=proposal).json()["verdict"], "Proceed")
        scar = c.post("/api/scars",json={**proposal,"reason":"Pool filled","evidence":"Pool at 100%","author":"Responder"}).json()["id"]
        pending = c.post("/api/decisions",json=proposal).json()
        self.assertEqual(pending["verdict"], "Caution")
        url = "/api/scars/"+scar+"/review"
        trial = {"status":"confirmed","reviewer":"Responder","note":"Reviewed log"}
        self.assertEqual(c.post(url,json=trial).status_code,400)
        self.assertEqual(c.post(url,json={**trial,"reviewer":"On-call"}).status_code,200)
        blocked = c.post("/api/decisions",json=proposal).json()
        self.assertEqual(blocked["verdict"], "Blocked")
        self.assertEqual(blocked["scars"][0]["id"], scar)
        self.assertEqual(c.post("/api/services/payments-api/deploy",json={"version":"2.15"}).status_code,200)
        replay = c.post("/api/decisions/"+blocked["id"]+"/replay").json()
        self.assertEqual((replay["verdict"], replay["parent"]), ("Caution",blocked["id"]))
        self.assertEqual(c.post(url,json={**trial,"reviewer":"On-call","status":"disputed"}).status_code,200)
        before = c.get("/api/audit").json()
        self.assertIn(blocked, before["events"])
        with TestClient(app) as restarted:
            self.assertEqual(before, restarted.get("/api/audit").json())

    def test_errors_and_seed(self):
        c = self.client
        state = c.get("/api/workspace").json()
        self.assertEqual(len([e for e in state["events"] if e["kind"]=="decision"]),15)
        proposal = {"service":"missing","action":"Restart","symptom":"Lag"}
        self.assertEqual(c.post("/api/decisions",json=proposal).status_code,404)
        self.assertEqual(c.post("/api/decisions",json={**proposal,"action":" "}).status_code,422)
        self.assertEqual(c.post("/api/decisions/missing/replay").status_code,404)
        self.assertEqual(c.post("/api/meko/search",json=proposal).status_code,503)
        self.assertEqual(c.get("/").status_code,200)
