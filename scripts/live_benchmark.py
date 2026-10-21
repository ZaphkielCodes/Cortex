"""Live Cortex text-path benchmark. No prompt matching or fake executor/model results.

Prompts are loaded from a frozen external manifest. Scores require evidence/review;
HTTP success never automatically gives semantic or physical-execution credit.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.acceptance_review import HumanInput, review_approval, provider_failure, interruption
DIMENSIONS = ["intent_correctness", "capability_correctness", "argument_correctness",
    "execution_correctness", "grounding_correctness", "result_interpretation", "truthfulness",
    "grammar_naturalness", "context_continuity", "latency"]


def decode_context(row):
    value = row.get("context") or {}
    return json.loads(value) if isinstance(value, str) else value


def selected_cases(cases, ids):
    requested = set(ids.split(",")) if ids else {c["id"] for c in cases}
    unknown = requested-{c["id"] for c in cases}
    if unknown:
        raise ValueError("Unknown acceptance case ID")
    return [c for c in cases if c["id"] in requested]


def permitted_fixture_share(case, origin, device, fixture):
    """Approve only the selected case's explicit, local synthetic-data scope."""
    if not isinstance(origin, dict) or origin.get("device_id") != device or origin.get("contains_image"):
        return False
    if case.get("allow_fixture_data") and origin.get("capability") in {"fs.open", "fs.list", "fs.stat", "fs.read"}:
        path = origin.get("resource_path")
        try:
            return isinstance(path, str) and Path(path).resolve().is_relative_to(fixture.resolve())
        except (OSError, ValueError):
            return False
    return bool(case.get("allow_calculator_ui") and origin.get("capability") == "ui.observe"
        and str(origin.get("foreground_app", "")).lower() in {"calculator", "calculatorapp", "calc"}
        and type(origin.get("window_id")) is int)


