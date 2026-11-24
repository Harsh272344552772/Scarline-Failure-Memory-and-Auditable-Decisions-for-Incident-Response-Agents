"""Regression tests for live intake, access control and cloud evidence contracts."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import cloud_actions
import ingress
import knowledge
import meko
import reporting
import signals
import simulator
from app import app
from service import change
from store import initialize, transaction
from workflow import run


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        values = {"SCARLINE_DB":str(Path(directory)/"test.sqlite"),"DATABASE_URL":"","OPENAI_API_KEY":"",
                  "MEKO_API_KEY":"","MEKO_DATAPACK_ID":"","MEKO_RAW_DATAPACK_ID":"",
                  "MEKO_KNOWLEDGE_DATAPACK_ID":"","MEKO_AUDIT_DATAPACK_ID":"",
                  "SCARLINE_OPERATOR_TOKEN":"","SCARLINE_USERS_JSON":"","SCARLINE_GRAFANA_TIMESTAMP_HEADER":""}
        self.enterContext(patch.dict(os.environ, values))
        initialize()

    def group(self, state="firing", service="payments-api", fingerprint="fp"):
        return {"status": state, "startsAt":"2026-11-24T00:00:00Z", "fingerprint":fingerprint,
                "labels":{"service":service,"alertname":"Latency"},"annotations":{"summary":"High latency"}}

    def receive(self, alerts):
        with patch.dict(os.environ,{"SCARLINE_ALERTMANAGER_SECRET":"fixture"}):
            return ingress.receive("alertmanager",json.dumps({"alerts":alerts}).encode(),{"Authorization":"Bearer fixture"})

    def test_group_is_atomic_and_resolved_work_never_runs(self):
        with self.assertRaises(KeyError):
            self.receive([self.group(),self.group(service="absent",fingerprint="bad")])
        self.assertEqual(reporting.workspace()["inbox"],[])
        first=self.receive([self.group()])["alerts"][0]
        self.assertEqual(first,self.receive([self.group()])["alerts"][0])
        self.receive([self.group("resolved")])
        self.assertFalse(ingress.consume_one())
        self.assertEqual(self.receive([self.group()])["alerts"][0]["state"],"resolved")
        self.assertEqual(len(reporting.workspace()["inbox"]),1)

    def test_resolution_during_retrieval_cancels_execution(self):
        trace=self.receive([self.group()])["alerts"][0]["trace_id"]
        def resolving(*args):
            self.receive([self.group("resolved")])
            return {"status":"local only","scar_ids":[],"conversation_id":None}
        with patch.object(meko,"recall",side_effect=resolving):
            ingress.consume_one()
        self.assertFalse(any(e["kind"]=="decision" and e.get("trace_id")==trace for e in reporting.audit()["events"]))

    def test_grafana_signature_covers_timestamp_and_bytes(self):
        stamp=str(int(datetime.now(timezone.utc).timestamp()))
        raw=json.dumps({"alerts":[self.group()]}).encode()
        sig=hmac.new(b"fixture",stamp.encode()+b":"+raw,hashlib.sha256).hexdigest()
        headers={"X-Grafana-Alerting-Signature":sig,"X-Signed-At":stamp}
        with patch.dict(os.environ,{"SCARLINE_GRAFANA_SECRET":"fixture","SCARLINE_GRAFANA_TIMESTAMP_HEADER":"X-Signed-At"}):
            self.assertEqual(ingress.receive("grafana",raw,headers)["accepted"],1)
            with self.assertRaises(PermissionError):
                ingress.receive("grafana",raw+b" ",headers)
            with self.assertRaises(PermissionError):
                ingress.receive("grafana",raw,{**headers,"X-Signed-At":"1"})

    def test_signal_transitions_dedupe_and_staleness(self):
        def sample(key,error):
            return {"id":key,"service":"payments-api","metrics":{"latency_ms":900,"replication_lag_s":0,"error_rate":error}}
        self.assertNotIn("incident",change(["api","telemetry"],sample("a",0))[1])
        self.assertIn("incident",change(["api","telemetry"],sample("b",0.1))[1])
        self.assertNotIn("incident",change(["api","telemetry"],sample("c",0.2))[1])
        self.assertTrue(change(["api","telemetry"],sample("b",0.1))[1]["duplicate"])
        with self.assertRaises(ValueError):
            change(["api","telemetry"],sample("b",0.2))
        with self.assertRaises(ValueError):
            change(["api","telemetry"],sample("d",float("nan")))
        old=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()
        with transaction(True) as q:
            q("UPDATE signals SET created=?",(old,))
        self.assertEqual(reporting.workspace()["health"]["payments-api"]["state"],"stale")

    def test_demo_stops_and_queues_bounded_work(self):
        simulator.control(True,1)
        self.assertTrue(simulator.tick())
        self.assertFalse(simulator.tick())
        self.assertFalse(reporting.workspace()["demo"]["running"])
        self.assertEqual(len(reporting.workspace()["inbox"]),1)
        self.assertTrue(ingress.consume_one())
        self.assertEqual(reporting.workspace()["inbox"][0]["state"],"done")

    def test_dashboard_incident_is_queued_before_investigation(self):
        with patch.object(ingress,"run") as investigate:
            status,result=change(["api","incidents"],{"service":"payments-api","symptom":"Timeouts"})
            self.assertEqual(status,202)
            investigate.assert_not_called()
        self.assertEqual(result["state"],"queued")
        ingress.consume_one()
        decision=next(e for e in reporting.audit()["events"] if e["kind"]=="decision" and e.get("trace_id")==result["trace_id"])
        self.assertEqual(decision["origin"],"dashboard queue")

    def test_retry_budget_and_manual_retry_preserve_trace(self):
        trace=self.receive([self.group()])["alerts"][0]["trace_id"]
        with patch.object(ingress,"run",side_effect=RuntimeError("secret must not enter logs")):
            for _ in range(3):
                with transaction(True) as q:
                    q("UPDATE inbox SET next_attempt=NULL")
                self.assertTrue(ingress.consume_one())
        item=reporting.workspace()["inbox"][0]
        self.assertEqual((item["state"],item["attempts"],item["error"]),("failed",3,"RuntimeError"))
        self.assertFalse(ingress.consume_one())
        self.assertEqual(ingress.retry(trace)["trace_id"],trace)
        ingress.consume_one()
        self.assertEqual(reporting.workspace()["inbox"][0]["state"],"done")

    def test_destructive_override_cannot_run(self):
        decision=run("payments-api","Timeouts","Delete database")
        self.assertEqual(decision["verdict"],"Blocked")
        self.assertFalse(decision["execution"]["ran"])
        with self.assertRaises(ValueError):
            run(parent=decision["id"],approval={"mode":"override","reviewer":"Human","note":"Try"})

    def test_activity_is_visible_before_model_finishes(self):
        from agents import responder
        previous=reporting.revision()
        def proposal(*args,**kwargs):
            events=reporting.workspace()["events"]
            self.assertTrue(any(e["kind"]=="agent" and e.get("agent")=="Investigator" and e.get("state")=="running" for e in events))
            self.assertNotEqual(previous,reporting.revision())
            return {"action":"Collect diagnostics","source":"test","usage":None}
        with patch.object(responder,"propose",side_effect=proposal):
            result=run("payments-api","Timeouts")
        self.assertEqual(len(result["steps"]),6)

    def test_reads_require_access_and_identity_cannot_be_spoofed(self):
        users=[{"name":"Alice","role":"reviewer","token":"alice-token"},
               {"name":"Bob","role":"reviewer","token":"bob-token"},
               {"name":"Guest","role":"viewer","token":"guest-token"}]
        with patch.dict(os.environ,{"SCARLINE_USERS_JSON":json.dumps(users)}), patch.object(ingress,"start_worker",return_value=threading.Event()), TestClient(app) as c:
            for url in ("/api/workspace","/api/audit","/audit","/api/events"):
                self.assertEqual(c.get(url).status_code,403)
            self.assertEqual(c.post("/api/session",json={"token":"wrong"}).status_code,403)
            login=c.post("/api/session",json={"token":"alice-token"})
            self.assertEqual(login.status_code,200)
            self.assertIn("HttpOnly",login.headers["set-cookie"])
            self.assertEqual(c.get("/api/workspace").status_code,200)
            scar=c.post("/api/scars",json={"service":"payments-api","action":"Inspect pool","reason":"Pool full","evidence":"Pool=100%","author":"Bob"}).json()["id"]
            data={"reviewer":"Someone else","status":"confirmed","note":"Verified"}
            self.assertEqual(c.post("/api/scars/"+scar+"/review",json=data).status_code,400)
            c.post("/api/session",json={"token":"bob-token"})
            self.assertEqual(c.post("/api/scars/"+scar+"/review",json=data).json()["reviewer"],"Bob")
            c.post("/api/session",json={"token":"guest-token"})
            self.assertEqual(c.post("/api/demo",json={"enabled":True}).status_code,403)
            self.assertEqual(c.post("/api/decisions",json=[]).status_code,422)

    def test_schema_changes_and_malformed_search_fail_closed(self):
        session=SimpleNamespace(scarline_tools={"memory_search":{"required":["new_required_field"]}},call_tool=AsyncMock())
        with patch.dict(os.environ,{"MEKO_DATAPACK_ID":"ops"}):
            with self.assertRaises(ValueError):
                asyncio.run(meko.call(session,"memory_search",{}))
        session.call_tool.assert_not_awaited()
        with self.assertRaises(ValueError):
            meko.search_items({"success":True})
        self.assertEqual(meko.search_items({"results":[]}),[])

    def test_runbook_upload_checkpoints_and_retrieves_content(self):
        calls=[]
        @asynccontextmanager
        async def connection():
            yield object()
        async def call(session,name,args,pack=None):
            calls.append((name,args,pack))
            if name=="conversation_create": return {"conversation_id":"kb-cid"}
            if name=="knowledgebase_upload_document":
                self.assertEqual(hashlib.sha256(knowledge.RUNBOOK.read_bytes()).hexdigest(),args["content_hash"])
                self.assertEqual(len(base64.b64decode(args["content_head_base64"])),min(3072,args["size"]))
                return {"key":"kb-key","already_uploaded":True}
            if name=="knowledgebase_document_status": return {"state":"completed"}
            if name=="knowledgebase_search": return {"results":[{"document_id":"kb-id","text":knowledge.MARKER+" Scarline payments-api connection pool"}]}
            raise AssertionError(name)
        with patch.dict(os.environ,{"MEKO_DATAPACK_ID":"ops","MEKO_KNOWLEDGE_DATAPACK_ID":"kb"}),patch.object(meko,"connection",connection),patch.object(meko,"call",side_effect=call):
            for _ in range(2): self.assertEqual(asyncio.run(knowledge.install())["state"],"completed")
        self.assertEqual(sum(name=="knowledgebase_upload_document" for name,_,_ in calls),1)
        self.assertTrue(all(pack=="kb" for _,_,pack in calls))

    def test_artifact_readback_must_match_uploaded_bytes(self):
        saved={}
        @asynccontextmanager
        async def connection():
            yield object()
        async def call(session,name,args,pack=None):
            if name=="conversation_create":return {"conversation_id":"audit-cid"}
            if name=="artifact_put":
                saved.update(args)
                return {"content_hash":hashlib.sha256(base64.b64decode(args["content_base64"])).hexdigest()}
            if name=="artifact_get":return {"content_base64":saved["content_base64"]}
            raise AssertionError(name)
        with patch.dict(os.environ,{"MEKO_DATAPACK_ID":"ops"}),patch.object(meko,"connection",connection),patch.object(meko,"call",side_effect=call):
            self.assertEqual(asyncio.run(cloud_actions.save_audit())["state"],"verified")
        bad=AsyncMock(side_effect=[{"conversation_id":"audit-cid"},{"content_hash":"wrong"}])
        with patch.dict(os.environ,{"MEKO_DATAPACK_ID":"ops"}),patch.object(meko,"connection",connection),patch.object(meko,"call",bad):
            with self.assertRaises(ValueError):asyncio.run(cloud_actions.save_audit())
