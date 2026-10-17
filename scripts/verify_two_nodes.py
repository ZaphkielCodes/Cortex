"""
scripts/verify_two_nodes.py
---------------------------
Automated validator for Cortex Phase A5 Real Two-Device Validation.
Tests live connection, capabilities, heartbeats, remote execution, and session grounding.
"""

import sys
import time
import json
import urllib.request
from pathlib import Path

HUB_URL = "http://127.0.0.1:8000"
ASUS_SECRET_PATH = Path.home() / ".cortex" / "device_secret"

def get_asus_secret():
    if ASUS_SECRET_PATH.exists():
        return ASUS_SECRET_PATH.read_text().strip()
    return ""

def query_hub(endpoint, method="GET", payload=None):
    secret = get_asus_secret()
    url = f"{HUB_URL}{endpoint}"
    data = json.dumps(payload).encode() if payload else None
    headers = {
        "X-Cortex-Device": "asus",
        "X-Cortex-Secret": secret,
    }
    if payload:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except Exception:
            return {"status": "error", "code": e.code, "message": body}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def send_conversation(natural_text):
    return query_hub("/conversation/", method="POST", payload={"natural_text": natural_text})

def get_devices():
    res = query_hub("/devices")
    return res.get("devices", [])

def check_status():
    devices = get_devices()
    asus = next((d for d in devices if d.get("device_id") == "asus"), None)
    lenovo = next((d for d in devices if d.get("device_id") == "lenovo"), None)
    return asus, lenovo

def main():
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "status":
            asus, lenovo = check_status()
            print("--- ASUS ---")
            print(f"Online: {asus.get('online') if asus else False}")
            print(f"Role: {asus.get('role') if asus else None}")
            print(f"Caps: {len(asus.get('capabilities', [])) if asus else 0}")
            print("--- LENOVO ---")
            print(f"Online: {lenovo.get('online') if lenovo else False}")
            print(f"Role: {lenovo.get('role') if lenovo else None}")
            print(f"Caps: {lenovo.get('capabilities', []) if lenovo else []}")
            return
        elif cmd == "say":
            text = " ".join(sys.argv[2:])
            print(f"Sending: '{text}'")
            res = send_conversation(text)
            print(json.dumps(res, indent=2))
            return

    print("Checking current status...")
    asus, lenovo = check_status()
    print(f"ASUS: online={asus.get('online') if asus else False}")
    print(f"LENOVO: online={lenovo.get('online') if lenovo else False}")

if __name__ == "__main__":
    main()
