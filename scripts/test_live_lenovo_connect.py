"""
scripts/test_live_lenovo_connect.py
Tests that an extracted bundle can actually connect, authenticate, advertise capabilities,
and send a heartbeat to the running Hub over WebSocket.
"""
import asyncio
import json
import subprocess
import sys
import time
import tempfile
import urllib.request
import zipfile
import shutil
from pathlib import Path

def test_live_connection():
    url = "http://127.0.0.1:8000/download/agent"
    tmp_dir = Path(tempfile.mkdtemp(prefix="cortex_live_test_"))
    zip_path = tmp_dir / "agent.zip"
    urllib.request.urlretrieve(url, zip_path)

    extract_dir = tmp_dir / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_dir)

    print(f"Testing live agent connection from {extract_dir} to Hub...")

    # Start the agent as a subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent.agent", "--hub", "ws://127.0.0.1:8000", "--device", "lenovo", "--no-ui"],
        cwd=str(extract_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:
        # Give it 4 seconds to connect and authenticate
        time.sleep(4)

        # Query Hub to see if lenovo is registered and online
        secret = (Path.home() / ".cortex" / "device_secret").read_text().strip()
        req = urllib.request.Request(
            "http://127.0.0.1:8000/devices",
            headers={"X-Cortex-Device": "asus", "X-Cortex-Secret": secret}
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            devices = data.get("devices", [])
            lenovo = next((d for d in devices if d.get("device_id") == "lenovo"), None)

            print("Lenovo device entry in Hub:")
            print("  Online:", lenovo.get("online") if lenovo else None)
            print("  Role:", lenovo.get("role") if lenovo else None)
            print("  Capabilities count:", len(lenovo.get("capabilities", [])) if lenovo else 0)
            print("  Capabilities:", lenovo.get("capabilities", []) if lenovo else [])

            out, err = "", ""
            try:
                proc.terminate()
                out, err = proc.communicate(timeout=2)
            except Exception:
                proc.kill()
                out, err = proc.communicate()
            print("Subprocess STDOUT:\n", out)
            print("Subprocess STDERR:\n", err)

            assert lenovo is not None, "Lenovo not found in Hub devices!"
            assert lenovo.get("online") is True, f"Lenovo is not online in Hub! STDOUT: {out} STDERR: {err}"
            assert len(lenovo.get("capabilities", [])) >= 7, "Lenovo did not advertise required capabilities!"
            print(">>> LIVE WEBSOCKET AUTH & CAPABILITY ADVERTISEMENT VERIFIED! <<<")


    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    test_live_connection()
