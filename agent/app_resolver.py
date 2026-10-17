import os
import shutil
import platform
import subprocess
import json

def resolve_installed_app(app_name: str) -> dict | None:
    if platform.system() != "Windows":
        return None

    app_name_lower = app_name.lower().strip()

    # 1. Known alias map
    aliases = {
        "calculator": "calc",
        "notepad": "notepad",
        "paint": "mspaint",
        "cmd": "cmd",
        "command prompt": "cmd",
        "powershell": "powershell",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
    }
    if app_name_lower == "notepad":
        return {
            "display_name": "Notepad",
            "launch_type": "app_user_model_id",
            "launch_target": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
            "source": "alias_map",
        }

    if app_name_lower in aliases:
        target = aliases[app_name_lower]
        return {
            "display_name": app_name,
            "launch_type": "executable",
            "launch_target": target,
            "source": "alias_map",
        }

    # 2. Get-StartApps (covers Start Menu, UWP, PWAs, Store apps)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json"],
            capture_output=True, text=True, creationflags=0x08000000
        )
        if result.returncode == 0:
            apps = json.loads(result.stdout)
            
            # Exact match first
            for app in apps:
                name = app.get("Name", "")
                if app_name_lower == name.lower():
                    return {
                        "display_name": name,
                        "launch_type": "app_user_model_id",
                        "launch_target": app.get("AppID"),
                        "source": "Get-StartApps",
                    }
            # Prefix/Substring match
            for app in apps:
                name = app.get("Name", "")
                if name.lower().startswith(app_name_lower) or app_name_lower in name.lower():
                    return {
                        "display_name": name,
                        "launch_type": "app_user_model_id",
                        "launch_target": app.get("AppID"),
                        "source": "Get-StartApps",
                    }
    except Exception as e:
        print(f"Error in Get-StartApps: {e}")

    # 3. Registry App Paths
    import winreg
    for hkey in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths", 0, winreg.KEY_READ) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, i)
                    if subkey_name.lower().startswith(app_name_lower):
                        try:
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                target, _ = winreg.QueryValueEx(subkey, "")
                                return {
                                    "display_name": subkey_name,
                                    "launch_type": "executable",
                                    "launch_target": target,
                                    "source": "registry_app_paths",
                                }
                        except OSError:
                            pass
        except OSError:
            pass

    # 4. PATH resolution
    path_target = shutil.which(app_name_lower)
    if path_target:
        return {
            "display_name": app_name,
            "launch_type": "executable",
            "launch_target": path_target,
            "source": "PATH",
        }

    return None

if __name__ == "__main__":
    for app in ["Calculator", "Notepad", "YouTube", "Spotify", "definitely_not_a_real_app"]:
        print(f"--- {app} ---")
        res = resolve_installed_app(app)
        print(res)
