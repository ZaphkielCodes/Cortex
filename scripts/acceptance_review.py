"""Local human review and safe metadata-only acceptance diagnostics."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import queue
import sys
import threading
import time


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def remaining_seconds(row):
    """Server expiry metadata is authoritative; a missing deadline is unknown."""
    if not row.get("created_at") or not row.get("expires_at") or not row.get("server_now"):
        return None
    return max(0., (datetime.fromisoformat(row["expires_at"])-datetime.fromisoformat(row["server_now"])).total_seconds())


class HumanInput:
    """One daemon reader: an expired approval cannot strand executor shutdown."""
    def __init__(self):
        self.answers = queue.Queue()
        def read():
            while True:
                line = sys.stdin.readline()
                self.answers.put((time.monotonic(), None if not line else line.strip().lower()))
                if not line:
                    return
        threading.Thread(target=read, daemon=True).start()

    def poll(self):
        try:
            return self.answers.get_nowait()
        except queue.Empty:
            return None


def approval_outcome(row):
    if row.get("runtime_outcome"):
        return row["runtime_outcome"]
    return {"approved":"APPROVAL_GRANTED", "denied":"APPROVAL_DENIED",
            "expired":"APPROVAL_EXPIRED", "cancelled":"APPROVAL_CANCELLED"}.get(row.get("status"))


async def review_approval(client, url, approval, case_id, parent_task_id, human, emit=print):
    cid = approval.get("command_id") or approval.get("id")
    surfaced_mono = time.monotonic()
    result = {"id":cid, "case":case_id, "task_id":parent_task_id,
              "surfaced_at":None, "created_at":approval.get("created_at"),
              "expires_at":approval.get("expires_at"), "human_decision_at":None,
              "remaining_seconds_when_decided":None, "outcome":None}
    row = approval
    announced = False
    last_countdown = None
    submitted = False
    while True:
        response = await client.get(url+"/api/control/approvals/"+cid)
        response.raise_for_status()
        row = response.json()
        for key in ("created_at", "expires_at"):
            result[key] = row.get(key) or result[key]
        outcome = approval_outcome(row)
        if outcome:
            result["outcome"] = outcome
            emit(json.dumps(result), flush=True)
            return result
        remaining = remaining_seconds(row)
        if not announced and remaining is not None:
            result["surfaced_at"] = utcnow()
            emit("\n=== HUMAN APPROVAL REQUIRED — ordinary case progression paused ===", flush=True)
            emit(json.dumps({"case":case_id,"task_id":parent_task_id,
                **{key:row.get(key) for key in ("recipient","model","data_class","approval_reason","created_at","expires_at")}}), flush=True)
            emit("Type y = approve, n = deny, c = cancel this task. No answer grants permission.", flush=True)
            announced = True
        if remaining is not None and int(remaining) != last_countdown:
            last_countdown = int(remaining)
            emit(f"Approval remaining: {remaining:.1f}s (runtime expires_at)", flush=True)
        answer = human.poll() if announced and not submitted and remaining is not None and remaining > 0 else None
        if answer is not None:
            read_at, choice = answer
            if read_at < surfaced_mono and choice is not None:
                continue  # A late answer from an earlier approval is never reused.
            if choice not in ("y", "yes", "n", "no", "c", "cancel", None):
                emit("Choose y, n, or c.", flush=True)
            elif remaining is not None and remaining > 0:
                received = datetime.now(timezone.utc)-timedelta(seconds=max(0.,time.monotonic()-read_at))
                result["human_decision_at"] = received.isoformat()
                result["remaining_seconds_when_decided"] = max(0.,(datetime.fromisoformat(row["expires_at"])-received).total_seconds())
                result["decision"] = choice or "cancel"
                if choice in ("c", "cancel", None):
                    decision = await client.post(url+"/api/control/tasks/"+parent_task_id+"/stop", json={})
                else:
                    decision = await client.post(url+"/api/control/approvals/"+cid+"/resolve",
                        json={"decision":"approve" if choice in ("y","yes") else "deny"})
                result["http_status"] = decision.status_code
                submitted = decision.status_code == 200
        # At zero, wait for the runtime's recorded expiry; never submit a default denial.
        await asyncio.sleep(.1)


def provider_failure(event):
    status = event.get("http_status")
    failure = event.get("failover_trigger") or {}
    kind, category = failure.get("kind"), failure.get("category")
    if status == 429: label = "HTTP_429"
    elif status == 400 and kind == "UNSUPPORTED_TOOL_USE": label = "HTTP_400_TOOL_USE_FAILED"
    elif status == 503: label = "HTTP_503"
    elif status == 504: label = "HTTP_504"
    elif category == "CONTEXT_TOO_LARGE" or status == 413: label = "CONTEXT_TOO_LARGE"
    elif kind == "TIMEOUT": label = "TIMEOUT"
    elif category == "PROVIDER_COOLDOWN": label = "COOLDOWN"
    else: label = category or kind or "UNCLASSIFIED_PROVIDER_ERROR"
    return {"classification":label, "provider":event.get("selected_provider"),
            "model":event.get("selected_model"), "http_status":status,
            "kind":kind, "category":category, "latency_seconds":event.get("latency_seconds")}


def interruption(record):
    outcomes = [r.get("outcome") for r in record.get("approval_decisions",[])]
    for outcome in ("APPROVAL_CANCELLED","APPROVAL_EXPIRED","APPROVAL_DENIED"):
        if outcome in outcomes:
            return outcome
    final = record.get("final",{})
    code = (final.get("data") or {}).get("error_code")
    if code in ("model_provider_unavailable", "invalid_model_decision"):
        return "PROVIDER_MODEL_INTERRUPTION"
    if record.get("final",{}).get("status") not in (None,"success"):
        return "SEMANTIC_TASK_FAILURE"
    return "SEMANTIC_REVIEW_REQUIRED"  # Runtime success is never semantic acceptance.
