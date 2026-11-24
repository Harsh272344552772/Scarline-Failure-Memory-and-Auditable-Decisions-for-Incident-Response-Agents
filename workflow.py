"""One decision path shared by both HTTP launchers, replays, alert jobs and the benchmark."""
import json
import os
import time

import meko
from agents import executor, responder, scribe, skeptic, observer, investigator, risk
from store import SCENARIOS, event, record, review, service_version, transaction, uid


OFFLINE = {"status": "local only", "scar_ids": [], "conversation_id": None}


def old_decision(q, key):
    row = q("SELECT payload FROM events WHERE id=? AND kind='decision'", (key,)).fetchone()
    if not row:
        raise KeyError("Unknown decision")
    return json.loads(row["payload"])


def commit_run(q, service, symptom, proposal, recall, trace_id, parent=None, approval=None, cloud=False, origin="dashboard", observation=None, started=None):
    version = service_version(q, service)
    action = proposal["action"]
    ledger = [dict(r) for r in q("SELECT * FROM scars WHERE service=? ORDER BY created DESC", (service,))]
    verdict, reason, hits = skeptic.evaluate(service, version, action, ledger, recall)
    assessment = risk.assess(action, verdict, hits, version)
    if assessment["verdict"] != verdict:
        verdict, reason = assessment["verdict"], reason + " " + assessment["detail"]
    if assessment["prohibited"] and approval:
        raise ValueError(assessment["detail"])
    if approval:
        previous = old_decision(q, parent)
        expected = "Blocked" if approval["mode"] == "override" else "Caution"
        if previous["version"] != version or verdict != expected or previous["verdict"] != expected:
            raise ValueError("The version or verdict has changed. Replay and review the new decision.")
        if q("SELECT child FROM approvals WHERE parent=?", (parent,)).fetchone():
            raise ValueError("A review for this decision was already applied.")
        q("INSERT INTO approvals VALUES (?,?)", (parent, trace_id))
    outcome = executor.execute(service, version, action, verdict, approval)
    saved = scribe.close(q, service, action, outcome, trace_id, cloud)
    steps = [{"agent": "Observer", "detail": (observation or {}).get("detail", "Incident context received.")},
             {"agent": "Investigator", "detail": f'Proposed “{action}” using {proposal["source"]}.'},
             {"agent": "Skeptic", "detail": reason},
             {"agent": "Risk Evaluator", "detail": assessment["detail"]},
             {"agent": "Executor", "detail": outcome["detail"]},
             {"agent": "Scribe", "detail": saved["detail"]}]
    decision = event(q, "decision", dict(trace_id=trace_id, service=service, version=version, action=action,
        symptom=symptom, verdict=verdict, reason=reason, scars=hits, parent=parent, agent="Skeptic", origin=origin,
        gap=not hits and not recall["scar_ids"] and recall["status"] in ("local only", "ok"),
        risk=assessment, observation=observation, latency_ms=round((time.perf_counter()-started)*1000,2) if started else None,
        proposal=proposal, recall=recall, execution=outcome, scribe=saved, approval=approval, steps=steps))
    for step in steps[-3:]:
        event(q, "agent", {"trace_id": trace_id, "service": service, **step, "state": "completed"})
    if cloud:
        cid = recall.get("conversation_id")
        if cid:
            q("UPDATE cloud_jobs SET conversation_id=? WHERE trace_id=? AND datapack_id=?", (cid, trace_id, os.environ["MEKO_DATAPACK_ID"]))
        meko.enqueue(q, trace_id, uid(), "conversation_add_message", {"input": symptom,
              "output": f'{action} — {verdict}. {reason} {outcome["detail"]} {saved["detail"]}',
              "plan": [s["agent"] + ": " + s["detail"] for s in steps],
              "metadata": {"scarline_trace_id": trace_id, "decision_id": decision["id"], "origin": origin}}, cid)
        if proposal.get("usage"):
            meko.enqueue(q, trace_id, uid(), "track_token_usage", {"name": "Responder proposal", **proposal["usage"]}, cid)
    return decision


def progress(trace_id, service, agent, state, detail):
    with transaction(True) as q:
        event(q, "agent", {"trace_id": trace_id, "service": service, "agent": agent, "state": state, "detail": detail})


def run(service=None, symptom=None, action=None, parent=None, approval=None, trace_id=None, origin="dashboard"):
    started = time.perf_counter()
    trace_id = trace_id or uid()
    with transaction() as q:
        if parent:
            prev = old_decision(q, parent)
            service, symptom, action = prev["service"], prev["symptom"], prev["action"]
        service_version(q, service)
    observation = observer.observe(service)
    progress(trace_id, service, "Observer", "completed", observation["detail"])
    progress(trace_id, service, "Skeptic", "running", "Retrieving failure memory and runbooks.")
    recall = meko.recall(trace_id, service, action or "Investigate incident", symptom)
    progress(trace_id, service, "Skeptic", "completed", "Retrieval " + recall["status"])
    progress(trace_id, service, "Investigator", "running", "Evaluating incident context.")
    proposal = investigator.investigate(service, symptom, action, recall)
    progress(trace_id, service, "Investigator", "completed", proposal["source"] + " proposed " + proposal["action"])
    with transaction(True) as q:
        if origin in {"webhook", "dashboard queue", "live demo", "telemetry"}:
            job = q("SELECT state FROM inbox WHERE trace_id=?", (trace_id,)).fetchone()
            if job and job["state"] in {"done", "resolved"}:
                return
        decision = commit_run(q, service, symptom, proposal, recall, trace_id, parent, approval,
                              meko.enabled(), origin, observation, started)
        if origin in {"webhook", "dashboard queue", "live demo", "telemetry"}:
            q("UPDATE inbox SET state='done',error=NULL WHERE trace_id=?", (trace_id,))
        return decision


def seed(q):
    # Build the exhibit by running the same policy and simulator as an interactive incident.
    for i, (service, version, action, symptom, reason) in enumerate(SCENARIOS):
        q("INSERT INTO services VALUES (?,?)", (service, version))

        def demo(proposed, parent=None):
            proposal = {"action": proposed, "source": "scenario policy", "usage": None}
            return commit_run(q, service, symptom, proposal, OFFLINE, uid(), parent=parent, origin="seed")

        if i % 2 == 0:
            first = demo(action)
            key = first["scribe"].get("scar_id") or record(q, service, action, reason, "Earlier simulation log: " + reason, "Executor")
            held = demo(action, first["id"])
            review(q, key, "On-call", "confirmed", "Confirmed against the simulated incident log.")
            demo(action, held["id"])
        else:
            demo("Collect diagnostics")
            key = record(q, service, action, reason, "Simulation log: " + reason, "Executor")
            review(q, key, "On-call", "confirmed", "Confirmed against the simulated incident log.")
            demo(action)
            demo("Warm cache" if i == 1 else "Inspect index")

