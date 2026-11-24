"""Paired, cold-start incident simulations with isolated ledgers. Optional metered proposals."""
import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from agents import executor, responder, skeptic
from store import DDL, SCENARIOS, review, transaction, uid
from workflow import OFFLINE, commit_run


def arm(protected, count, model):
    totals = dict(incidents=count, repeated_failures=0, failed_executions=0, target_calls=0,
                  passed_health_checks=0, blocks=0, supervised_retests=0, model_usage_reports=0)
    token_counts = []
    known_failures = set()
    delays = []
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        "DATABASE_URL": "", "SCARLINE_DB": str(Path(tmp) / "bench.sqlite")}):
        with transaction(True) as q:
            for statement in DDL:
                q(statement)
            for service, version, *_ in SCENARIOS:
                q("INSERT INTO services VALUES (?,?)", (service, version))
        for i in range(count):
            start = time.perf_counter()
            service, base, _, symptom, _ = SCENARIOS[i % len(SCENARIOS)]
            version = base if i < (count * 3 // 4) else base + ".1"
            proposal = responder.propose(service, symptom, local_only=not model)
            if proposal["usage"]:
                token_counts.append(proposal["usage"]["total_tokens"])
            key = (service, version, skeptic.words(proposal["action"]))
            with transaction(True) as q:
                q("UPDATE services SET version=? WHERE name=?", (version, service))
                if protected:
                    first = commit_run(q, service, symptom, proposal, OFFLINE, uid(), origin="benchmark")
                    outcome = first["execution"]
                    if first["verdict"] == "Caution":
                        totals["supervised_retests"] += 1
                        trial = commit_run(q, service, symptom, proposal, OFFLINE, uid(), first["id"],
                                    {"mode":"approve", "reviewer":"Benchmark reviewer", "note":"Inspect canary."}, origin="benchmark")
                        outcome = trial["execution"]
                        first = trial
                    if first["verdict"] == "Blocked":
                        totals["blocks"] += 1
                    if first["scribe"].get("scar_id"):
                        review(q, first["scribe"]["scar_id"], "Benchmark reviewer", "confirmed", "Verified simulated log.")
                else:
                    outcome = executor.execute(service, version, proposal["action"], "Proceed")
                totals["target_calls"] += int(outcome["ran"])
                if outcome["state"] == "failed":
                    totals["failed_executions"] += 1
                    totals["repeated_failures"] += int(key in known_failures)
                    known_failures.add(key)
                if outcome["state"] != "succeeded":
                    recovery = {"action": "Apply recovery runbook", "source": "scenario policy", "usage": None}
                    if protected:
                        second = commit_run(q, service, symptom, recovery, OFFLINE, uid(), first["id"], origin="benchmark")
                        outcome = second["execution"]
                    else:
                        outcome = executor.execute(service, version, recovery["action"], "Proceed")
                    totals["target_calls"] += int(outcome["ran"])
                totals["passed_health_checks"] += int(outcome["state"] == "succeeded")
            delays.append(1000 * (time.perf_counter() - start))
    totals.update(repeated_mistake_rate=totals["repeated_failures"]/count,
                  simulated_health_pass_rate=totals["passed_health_checks"]/count,
                  mean_local_ms=round(statistics.mean(delays), 3), model_usage_reports=len(token_counts),
                  reported_tokens=sum(token_counts) if token_counts else None,
                  tokens_per_incident=sum(token_counts)/count if len(token_counts) == count else None)
    return totals


def benchmark(count=40, model=False, repetitions=1):
    if count < 5:
        raise ValueError("Use at least five incidents.")
    if model and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("--model requires OPENAI_API_KEY.")
    if not 1 <= repetitions <= 100:
        raise ValueError("Use between one and 100 repetitions.")
    pairs = []
    for i in range(repetitions):
        pair = {}
        for protected in ((False,True) if i % 2 == 0 else (True,False)):
            pair["scarline" if protected else "baseline"] = arm(protected, count, model)
        pairs.append(pair)
    return {"workload":"Five simulated services, cold start, original version for the first 75%, then upgrade.",
            "proposal_mode":"independent OpenAI calls" if model else "fixed scenario policy",
            "repetitions":repetitions, "baseline":pairs[0]["baseline"], "scarline":pairs[0]["scarline"],
            "pairs":pairs, "limits":"Synthetic outcomes; local processing latency; no Meko retrieval quality measurement; no token savings claim."}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--incidents", type=int, default=40)
    p.add_argument("--model", action="store_true", help="Make one metered OpenAI proposal per incident per arm.")
    p.add_argument("--runs", type=int, default=10)
    args = p.parse_args()
    print(json.dumps(benchmark(args.incidents, args.model, args.runs), indent=2))

