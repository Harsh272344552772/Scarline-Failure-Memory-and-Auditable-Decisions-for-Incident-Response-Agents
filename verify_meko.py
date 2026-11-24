"""Opt-in smoke test. Writes one synthetic scar/review and a conversation to your selected datapack."""
import asyncio
import json
import secrets
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import ingress
import meko
from reporting import audit
from service import change
from store import initialize, uid
from workflow import run


async def read_back(cid, pack=None):
    async with asyncio.timeout(18), meko.connection() as session:
        return await meko.call(session, "conversation_get", {"agent_id": meko.OWNER, "conversation_id": cid,
                                "include_messages": True, "limit": 20}, pack)


def main():
    if not meko.enabled():
        sys.exit("Set MEKO_API_KEY and MEKO_DATAPACK_ID first.")
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DATABASE_URL":"", "OPENAI_API_KEY":"", "SCARLINE_DB":str(Path(tmp)/"smoke.sqlite")}):
        initialize()
        service = "payments-api"
        marker = "Smoke check " + uid()[:12]
        key = change(["api","scars"], {"service":service,"action":marker,"reason":"Synthetic pool exhaustion",
                      "evidence":"Synthetic fixture, 100% pool use","author":"Executor"})[1]["id"]
        change(["api","scars",key,"review"], {"reviewer":"Smoke reviewer","status":"confirmed","note":"Checked synthetic fixture."})
        while meko.deliver_one():
            pass
        jobs = audit()["cloud_jobs"]
        if len(jobs) != 2 or not all(j["state"] == "acknowledged" for j in jobs):
            sys.exit("Scar or review did not receive an acknowledgement. Inspect the datapack before retrying.")
        for attempt in range(4):
            decision = run(service, "Synthetic verification " + marker, marker)
            while meko.deliver_one():
                pass
            if key in decision["recall"]["scar_ids"]:
                break
            if attempt < 3:
                time.sleep(3)
        if decision["recall"]["status"] != "ok" or key not in decision["recall"]["scar_ids"]:
            sys.exit("The scar was acknowledged but its reference has not been verified in recall.")
        if decision["verdict"] != "Blocked":
            sys.exit("Unexpected verdict.")
        if any(j["state"] != "acknowledged" for j in audit()["cloud_jobs"]):
            sys.exit("One or more trace writes are unverified.")
        cid = decision["recall"]["conversation_id"]
        if marker not in json.dumps(asyncio.run(read_back(cid))):
            sys.exit("Conversation opened but the stored action was not found on readback.")
        if raw_pack := os.getenv("MEKO_RAW_DATAPACK_ID"):
            secret = secrets.token_urlsafe()
            with patch.dict(os.environ, {"SCARLINE_CHECKOUT_DEMO_SECRET": secret}):
                data = json.dumps({"id": uid(), "service": service, "symptom": "Synthetic intake check"}).encode()
                trace = ingress.receive("checkout-demo", data, {"X-Scarline-Token": secret})["trace_id"]
            ingress.consume_one()
            while meko.deliver_one():
                pass
            alerts = [j for j in audit()["cloud_jobs"] if j["trace_id"] == trace]
            copies = [j for j in alerts if j["datapack_id"] == raw_pack]
            if len(copies) != 1 or any(j["state"] != "acknowledged" for j in alerts):
                sys.exit("The alert intake writes were not acknowledged.")
            stored = asyncio.run(read_back(copies[0]["conversation_id"], raw_pack))
            if "Incoming checkout-demo alert on payments-api." not in json.dumps(stored):
                sys.exit("The alert turn was not found in the raw datapack.")
            print("Verified alert intake readback:", copies[0]["conversation_id"])
        print("Verified scar, review, recall, block and trace readback.")
        print("Meko conversation:", cid)
        print("Scarline trace:", decision["trace_id"])


if __name__ == "__main__":
    main()

