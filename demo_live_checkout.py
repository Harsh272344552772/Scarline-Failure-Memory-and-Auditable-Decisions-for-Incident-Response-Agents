"""Generate real loopback HTTP traffic against a synthetic checkout and alert on observed errors."""
import argparse
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import ingress
from local_server import Handler
from store import initialize, now, uid


class Checkout(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/checkout":
            return self.send_error(404)
        self.server.requests_seen += 1
        failed = self.server.fault_enabled and self.server.requests_seen % 2 == 0
        self.send_response(503 if failed else 200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class QuietConsole(Handler):
    def log_message(self, *args):
        pass


def serve(handler, stack):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    stack.callback(server.server_close)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    stack.callback(server.shutdown)
    return server


def measure(url):
    started = now()
    failed = 0
    for _ in range(10):
        try:
            with urlopen(Request(url + "/checkout", data=b""), timeout=3):
                pass
        except HTTPError as err:
            if err.code != 503:
                raise
            failed += 1
            err.close()
        time.sleep(0.06)
    return {"started": started, "ended": now(), "attempts": 10, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, help="Send the measured alert to a running loopback Scarline server.")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        if args.port:
            secret = os.getenv("SCARLINE_CHECKOUT_DEMO_SECRET")
            if not secret:
                parser.error("Set SCARLINE_CHECKOUT_DEMO_SECRET to the value configured on the running receiver.")
            base = f"http://127.0.0.1:{args.port}"
        else:
            from unittest.mock import patch
            secret = secrets.token_urlsafe(32)
            stack.enter_context(patch.dict(os.environ, {"DATABASE_URL":"", "SCARLINE_DB":str(Path(tmp)/"proof.sqlite"),
                "MEKO_API_KEY":"", "MEKO_DATAPACK_ID":"", "OPENAI_API_KEY":"", "SCARLINE_CHECKOUT_DEMO_SECRET":secret}))
            initialize()
            receiver = serve(QuietConsole, stack)
            stack.callback(ingress.start_worker().set)
            base = f"http://127.0.0.1:{receiver.server_port}"
        target = serve(Checkout, stack)
        checkout = f"http://127.0.0.1:{target.server_port}"
        target.requests_seen, target.fault_enabled = 0, False
        baseline = measure(checkout)
        target.fault_enabled = True
        observed = measure(checkout)
        if observed["failed"] / observed["attempts"] < 0.30:
            raise RuntimeError("Threshold did not fire.")
        symptom = (f'Synthetic checkout returned HTTP 503 on {observed["failed"]}/{observed["attempts"]} '
                   f'requests between {observed["started"]} and {observed["ended"]}; '
                   f'baseline {baseline["failed"]}/{baseline["attempts"]}.')
        alert = {"id":uid(),"service":"payments-api","symptom":symptom}
        req = Request(base+"/api/webhooks/checkout-demo", data=json.dumps(alert).encode(),
                      headers={"Content-Type":"application/json","X-Scarline-Token":secret})
        with urlopen(req, timeout=5) as response:
            trace = json.load(response)["trace_id"]
        for _ in range(300 if args.port else 50):
            with urlopen(base+"/api/audit",timeout=3) as response:
                decisions = [e for e in json.load(response)["events"] if e.get("trace_id")==trace and e["kind"]=="decision"]
            if decisions:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("The worker did not publish the alert decision.")
        d=decisions[0]
        if not args.port and (d["verdict"] != "Blocked" or d["execution"]["ran"]):
            raise RuntimeError("Expected the seeded, confirmed worker restart to be blocked.")
        print(json.dumps({"source":"live loopback HTTP / synthetic checkouts","baseline":baseline,
                          "alert_window":observed,"threshold":0.30,"trace_id":trace,
                          "decision_time":d["created"],"action":d["action"],"verdict":d["verdict"],
                          "scar_id":d["scars"][0]["id"] if d["scars"] else None,"executor_ran":d["execution"]["ran"]},indent=2))


if __name__ == "__main__":
    main()

