import time
import platform
import psutil

def verify_windows_app_launch(resolved_app: dict, timeout: int = 5) -> dict:
    """
    Generic verification that an app successfully launched.
    Looks for a visible top-level window matching the display name or executable,
    or falls back to a background process check.
    """
    if platform.system() != "Windows":
        return {"status": "success", "message": f"Opened '{resolved_app['display_name']}'"}
        
    try:
        import win32gui
        import win32process
    except ImportError:
        return {"status": "success", "message": f"Opened '{resolved_app['display_name']}' (unverified)"}

    # Extract exe name for process matching
    exe_name = ""
    if resolved_app.get("launch_type") == "executable":
        exe_name = resolved_app.get("launch_target", "").split("\\")[-1].lower()
        if exe_name.endswith(".exe"):
            exe_name = exe_name[:-4]
            
    display_name = resolved_app.get("display_name", "").lower()
    
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        visible_windows = []
        
        def enum_windows_callback(hwnd, ctx):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).lower()
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    p = psutil.Process(pid)
                    p_name = p.name().lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    p_name = ""
                
                # Exclude standard Explorer windows that might just be a folder
                if p_name != "explorer.exe" or display_name not in title:
                    visible_windows.append((title, p_name))
        
        try:
            win32gui.EnumWindows(enum_windows_callback, None)
        except Exception:
            pass
            
        # 1. Prefer Confirmation of a visible top-level window
        for title, p_name in visible_windows:
            # Check if display name is in the window title
            if display_name and display_name in title:
                return {"status": "success", "message": f"Opened '{resolved_app['display_name']}'"}
            # Or if the exact executable is running a visible window
            if exe_name and exe_name in p_name:
                return {"status": "success", "message": f"Opened '{resolved_app['display_name']}'"}
                
        # 2. Check for background process
        for p in psutil.process_iter(['name']):
            try:
                p_name = p.info['name']
                if p_name and isinstance(p_name, str):
                    p_name = p_name.lower()
                    if exe_name and exe_name in p_name:
                        return {"status": "success", "message": f"Started '{resolved_app['display_name']}' (background process only)"}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
                
        time.sleep(0.5)
        
    return {"status": "failure", "message": f"Could not verify '{resolved_app['display_name']}' launched. No matching window or process appeared."}
