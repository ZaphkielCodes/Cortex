"""Bounded desktop-to-hub transport. Credentials stay in Python, never JavaScript."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
import httpx


class DesktopControlTransport:
    def __init__(self, runtime, ipc, emit):
        self.runtime, self.ipc, self.emit = runtime, ipc, emit
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cortex-controls")
        self.runtime_lock = threading.Lock()

    def submit(self, raw):
        try:
            if len(raw) > 65536:
                raise ValueError("Request is too large.")
            request = json.loads(raw)
            if not isinstance(request, dict) or not isinstance(request.get("id"), str):
                raise ValueError("Invalid request.")
        except (ValueError, TypeError):
            return
        self.pool.submit(self._reply, request)

    def _reply(self, request):
        response = {"id": request["id"]}
        try:
            response.update(ok=True, data=self.perform(request.get("method", "GET"),
                request.get("path", ""), request.get("body")))
        except Exception as error:
            # Never return HTTP request objects, headers or secrets across WebChannel.
            response.update(ok=False, error=str(error) if isinstance(error, ValueError) else
                            "Could not reach Cortex. Check the hub connection and try again.")
        self.emit(json.dumps(response))

    def runtime_state(self):
        runtime = self.runtime
        return {"core_url": runtime.core_url, "device_id": runtime.device_id,
                "hub": runtime.hub_state, "agent": runtime.agent_state, "voice": runtime.voice_state,
                "model": runtime.model_name, "provider": runtime.provider_name,
                "managed_hub": bool(runtime.hub_proc and runtime.hub_proc.poll() is None),
                "managed_agent": bool(runtime.agent_proc and runtime.agent_proc.poll() is None),
                "ipc_connected": bool(self.ipc.connected)}

    def perform(self, method, path, body=None):
        method = str(method).upper()
        if path == "/desktop/state" and method == "GET":
            return self.runtime_state()
        if path == "/desktop/voice" and method == "POST":
            action = (body or {}).get("action")
            if action not in ("push_to_talk", "mute", "unmute"):
                raise ValueError("Unknown voice control.")
            if not self.ipc.connected:
                raise ValueError("The device agent is disconnected. Start it before using voice.")
            self.ipc.send({"type": "action", "action": action})
            if not self.ipc.connected:
                raise ValueError("The voice command could not be delivered.")
            return {"status": "sent", "action": action}
        if path == "/desktop/runtime" and method == "POST":
            action = (body or {}).get("action")
            operations = {"start": self.runtime.start_cortex, "stop": self.runtime.stop_cortex,
                          "restart_agent": self.runtime.restart_agent, "restart_hub": self.runtime.restart_hub}
            if action not in operations:
                raise ValueError("Unknown runtime control.")
            state = self.runtime_state()
            if action == "restart_agent" and not state["managed_agent"]:
                raise ValueError("This agent is managed by another terminal. Restart it there.")
            if action == "restart_hub" and not state["managed_hub"]:
                raise ValueError("This hub is managed by another terminal. Restart it there.")
            if action == "stop" and not (state["managed_agent"] or state["managed_hub"]):
                raise ValueError("No processes are managed by this window.")
            if not self.runtime_lock.acquire(blocking=False):
                raise ValueError("A runtime operation is already in progress.")
            try:
                if not operations[action]():
                    raise ValueError("Cortex could not complete that runtime operation. Check Activity.")
            finally:
                self.runtime_lock.release()
            return self.runtime_state()

        allowed = {
            ("GET", "/api/control/state"), ("POST", "/api/control/message"),
            ("POST", "/api/control/autonomy"), ("POST", "/api/control/tasks/pause_all"),
            ("GET", "/memory/"), ("POST", "/memory/"), ("GET", "/tasks/"),
        }
        patterns = {"GET": [r"/tasks/[A-Za-z0-9_-]{1,128}"],
                    "PUT": [r"/memory/[A-Za-z0-9_-]{1,128}"],
                    "POST": [r"/tasks/[A-Za-z0-9_-]{1,128}/(?:pause|resume)",
                             r"/api/control/tasks/[A-Za-z0-9_-]{1,128}/stop",
                             r"/api/control/approvals/[A-Za-z0-9_-]{1,128}/resolve",
                             r"/api/control/transfers/[A-Za-z0-9_-]{1,128}/(?:cancel|resume)"]}
        if (method, path) not in allowed and not any(re.fullmatch(p, path) for p in patterns.get(method, [])):
            raise ValueError("This desktop operation is not supported.")
        secret = self.runtime.get_device_secret()
        if not secret:
            raise ValueError("No enrolled device credential is available. Enroll this device before connecting.")
        if path.startswith("/api/control/approvals/"):
            # Use the authenticated resolver once, with the actual caller identity.
            path = path.replace("/api/control/approvals/", "/pending/", 1)
        if path == "/api/control/message":
            body = dict(body or {}, device_id=self.runtime.device_id)
        with httpx.Client(timeout=8.0, follow_redirects=False) as client:
            response = client.request(method, self.runtime.core_url + path, json=body,
                headers={"X-Cortex-Device": self.runtime.device_id, "X-Cortex-Secret": secret})
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
                detail = detail if isinstance(detail, str) else "The hub rejected this operation."
            except Exception:
                detail = "The hub rejected this operation."
            raise ValueError(f"{detail} (HTTP {response.status_code})")
        return response.json()

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