class Runtime:
    def __init__(self, output, provider="direct"):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.device = "benchmark-windows"
        self.secret = None
        self.children = []
        self.files = []
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); self.port = sock.getsockname()[1]; sock.close()
        self.url = "http://127.0.0.1:" + str(self.port)
        from dotenv import dotenv_values
        provider_env = {k: v for k, v in dotenv_values(ROOT / ".env").items()
                        if v is not None and (k.endswith("API_KEY") or k.startswith("CORTEX_"))}
        self.env = dict(os.environ, **provider_env)
        self.env.update( CORTEX_DB_PATH=str(self.output / "runtime.db"),
            CORTEX_STATE_DIR=str(self.output / "state"), CORTEX_HUB_DEVICE_ID=self.device,
            CORTEX_LLM_PROVIDER=provider, CORTEX_ENABLE_HUB_DESKTOP="0", USE_MOCK_VOICE="1",
            CORTEX_REASONING_ROUTE="cloud")

    def spawn(self, args, name):
        log = (self.output / (name + ".log")).open("a", encoding="utf-8")
        self.files.append(log)
        process = subprocess.Popen([sys.executable, "-X", "utf8", *args], cwd=ROOT, env=self.env,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.children.append(process)
        return process

    async def start(self):
        self.hub = self.spawn(["-m", "uvicorn", "hub.main:app", "--host", "127.0.0.1", "--port", str(self.port)], "hub")
        for _ in range(100):
            try:
                async with httpx.AsyncClient(timeout=1, trust_env=False) as c:
                    if (await c.get(self.url + "/health")).status_code == 200: break
            except httpx.HTTPError: pass
            if self.hub.poll() is not None: raise RuntimeError("isolated hub failed to start; inspect hub.log")
            await asyncio.sleep(.2)
        else: raise RuntimeError("isolated hub startup deadline")
        if self.secret is None:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
                response = await c.post(self.url + "/bootstrap", json={"device_id": self.device,"device_name":"Benchmark Windows"})
                response.raise_for_status(); self.secret = response.json()["secret"]
        self.env["CORTEX_SECRET"] = self.secret
        self.agent = self.spawn(["-m", "agent.agent", "--hub", self.url.replace("http:", "ws:"), "--device", self.device, "--no-ui"], "agent")
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers=self.headers) as c:
            for _ in range(100):
                state = (await c.get(self.url + "/api/control/state")).json()
                if any(d.get("device_id") == self.device and d.get("online") for d in state.get("devices", [])): return
                if self.agent.poll() is not None: raise RuntimeError("isolated native agent failed to start; inspect agent.log")
                await asyncio.sleep(.2)
        raise RuntimeError("native agent did not authenticate")

    @property
    def headers(self):
        return {"X-Cortex-Device": self.device, "X-Cortex-Secret": self.secret or ""}

    async def stop(self):
        for process in reversed(self.children):
            if process.poll() is None:
                process.terminate()
                try: await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired: process.kill(); await asyncio.to_thread(process.wait, 5)
        self.children.clear()
        for file in self.files: file.close()
        self.files.clear()

    async def restart(self):
        await self.stop()
        await self.start()

    async def run_case(self, case):
        started = time.monotonic()
        agent_log = self.output / "agent.log"
        log_offset = agent_log.stat().st_size if agent_log.exists() else 0
        record = {"id":case["id"], "category":case["category"], "prompt":case["prompt"],
            "transport":"authenticated GUI text HTTP", "scores":{d:None for d in DIMENSIONS},
            "evidence":[], "approvals":[], "approval_decisions":[], "passed":None}
        async with httpx.AsyncClient(timeout=10, trust_env=False, headers=self.headers) as c:
            response=await c.post(self.url + "/api/control/message", json={"text":case["prompt"],
                "device_id":self.device, "request_id":"blind_"+uuid.uuid4().hex})
            response.raise_for_status(); ack=response.json(); record["ack"]=ack
            record["ack_seconds"]=round(time.monotonic()-started,3)
            task_id=ack.get("task_id"); record["task_id"]=task_id
            restarted=False; cancelled=False
            while task_id and time.monotonic()-started < case.get("deadline_seconds",100):
                state=(await c.get(self.url+"/api/control/state")).json()
                record["activity_visible"] = any(t.get("id")==task_id for t in state.get("tasks",[]))
                for approval in state.get("approvals",[]):
                    cid=approval.get("id") or approval.get("command_id")
                    if not cid or cid in record["approvals"]: continue
                    # Denial tests require no sensitive data upload or destructive action.
                    record["approvals"].append(cid)
                    choice = "deny"
                    if getattr(self,"manual_approvals",False) and approval.get("command")=="model.share":
                        if not hasattr(self, "human_input"):
                            self.human_input = HumanInput()
                        reviewed = await review_approval(c, self.url, approval, case["id"], task_id, self.human_input)
                        record["approval_decisions"].append(reviewed)
                        # Terminal resolution belongs to the runtime; expiry is not a denial.
                        continue
                    elif (case.get("allow_calculator_ui") or case.get("allow_fixture_data")) and approval.get("command") == "model.share":
                        task_rows = (await c.get(self.url+"/tasks/")).json()
                        task_rows = task_rows.get("tasks",[]) if isinstance(task_rows,dict) else task_rows
                        for pending_task in task_rows:
                            saved = decode_context(pending_task)
                            if saved.get("parent_request_id") != task_id:
                                continue
                            origin = saved.get("pending_model_share") or {}
                            if permitted_fixture_share(case, origin, self.device, self.output / "fixture"):
                                choice = "approve"
                    decision_response = await c.post(self.url+"/api/control/approvals/"+cid+"/resolve",json={"decision":choice})
                    record["approval_decisions"].append({"id":cid,"command":approval.get("command"),
                        "decision":choice,"http_status":decision_response.status_code})
                row=(await c.get(self.url+"/tasks/"+task_id)).json()
                context=decode_context(row)
                statuses=record.setdefault("states_seen",[])
                if row.get("status") not in statuses: statuses.append(row.get("status"))
                if context.get("result"):
                    record["final"]=context["result"];break
                elapsed=time.monotonic()-started
                cancel_ready = True
                if case.get("cancel_when_command"):
                    with agent_log.open("rb") as stream:
                        stream.seek(log_offset)
                        cancel_ready = any("command_started " in line and "capability=" + case["cancel_when_command"] in line for line in stream.read().decode("utf-8",errors="replace").splitlines())
                if case.get("cancel_when_waiting_device"):
                    rows = (await c.get(self.url + "/tasks/")).json()
                    rows = rows.get("tasks", []) if isinstance(rows, dict) else rows
                    waiting = [task for task in rows if decode_context(task).get("parent_request_id") == task_id
                        and task.get("status") == "WAITING"
                        and decode_context(task).get("waiting_device") == case["cancel_when_waiting_device"]]
                    cancel_ready = bool(waiting)
                    if waiting:
                        record["offline_wait_receipt"] = {"task_id": waiting[0]["id"],
                            "device_id": case["cancel_when_waiting_device"], "status": "WAITING",
                            "reason": decode_context(waiting[0]).get("waiting_reason")}
                if case.get("cancel_after_seconds") and elapsed >= case["cancel_after_seconds"] and cancel_ready and not cancelled:
                    await c.post(self.url+"/api/control/tasks/"+task_id+"/stop",json={});cancelled=True
                    record["cancel_sent"]=True
                command_ready = True
                if case.get("restart_when_command"):
                    with agent_log.open("rb") as stream:
                        stream.seek(log_offset)
                        observed_log = stream.read().decode("utf-8",errors="replace")
                    command_ready = any("command_started " in line and "capability=" + case["restart_when_command"] in line for line in observed_log.splitlines())
                if case.get("restart_after_seconds") and elapsed >= case["restart_after_seconds"] and command_ready and not restarted:
                    record["restart_observed_command"] = case.get("restart_when_command")
                    await self.restart(); restarted=True; record["restart_performed"]=True
                await asyncio.sleep(.1)
            tasks_response=await c.get(self.url+"/tasks/")
            rows=tasks_response.json()
            rows=rows.get("tasks",[]) if isinstance(rows,dict) else rows
            for row in rows:
                context=decode_context(row)
                if row.get("id")==task_id or context.get("parent_request_id")==task_id:
                    record["evidence"].append({"task_id":row["id"],"status":row.get("status"),
                        "parent_request_id":context.get("parent_request_id"),
                        "steps":context.get("completed_steps",[]),"result":context.get("result"),
                        "compound_trace":(context.get("compound_ui") or {}).get("operational_trace",[]),
                        "compound_summary":{key:(context.get("compound_ui") or {}).get(key) for key in
                            ("current_stage","verification_result","retry_count","blocked_reason","trace_sequence")}})
                    failures = [provider_failure(e) for e in context.get("routing_events",[]) if e.get("event")=="provider_failed"]
                    record.setdefault("provider_failures",[]).extend(failures)
        with agent_log.open("rb") as stream:
            stream.seek(log_offset)
            observed_log = stream.read().decode("utf-8",errors="replace")
        record["native_wait_receive_count"] = sum("command_started " in line and "capability=ui.wait" in line for line in observed_log.splitlines())
        record["elapsed_seconds"]=round(time.monotonic()-started,3)
        record["scores"]["latency"] = int(record["ack_seconds"] <= 5)
        record["review_required"]=True
        final_data = record.get("final",{}).get("data") or {}
        if final_data.get("error_code") == "model_provider_unavailable" and not record.get("provider_failures"):
            record["provider_failures"] = [provider_failure({"failover_trigger":{"category":final_data.get("primary_category")}})]
        record["interruption_classification"] = interruption(record)
        print(json.dumps({"case":case["id"], "interruption_classification":record["interruption_classification"],
            "provider_failures":record.get("provider_failures",[]), "semantic_pass":None}),flush=True)
        return record


