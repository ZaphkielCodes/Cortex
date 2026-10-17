"""
scripts/smoke_test_bundle.py
----------------------------
Smoke tests the exact artifact downloaded from http://127.0.0.1:8000/download/agent:
1. Downloads bundle via HTTP
2. Extracts into a clean isolated temp directory
3. Runs py -m agent.agent --help
4. Tests headless secret resolution & required capability advertisement
"""
import urllib.request
import zipfile
import tempfile
import subprocess
import shutil
import sys
from pathlib import Path

def run_smoke_test():
    url = "http://127.0.0.1:8000/download/agent"
    print(f"1. Downloading bundle from {url}...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="cortex_bundle_test_"))
    zip_path = tmp_dir / "downloaded_agent.zip"
    urllib.request.urlretrieve(url, zip_path)
    print(f"Downloaded {zip_path.stat().st_size} bytes.")

    # 2. Extract into clean directory
    extract_dir = tmp_dir / "cortex_extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_dir)
    print(f"2. Extracted to {extract_dir}. Total files: {len(list(extract_dir.rglob('*')))}")

    # 3. Test py -m agent.agent --help in extracted bundle
    print("3. Running: python -m agent.agent --help in extracted bundle...")
    res_help = subprocess.run(
        [sys.executable, "-m", "agent.agent", "--help"],
        cwd=str(extract_dir),
        capture_output=True,
        text=True
    )
    print("Help exit code:", res_help.returncode)
    print("Help STDOUT:\n", res_help.stdout)
    if res_help.stderr:
        print("Help STDERR:\n", res_help.stderr)
    assert res_help.returncode == 0, "python -m agent.agent --help failed in extracted bundle!"

    # 4. Headless startup smoke test
    print("4. Testing secret auto-provisioning and headless capability advertisement...")
    smoke_code = (
        "import asyncio\n"
        "from agent.agent import _load_secret\n"
        "from agent.executor import get_capabilities\n"
        "secret = _load_secret(None)\n"
        "print('Loaded secret length:', len(secret))\n"
        "caps = get_capabilities()\n"
        "print('Advertised capabilities count:', len(caps))\n"
        "required = ['app.list_running', 'app.launch', 'app.close', 'window.list', 'window.focus', 'fs.open', 'screen.capture']\n"
        "for r in required:\n"
        "    assert r in caps, f'Missing required capability {r}!'\n"
        "print('All required capabilities verified:', required)\n"
        "print('HEADLESS SMOKE TEST PASSED!')\n"
    )
    res_smoke = subprocess.run(
        [sys.executable, "-c", smoke_code],
        cwd=str(extract_dir),
        capture_output=True,
        text=True
    )
    print("Smoke exit code:", res_smoke.returncode)
    print("Smoke STDOUT:\n", res_smoke.stdout)
    if res_smoke.stderr:
        print("Smoke STDERR:\n", res_smoke.stderr)
    assert res_smoke.returncode == 0, "Headless smoke test failed in extracted bundle!"

    # 5. Clean up
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("5. Isolated temp directory cleaned up.")
    print(">>> BUNDLE SMOKE TEST PASSED SUCCESSFULLY! <<<")

if __name__ == "__main__":
    run_smoke_test()
