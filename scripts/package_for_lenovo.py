import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
DIST.mkdir(exist_ok=True)
ZIP_PATH = DIST / "cortex_lenovo.zip"

EXCLUDE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".gemini", "logs", "dist", "node_modules", ".pnpm-store", "vosk-model", "screenshots"}
EXCLUDE_EXTS = {".pyc", ".pyo"}

def should_include(rel_path: Path) -> bool:
    for part in rel_path.parts:
        if part in EXCLUDE_DIRS:
            return False
    if rel_path.suffix in EXCLUDE_EXTS:
        return False
    # Don't send production database or server secret to worker node
    if rel_path.parts and rel_path.parts[0] == "data" and rel_path.name == "cortex.db":
        return False
    return True

def create_package():
    print(f"Creating Lenovo agent package at {ZIP_PATH}...")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        # Walk and add relevant folders
        for folder in ["agent", "hub", "scripts"]:
            src_dir = ROOT / folder
            if not src_dir.exists():
                continue
            for root, _, files in os.walk(src_dir):
                for f in files:
                    p = Path(root) / f
                    rel = p.relative_to(ROOT)
                    if should_include(rel):
                        zf.write(p, rel)
        
        # Add root files (never include plaintext secrets)
        for f_name in ["requirements.txt", "pyproject.toml"]:
            p = ROOT / f_name
            if p.exists():
                zf.write(p, p.relative_to(ROOT))

        # Resolve Lenovo deployment secret from isolated storage (dist/.lenovo_secret)
        dist_secret_file = DIST / ".lenovo_secret"
        legacy_root_secret = ROOT / "lenovo_secret.txt"
        if legacy_root_secret.exists():
            dist_secret_file.write_text(legacy_root_secret.read_text(encoding="utf-8").strip(), encoding="utf-8")
            legacy_root_secret.unlink(missing_ok=True)

        if dist_secret_file.exists():
            lenovo_secret = dist_secret_file.read_text(encoding="utf-8").strip()
            import json
            enrollment_meta = {
                "device_id": "lenovo",
                "enrollment_secret": lenovo_secret,
                "one_time": True,
            }
            # Add device-bound deployment credential directly to zip without touching repo root
            zf.writestr(".cortex_enrollment.json", json.dumps(enrollment_meta, indent=2))

    print(f"Package created: {ZIP_PATH} ({ZIP_PATH.stat().st_size / 1024:.1f} KB)")


    # Write setup.ps1 into dist directory for one-line download on Lenovo
    setup_ps1 = DIST / "setup.ps1"
    setup_ps1.write_text("""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  CORTEX LENOVO AGENT - AUTOMATED SETUP   " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$hub_ip = "192.168.86.128"
$target_dir = "$HOME\\Cortex"

Write-Host "1. Downloading Cortex package from ASUS ($hub_ip)..." -ForegroundColor Yellow
Invoke-WebRequest -Uri "http://${hub_ip}:8000/download/agent" -OutFile "$HOME\\cortex_lenovo.zip"

Write-Host "2. Extracting to $target_dir..." -ForegroundColor Yellow
if (-not (Test-Path $target_dir)) {
    New-Item -ItemType Directory -Path $target_dir -Force | Out-Null
}
Expand-Archive -Path "$HOME\\cortex_lenovo.zip" -DestinationPath $target_dir -Force

Write-Host "3. Installing required dependencies (websockets, psutil, pillow, pyautogui)..." -ForegroundColor Yellow
py -m pip install websockets psutil pillow pyautogui

Write-Host "4. Starting Cortex Agent on Lenovo..." -ForegroundColor Green
Set-Location $target_dir
py -m agent.agent --hub ws://${hub_ip}:8000 --device lenovo --no-ui
""", encoding="utf-8")
    print(f"Setup script written to: {setup_ps1}")



if __name__ == "__main__":
    create_package()