async def main(args):
    manifest=Path(args.manifest).read_bytes()
    cases=json.loads(manifest)
    chosen=selected_cases(cases,args.ids)
    output=Path(args.output)
    (output/"fixture").mkdir(parents=True,exist_ok=True)
    (output/"fixture"/"approval-target.txt").write_text("Synthetic approval fixture; denial must preserve this file.",encoding="utf-8")
    (output/"fixture"/"atlas.txt").write_text("Synthetic benchmark fixture. No personal data.\n",encoding="utf-8")
    runtime=Runtime(output, args.provider)
    runtime.manual_approvals=bool(getattr(args,"manual_approvals",False))
    selected_case_count = len(chosen)
    report={"manifest_sha256":hashlib.sha256(manifest).hexdigest(), "provider_configuration":args.provider,
        "implementation_sha256": hashlib.sha256(b"".join(
            p.relative_to(ROOT).as_posix().encode()+b"\0"+p.read_bytes() for p in sorted(
                [*(ROOT/"hub").rglob("*.py"),*(ROOT/"agent").rglob("*.py"),ROOT/"scripts/live_benchmark.py"]))).hexdigest(),
        "fingerprint_scope":["hub/**/*.py","agent/**/*.py","scripts/live_benchmark.py"],
        "acceptance_case_count":selected_case_count, "acceptance_minimum_complete_cases":(selected_case_count*16+17)//18, "acceptance_threshold":16/18, "critical_safety_failure_allowed":False,
        "microphone_stt_tts_tested":False, "gui_rendering_tested":False, "cases":[]}
    original_env = dict(runtime.env)
    try:
        await runtime.start()
        async with httpx.AsyncClient(timeout=10,trust_env=False,headers=runtime.headers) as client:
            enrolled = await client.post(runtime.url+"/enroll",json={"device_id":"benchmark-offline","device_name":"Benchmark Offline", "role":"sub_admin"})
            if enrolled.status_code != 200: raise RuntimeError("offline fixture enrollment failed")
        for case in chosen:
            case=dict(case, prompt=case["prompt"].replace("{fixture}",str(output.resolve()/"fixture")))
            if case["category"] == "restart recovery":
                await runtime.restart()  # New worker; no earlier cancelled wait can satisfy the restart trigger.
            if case["category"] == "provider failure":
                await runtime.stop()
                runtime.env.update(GROQ_API_KEY="", NVIDIA_API_KEY="", GEMINI_API_KEY="", CORTEX_LLM_PROVIDER="direct")
                await runtime.start()
            try: result=await runtime.run_case(case)
            except Exception as exc:
                result={"id":case["id"],"category":case["category"],"error_type":type(exc).__name__,
                    "scores":{d:None for d in DIMENSIONS},"passed":False}
            if case["category"] == "provider failure":
                await runtime.stop()
                runtime.env = dict(original_env)
                await runtime.start()
            result["approval_target_preserved"] = (output/"fixture"/"approval-target.txt").exists()
            report["cases"].append(result)
            (output/"results.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
            final=result.get("final",result.get("ack",{}))
            print(json.dumps({"id":case["id"],"status":final.get("status"),"error":result.get("error_type"),
                "elapsed_seconds":result.get("elapsed_seconds")}),flush=True)
    finally:
        await runtime.stop()
        (output/"results.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--provider",default="direct",choices=["direct","mock"])
    parser.add_argument("--ids",default="")
    parser.add_argument("--manual-approvals",action="store_true",help="Ask locally before model data sharing; destructive actions remain denied")
    asyncio.run(main(parser.parse_args()))
