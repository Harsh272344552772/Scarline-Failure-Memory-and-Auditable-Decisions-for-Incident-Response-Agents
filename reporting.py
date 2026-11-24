import hashlib
import html
import json
import os
from collections import Counter

import meko
from store import transaction


def events(q):
    return [json.loads(r["payload"]) for r in q("SELECT payload FROM events ORDER BY created DESC,id DESC")]


def workspace():
    import signals
    import simulator
    with transaction() as q:
        jobs = [dict(r) for r in q("SELECT trace_id,kind,state,conversation_id,datapack_id FROM cloud_jobs ORDER BY created")]
        return {"services": [dict(r) for r in q("SELECT * FROM services ORDER BY name")],
                "scars": [dict(r) for r in q("SELECT * FROM scars ORDER BY created DESC")],
                "events": events(q), "meko": meko.enabled(), "cloud_jobs": jobs,
                "health": signals.snapshot(q), "demo": simulator.status(q), "benchmark": benchmark_summary(),
                "inbox": [dict(r) for r in q("SELECT * FROM inbox ORDER BY created DESC LIMIT 100")],
                "meko_calls": [dict(r) for r in q("SELECT tool,state,COUNT(*) AS count FROM meko_calls GROUP BY tool,state ORDER BY tool,state")],
                "kb_files": [dict(r) for r in q("SELECT filename,state,datapack_id FROM kb_files")],
                "datapacks": {label: os.getenv(key) or os.getenv("MEKO_DATAPACK_ID", "") for label,key in
                    (("Operations","MEKO_DATAPACK_ID"),("Knowledge","MEKO_KNOWLEDGE_DATAPACK_ID"),("Audit","MEKO_AUDIT_DATAPACK_ID"))},
                "alert_sources": {name: bool(os.getenv("SCARLINE_"+name.upper().replace("-","_")+"_SECRET"))
                    for name in ("grafana","alertmanager","datadog","pagerduty","checkout-demo")}}


def benchmark_summary():
    from pathlib import Path
    try:
        result = json.loads((Path(__file__).parent / "benchmark-results.json").read_text())
        return {key: result[key] for key in ("baseline", "scarline", "repetitions") if key in result}
    except (OSError, ValueError, KeyError):
        return None


def audit():
    data = workspace()
    return {"project": "Scarline", "environment": "simulation", "events": data["events"], "cloud_jobs": data["cloud_jobs"]}


def revision():
    import time
    with transaction() as q:
        row = q("SELECT id FROM events ORDER BY created DESC,id DESC LIMIT 1").fetchone()
        jobs = [(r["state"], r["n"]) for r in q("SELECT state,COUNT(*) AS n FROM cloud_jobs GROUP BY state ORDER BY state")]
        calls = q("SELECT COUNT(*) AS n FROM meko_calls").fetchone()["n"]
        queue = [(r["state"],r["n"]) for r in q("SELECT state,COUNT(*) AS n FROM inbox GROUP BY state ORDER BY state")]
        kb = [tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in q("SELECT document_key,state FROM kb_files ORDER BY document_key")]
    return hashlib.sha256(repr((row["id"] if row else "", jobs,calls,queue,kb,int(time.time()/30))).encode()).hexdigest()[:16]


def explain(d):
    outcome = d.get("execution", {}).get("detail", "This historical trace contains only a pre-flight decision.")
    return f'On {d["service"]} v{d["version"]}, the proposed action was “{d["action"]}”. {d["reason"]} {outcome}'


def report():
    data = audit()
    decisions = [e for e in data["events"] if e["kind"] == "decision"]
    totals = Counter(e["verdict"] for e in decisions)
    esc = lambda s: html.escape(str(s), quote=True)
    articles = []
    for d in decisions:
        cites = "".join(f'<li>Scar {esc(s["id"][:8])} · {esc(s["status"])} · v{esc(s["version"])}. {esc(s["evidence"])}</li>'
                        for s in d["scars"])
        steps = "".join(f'<li><b>{esc(s["agent"])}</b> — {esc(s["detail"])}</li>' for s in d.get("steps", []))
        link = f'<a href="#d-{esc(d["parent"])}">Earlier decision</a>' if d.get("parent") else ""
        approval = d.get("approval")
        operator = f'<p>Human {esc(approval["mode"])} by {esc(approval["reviewer"])}. {esc(approval["note"])}</p>' if approval else ""
        articles.append(f'<article class="panel" id="d-{esc(d["id"])}"><span class="badge {esc(d["verdict"].lower())}">{esc(d["verdict"])}</span><h2>{esc(d["action"])}</h2><p>{esc(explain(d))}</p>{operator}<ul>{cites}</ul><ol>{steps}</ol><div class="sub">Trace {esc(d.get("trace_id", d["id"]))} · {esc(d["created"])} · {esc(d.get("origin", "historical"))}</div>{link}</article>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scarline · Audit report</title><link rel="stylesheet" href="/style.css"></head><body>'
            '<header><strong class="brand">Scarline</strong><span>Incident audit</span></header><main class="report"><a href="/">← Workspace</a><h1>Decision audit</h1>'
            f'<p>{len(decisions)} traced decisions · {totals["Proceed"]} Proceed · {totals["Caution"]} Caution · {totals["Blocked"]} Blocked</p>'
            '<p>Evidence and service versions are snapshots from the time of each decision.</p><a href="/api/audit">Download the audit JSON</a>'
            + "".join(articles) + '</main></body></html>')

