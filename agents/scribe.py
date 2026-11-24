"""Close a decision with its simulated outcome and evidence."""
import meko
from store import record, uid


def close(q, service, action, outcome, trace_id, cloud):
    from store import service_version
    version = service_version(q, service)
    if outcome["state"] == "failed":
        scar_id = record(q, service, action, outcome["detail"], "Simulation log: " + outcome["detail"],
                         "Executor", trace_id=trace_id, cloud=cloud)
        return {"scar_id": scar_id, "detail": f"Recorded pending scar {scar_id[:8]} for independent review."}
    if outcome["ran"]:
        if cloud:
            meko.enqueue(q, trace_id, uid(), "memory_add", {
                "text": f"Scarline simulated successful remedy for {service} v{version}. Action {action}. {outcome['detail']}",
                "run_id": trace_id, "metadata": {"app": "scarline", "kind": "successful_retest" if outcome.get("supervised") else "success",
                                                 "service": service, "version": version, "trace_id": trace_id}
            })
        return {"detail": "Saved the successful simulated health check in this trace."}
    return {"detail": "Saved the decision and its evidence. No execution outcome was observed."}

