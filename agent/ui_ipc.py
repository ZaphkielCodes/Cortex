import asyncio
import json
import os
import logging
import socket
import threading

log = logging.getLogger("cortex.ui_ipc")

class UIIPCServer:
    """Runs in the Agent process (asyncio)."""
    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self.server = None
        self.clients = []
        self.on_message_callback = None
    
    async def start(self):
        self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        addr = self.server.sockets[0].getsockname()
        self.port = addr[1]
        log.info(f"UI IPC Server listening on {addr}")
        
    async def _handle_client(self, reader, writer):
        self.clients.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                    if self.on_message_callback:
                        await self.on_message_callback(msg)
                except Exception as e:
                    log.error(f"UI IPC bad message: {e}")
        except ConnectionResetError:
            pass
        finally:
            if writer in self.clients:
                self.clients.remove(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
            except Exception as e:
                log.warning(f"Error waiting for writer to close: {e}")
            
    async def stop(self):
        """Stop server and close all client connections."""
        for c in list(self.clients):
            try:
                c.close()
                await c.wait_closed()
            except Exception:
                pass
        self.clients.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        data = line.encode()
        for c in list(self.clients):
            try:
                c.write(data)
            except Exception as e:
                log.warning(f"Failed to send IPC message to UI: {e}")

import time
from pathlib import Path

class UIIPCClient(threading.Thread):
    """Runs in the UI process (thread). Auto-reconnects when Agent restarts."""
    def __init__(self, port=None, on_message=None, auto_reconnect=True):
        super().__init__(daemon=True)
        self.port = port
        self.on_message = on_message
        self.auto_reconnect = auto_reconnect
        self.sock = None
        self.running = False
        self.connected = False
        self._lock = threading.Lock()
        
    def _discover_port(self) -> int | None:
        port_file = Path(os.environ.get("CORTEX_STATE_DIR", str(Path.home() / ".cortex"))) / ".ipc_port"
        if port_file.exists():
            try:
                content = port_file.read_text().strip()
                if content:
                    return int(content)
            except Exception:
                pass
        return None

    def run(self):
        self.running = True
        while self.running:
            target_port = self.port or self._discover_port()
            if not target_port:
                if not self.auto_reconnect:
                    break
                time.sleep(1.0)
                continue

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect(("127.0.0.1", target_port))
                sock.settimeout(None)
                with self._lock:
                    self.sock = sock
                    self.connected = True
                
                f = sock.makefile('r', encoding='utf-8')
                while self.running:
                    line = f.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line.strip())
                        if self.on_message:
                            self.on_message(msg)
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                with self._lock:
                    self.connected = False
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None

            if not self.auto_reconnect:
                break
            time.sleep(1.0)
            
    def send(self, msg: dict):
        with self._lock:
            if self.sock and self.connected:
                try:
                    line = json.dumps(msg) + "\n"
                    self.sock.sendall(line.encode())
                except Exception:
                    self.connected = False
                
    def stop(self):
        self.running = False
        with self._lock:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
