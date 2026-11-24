import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ingress
import meko
import reporting
from agents import responder
from benchmark import benchmark
from service import change
from store import initialize, transaction
from workflow import run


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"SCARLINE_DB":str(Path(tmp)/"test.sqlite"), "DATABASE_URL":"",
                              "OPENAI_API_KEY":"", "MEKO_API_KEY":"", "MEKO_DATAPACK_ID":""}))
        initialize()

    def test_version_approval_and_repeated_click(self):
        d = run("payments-api", "Checkout stalls", "Recycle workers")
        self.assertEqual(d["verdict"], "Blocked")
        self.assertFalse(d["execution"]["ran"])
        change(["api","services","payments-api","deploy"], {"version":"2.15"})
        caution = run(parent=d["id"])
        self.assertEqual(caution["verdict"], "Caution")
        self.assertFalse(caution["execution"]["ran"])
        approval = {"mode":"approve","reviewer":"Harsh","note":"Verified a single canary"}
        done = run(parent=caution["id"], approval=approval)
        self.assertEqual(done["execution"]["state"], "succeeded")
        self.assertTrue(done["execution"]["supervised"])
        with self.assertRaises(ValueError):
            run(parent=caution["id"], approval=approval)
        self.assertIn("Verified a single canary", reporting.report())
        self.assertEqual(len(done["steps"]), 6)

    def test_partial_and_failed_recall(self):
        self.assertEqual(run("payments-api","Timeouts","Drain workers")["verdict"], "Caution")
        with patch.object(meko, "recall", return_value={"status":"failed","scar_ids":[],"conversation_id":None}):
            d = run("payments-api","Timeouts","Open dashboard")
            self.assertFalse(d["gap"])
            self.assertEqual(d["verdict"], "Caution")

    def test_remote_tool_contract(self):
        with transaction() as q:
            scar = q("SELECT id FROM scars WHERE service='payments-api'").fetchone()["id"]
        calls = []

        class Session:
            async def call_tool(self, name, args):
                calls.append((name, args))
                if name == "conversation_create":
                    result = {"conversation_id":"cloud-conv"}
                elif name == "context_search":
                    result = {"memory":{"status":"ok","results":[{"memory":"Scarline scar "+scar+" for payments-api v2.14."}]}}
                elif name in {"memory_search", "knowledgebase_search"}:
                    result = {"results": []}
                else:
                    result = {"success":True}
                return SimpleNamespace(isError=False, structuredContent=None,
                                       content=[SimpleNamespace(type="text",text=json.dumps(result))])

        @asynccontextmanager
        async def connected():
            yield Session()

        with patch.dict(os.environ, {"MEKO_API_KEY":"contract-test","MEKO_DATAPACK_ID":"test-pack"}), patch.object(meko,"connection",connected):
            # Remote semantic retrieval changes this otherwise unrelated action to Caution.
            d = run("payments-api","Lag","Rotate handles")
            self.assertEqual(d["verdict"], "Caution")
            self.assertEqual(d["scars"][0]["match"], "semantic")
            payload = {"service":"payments-api","action":"Inspect queue","reason":"Queue busy","evidence":"Log 42","author":"Executor"}
            key = change(["api","scars"],payload)[1]["id"]
            change(["api","scars",key,"review"], {"reviewer":"On-call","status":"confirmed","note":"Verified"})
            with patch.object(responder, "propose",return_value={"action":"Inspect queue","source":"OpenAI","usage":{
                    "input_tokens":10,"output_tokens":4,"total_tokens":14,"model":"test"}}):
                run("payments-api","Slow queue")
            while meko.deliver_one():
                pass
        names = [name for name,_ in calls]
        self.assertEqual(names.count("memory_add"),2)
        self.assertEqual(names.count("track_token_usage"),1)
        self.assertEqual(names.count("conversation_add_message"),2)
        self.assertTrue(all(args["datapack_id"]=="test-pack" for _,args in calls))
        self.assertTrue(all(j["state"]=="acknowledged" for j in reporting.audit()["cloud_jobs"]))

    def test_uncertain_ack_is_visible(self):
        with patch.dict(os.environ, {"MEKO_API_KEY":"contract-test","MEKO_DATAPACK_ID":"test-pack"}):
            with transaction(True) as q:
                meko.enqueue(q, "trace", "job", "memory_add", {"text":"test"})
            async def interrupted(job):
                raise TimeoutError()
            with patch.object(meko,"deliver",interrupted):
                self.assertTrue(meko.deliver_one())
                self.assertFalse(meko.deliver_one())
        self.assertEqual(reporting.audit()["cloud_jobs"][0]["state"],"unverified")

    def test_alert_deduplication_and_trace(self):
        data = json.dumps({"id":"event-1","service":"mail-worker","symptom":"Mail delivery lag"}).encode()
        with patch.dict(os.environ, {"SCARLINE_DATADOG_SECRET":"test"}):
            with self.assertRaises(PermissionError):
                ingress.receive("datadog",data,{})
            result = ingress.receive("datadog",data,{"X-Scarline-Token":"test"})
            self.assertTrue(ingress.consume_one())
            repeated = ingress.receive("datadog",data,{"X-Scarline-Token":"test"})
        self.assertEqual(repeated, {"trace_id":result["trace_id"],"state":"done"})
        hits = [e for e in reporting.audit()["events"] if e.get("trace_id")==result["trace_id"] and e["kind"]=="decision"]
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]["origin"],"webhook")

    def test_pagerduty_signature(self):
        import hashlib
        import hmac
        data=json.dumps({"event":{"id":"pd1","event_type":"incident.triggered","data":{
                         "service":{"summary":"orders-db"},"title":"Replica behind"}}}).encode()
        sig=hmac.new(b"test",data,hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"SCARLINE_PAGERDUTY_SECRET":"test"}):
            result=ingress.receive("pagerduty",data,{"X-PagerDuty-Signature":"v1="+sig})
            self.assertEqual(result["state"],"queued")
            with self.assertRaises(PermissionError):
                ingress.receive("pagerduty",data+b" ",{"X-PagerDuty-Signature":"v1="+sig})

    def test_model_proposal_and_usage_from_provider(self):
        from io import BytesIO
        data = {"choices":[{"message":{"content":json.dumps({"action":"Collect diagnostics"})}}],
                "model":"provider-model","usage":{"prompt_tokens":9,"completion_tokens":3,"total_tokens":12}}
        with patch.dict(os.environ, {"OPENAI_API_KEY":"unit-test"}), patch.object(responder,"urlopen",return_value=BytesIO(json.dumps(data).encode())):
            d=run("orders-db","Replica behind")
        self.assertEqual(d["action"],"Collect diagnostics")
        self.assertEqual(d["proposal"]["usage"]["total_tokens"],12)

    def test_benchmark_measures_equal_workloads(self):
        report=benchmark()
        self.assertEqual(report["scarline"]["incidents"],report["baseline"]["incidents"])
        self.assertEqual(report["baseline"]["repeated_failures"],25)
        self.assertEqual(report["scarline"]["repeated_failures"],0)
        self.assertIsNone(report["scarline"]["tokens_per_incident"])
        self.assertEqual(report["baseline"]["passed_health_checks"],report["scarline"]["passed_health_checks"])

