"""
agent/daemon.py
---------------
Cross-platform abstraction for launching the Cortex agent in the background.
"""

import os
import platform
import subprocess
import sys

def run_in_background(args: list[str]) -> None:
    """Spawns the agent as a background process and detaches from the current terminal."""
    system = platform.system()
    agent_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.py")
    
    cmd = []
    
    if system == "Windows":
        # Use pythonw.exe to suppress the console window
        executable = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(executable):
            executable = sys.executable  # fallback
        
        cmd = [executable, agent_script] + args
        print(f"Starting background agent on Windows: {' '.join(cmd)}")
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        # POSIX systems (Linux, macOS) - fork and detach
        cmd = [sys.executable, agent_script] + args
        print(f"Starting background agent on POSIX: {' '.join(cmd)}")
        
        if os.fork() > 0:
            return  # Parent exits
        
        # Child process becomes session leader
        os.setsid()
        
        # Fork again to prevent acquiring a controlling terminal
        if os.fork() > 0:
            sys.exit(0)
            
        # Redirect standard file descriptors
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, sys.stdin.fileno())
        os.dup2(devnull, sys.stdout.fileno())
        os.dup2(devnull, sys.stderr.fileno())
        
        os.execv(sys.executable, cmd)
