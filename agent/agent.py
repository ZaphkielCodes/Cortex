"""
agent/agent.py
--------------
Cortex Device Agent — lightweight WebSocket client (Phase 3).

What it does
------------
1. Loads the device secret from --secret flag, CORTEX_SECRET env var, or .cortex_secret file
2. Connects to the Cortex Hub at ws://<HUB_IP>:<PORT>/ws/<DEVICE_ID>
3. Authenticates via challenge/response (hub sends nonce, agent replies with secret)
4. Sends a periodic heartbeat ({"type": "status", "status": "online"}) every 30 s
5. Listens for CommandMessage JSON from the hub (now includes command_id)
6. Executes the command via executor.execute()
7. Sends back a ResultMessage JSON (echoing the command_id)
8. Reconnects automatically with exponential back-off if the connection drops

Usage
-----
    python agent.py --hub ws://192.168.1.10:8000 --device laptop-2 --secret <hex>

    # Or store the secret in a file:
    echo <hex> > .cortex_secret
    python agent.py --hub ws://192.168.1.10:8000 --device laptop-2

Arguments
---------
  --hub      Hub WebSocket base URL   (default: ws://localhost:8000)
  --device   Unique device identifier (default: hostname of this machine)
  --secret   Device secret for authentication (or use CORTEX_SECRET env var / .cortex_secret file)
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, List, Union, Callable, Awaitable
import uuid

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = ROOT / ".env"
if DOTENV_PATH.exists():
    load_dotenv(dotenv_path=DOTENV_PATH, override=False)

# only after dotenv is loaded:
from .voice.mock_providers import MockWakeWordProvider, MockSTTProvider, MockTTSProvider
from .voice.controller import VoiceController


try:
    import websockets
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
except ImportError:
    print("ERROR: 'websockets' not installed. Run: pip install websockets")
    sys.exit(1)

from .executor import execute, get_capabilities
import subprocess
from .ui_ipc import UIIPCServer

# ---------------------------------------------------------------------------
# Logging (with secret-scrubbing guard)
# ---------------------------------------------------------------------------

class _SecretScrubFilter(logging.Filter):
    """Defence-in-depth: strip any 64-char hex string from log messages.

    Device secrets are 32-byte hex (64 chars). Even if a developer
    accidentally passes the secret to log.debug(), this filter replaces
    it with '<REDACTED>'.  This is intentionally broad — false positives
    (other 64-char hex strings) are acceptable in a security context.
    """

    import re
    _HEX_64 = re.compile(r'[0-9a-fA-F]{64}')

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._HEX_64.sub('<REDACTED>', record.msg)
        # Also scrub args if they contain strings
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._HEX_64.sub('<REDACTED>', v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._HEX_64.sub('<REDACTED>', a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AGENT] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
log.addFilter(_SecretScrubFilter())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL = 30   # seconds between status pings
MAX_BACKOFF        = 60   # max seconds between reconnection attempts
INITIAL_BACKOFF    = 2    # seconds for first retry
AUTH_TIMEOUT       = 10   # seconds to wait for auth challenge

# Path to secret file (in user home directory to separate state from source)
LOCAL_STATE_DIR = Path(os.environ.get("CORTEX_STATE_DIR", str(Path.home() / ".cortex")))
LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
SECRET_FILE = LOCAL_STATE_DIR / "device_secret"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Secret loading
# ---------------------------------------------------------------------------
def _load_secret(cli_secret: str | None, device_id: str | None = None) -> str:
    """Load device secret with priority:
    1. Explicit CLI argument (--secret)
    2. Environment variable (CORTEX_SECRET)
    3. Existing enrolled credential in ~/.cortex/ (PROTECT EXISTING INSTALLATIONS — NEVER OVERWRITE)
    4. Device-bound one-time deployment credential (.cortex_enrollment.json)
    """
    # 1. CLI argument
    if cli_secret:
        return cli_secret.strip()

    # 2. Environment variable
    env_secret = os.environ.get("CORTEX_SECRET", "").strip()
    if env_secret:
        return env_secret

    # 3. Existing enrolled credential in ~/.cortex/ (PROTECT EXISTING CREDENTIALS)
    if device_id:
        dev_file = LOCAL_STATE_DIR / f"{device_id}_secret"
        if dev_file.exists():
            content = dev_file.read_text(encoding="utf-8").strip()
            if content:
                log.info(f"Loaded secret from existing {dev_file.name}")
                return content

    if SECRET_FILE.exists():
        content = SECRET_FILE.read_text(encoding="utf-8").strip()
        if content:
            log.info("Loaded secret from existing local credential file.")
            return content

    # 4. Device-bound one-time deployment credential (.cortex_enrollment.json)
    # Only evaluated if NO existing credential was found above!
    enrollment_candidates = [
        ROOT / ".cortex_enrollment.json",
    ]
    if device_id:
        enrollment_candidates.append(ROOT / f"{device_id}.enrollment.json")

    for cand in enrollment_candidates:
        if cand.exists():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                target_dev = (data.get("device_id") or "").strip()
                secret_val = (data.get("enrollment_secret") or data.get("secret") or "").strip()

                # Guard 1: Verify device binding
                if not target_dev or (device_id and target_dev.lower() != device_id.lower()):
                    log.warning(
                        f"Ignoring deployment credential in {cand.name}: intended for device "
                        f"'{target_dev}', but current agent is starting as '{device_id}'."
                    )
                    continue

                # Guard 2: Valid secret found for this device
                if secret_val:
                    # Write to local state directory (never overwriting another device)
                    SECRET_FILE.write_text(secret_val, encoding="utf-8")
                    if device_id:
                        (LOCAL_STATE_DIR / f"{device_id}_secret").write_text(secret_val, encoding="utf-8")

                    # If one-time deployment, remove the deployment file
                    if data.get("one_time", True):
                        try:
                            cand.unlink(missing_ok=True)
                        except Exception:
                            pass

                    log.info(f"Securely provisioned one-time deployment credential for device '{target_dev}'.")
                    return secret_val
            except Exception as exc:
                log.warning(f"Failed to process deployment credential {cand.name}: {exc}")

    print(
        "ERROR: No device secret provided.\n"
        "  Use --secret <hex>, set CORTEX_SECRET env var,\n"
        f"  or ensure {SECRET_FILE} exists with the secret."
    )
    sys.exit(1)





# ---------------------------------------------------------------------------
# Authentication handshake
# ---------------------------------------------------------------------------
async def _authenticate(ws, device_id: str, secret: str) -> bool:
    """
    Handle the hub's auth challenge.

    Flow:
      1. Hub sends {"type": "auth_challenge", "challenge": "<nonce>"}
      2. Agent replies {"type": "auth_response", "device_id": "...", "response": "<secret>"}
         (The secret is sent directly; the challenge proves freshness/prevents replay.)
      3. Hub replies {"type": "auth_result", "success": true/false}
    """
    try:
        # Wait for auth_challenge
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        data = json.loads(raw)

        if data.get("type") == "auth_result":
            if not data.get("success"):
                log.error(f"Hub rejected connection immediately: {data.get('message')}")
            else:
                log.error("Expected auth_challenge, but hub sent auth_result (success).")
            return False
        elif data.get("type") != "auth_challenge":
            log.error(f"Expected auth_challenge, got: {data.get('type')}")
            return False

        challenge = data.get("challenge", "")
        log.debug(f"Received auth challenge: {challenge[:16]}…")

        # Send auth response (secret directly — challenge is for freshness)
        auth_response = {
            "type": "auth_response",
            "device_id": device_id,
            "response": secret,
            "platform": platform.system(),
            "capabilities": get_capabilities(),
        }
        await ws.send(json.dumps(auth_response))

        # Wait for auth result
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
        result = json.loads(raw)

        if result.get("type") != "auth_result":
            log.error(f"Expected auth_result, got: {result.get('type')}")
            return False

        if result.get("success"):
            log.info("✓ Authentication successful")
            return True
        else:
            log.error(f"✗ Authentication failed: {result.get('message', '?')}")
            return False

    except asyncio.TimeoutError:
        log.error("Auth handshake timed out")
        return False
    except Exception as exc:
        log.error(f"Auth error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Heartbeat coroutine
# ---------------------------------------------------------------------------
async def _heartbeat(ws, device_id: str, stop: asyncio.Event) -> None:
    """Send a status:online ping every HEARTBEAT_INTERVAL seconds."""
    while not stop.is_set():
        try:
            await ws.send(json.dumps({"type": "status", "status": "online"}))
            log.debug(f"Heartbeat sent ({device_id})")
        except Exception:
            break
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ---------------------------------------------------------------------------
# Main connection loop (single connection session)
# ---------------------------------------------------------------------------
async def _session(ws_url: str, hub_base: str, device_id: str, secret: str, stop: asyncio.Event, ui_server: UIIPCServer, voice, active_state: dict) -> None:
    """Run one connection session. Returns when the connection closes."""
    log.info(f"Connecting to hub: {ws_url}")
    
    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=30) as ws:
        active_state['ws'] = ws
        async def on_ui_msg(msg):
            mtype = msg.get("type")
            if mtype == "action":
                action = msg.get("action")
                if action == "push_to_talk":
                    voice.push_to_talk()
                elif action == "mute":
                    voice.mute()
                elif action == "unmute":
                    voice.unmute()
            elif mtype == "approval_response":
                resp = {
                    "type": "confirmation_response",
                    "command_id": msg.get("id"),
                    "decision": "approve" if msg.get("approved") else "deny"
                }
                await ws.send(json.dumps(resp))
                
        ui_server.on_message_callback = on_ui_msg
        
        def on_voice_state(state):
            ui_server.send({"type": "state", "state": state})

        # ── Authenticate ────────────────────────────────────────────
        if not await _authenticate(ws, device_id, secret):
            log.error("Authentication failed — will retry with backoff")
            log.info("Agent authenticated: False")
            ui_server.send({"type": "agent_status", "connected": True, "authenticated": False, "voice_running": False, "device_id": device_id})
            return
            
        log.info(f"Connected as '{device_id}' ✓")
        log.info("Agent authenticated: True")
        ui_server.send({"type": "agent_status", "connected": True, "authenticated": True, "voice_running": True, "device_id": device_id})
        
        # Start voice only after successful authentication
        voice.start()
        log.info(f"VoiceController running: {getattr(voice, '_already_running', False)}")
        
        # Phase 9F: Voice active short acknowledgment (without general LLM)
        asyncio.create_task(voice.speak_result("I'm awake."))

        # Send initial online status
        await ws.send(json.dumps({"type": "status", "status": "online"}))

        from .peer_worker import PeerWorker
        peer_worker = PeerWorker()
        # Start heartbeat in background
        hb_task = asyncio.create_task(_heartbeat(ws, device_id, stop))

        from .command_dispatcher import CommandDispatcher
        if "command_dispatcher" not in active_state:
            active_state["command_dispatcher"] = CommandDispatcher()
        dispatcher = active_state["command_dispatcher"]
        command_handlers = set()

        async def handle_command(data):
            command, command_id, args = data.get("command", ""), data.get("command_id", ""), data.get("args", [])
            latency = data.get("latency_metrics") or {}
            async def perform(pool):
                import time
                latency["executor_start"] = time.monotonic()
                log.info("command_started command_id=%s capability=%s",command_id,command)
                if command.startswith("transfer."):
                    return await peer_worker.execute(command, args)
                controls = {"voice.mute": voice.mute, "voice.unmute": voice.unmute, "voice.push_to_talk": voice.push_to_talk}
                if command in controls:
                    controls[command]()
                    return {"status": "success", "message": "Voice control applied."}
                return await asyncio.get_running_loop().run_in_executor(pool, execute, command, args, latency)
            result = await dispatcher.dispatch(command_id, command, args, perform)
            log.info("command_finished command_id=%s capability=%s status=%s",command_id,command,result.get("status"))
            await ws.send(json.dumps({"type": "result", "command_id": command_id, "command": command,
                "status": result["status"], "message": result["message"], "data": result.get("data"), "error": result.get("error")}))

        def command_done(job):
            command_handlers.discard(job)
            if not job.cancelled() and job.exception():
                log.warning("command_reply_failed reason=%s completion_unverified=true", type(job.exception()).__name__)

        try:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Normal — just check stop flag and loop
                    continue
                except (ConnectionClosedOK, ConnectionClosedError) as exc:
                    log.warning(f"Connection closed | code: {exc.code} | reason: {exc.reason} | STT active: {voice.state == 'listening'}")
                    break

                # ── Parse message ────────────────────────────────────────
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning(f"Bad JSON from hub: {exc}")
                    continue

                msg_type = data.get("type")

                if msg_type == "voice_response":
                    from .voice.response_router import route_voice_response
                    for spoken in route_voice_response(data, active_state):
                        asyncio.create_task(voice.speak_result(spoken))
                    continue

                if msg_type == "voice_response_partial":
                    log.debug(f"Ignoring voice_response_partial chunk: {data.get('chunk', '')[:40]}")
                    continue

                if msg_type != "command":
                    log.debug(f"Ignored message type: {msg_type}")
                    continue

                job = asyncio.create_task(handle_command(data))
                command_handlers.add(job)
                job.add_done_callback(command_done)

        finally:
            for job in list(command_handlers):
                job.cancel()
            await asyncio.gather(*command_handlers, return_exceptions=True)
            await peer_worker.close()
            log.info(f"Session finally block: stopping VoiceController (STT active: {voice.state == 'listening'})")
            voice.stop()
            ui_server.send({"type": "agent_status", "connected": False, "authenticated": False, "voice_running": False, "device_id": device_id})
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Reconnection wrapper with exponential back-off
# ---------------------------------------------------------------------------
async def run_agent(hub_base: str, device_id: str, secret: str, no_ui: bool = False) -> None:
    ws_url  = hub_base.rstrip("/") + f"/ws/{device_id}"
    stop    = asyncio.Event()
    backoff = INITIAL_BACKOFF

    # Graceful shutdown on Ctrl+C / SIGTERM
    loop = asyncio.get_running_loop()

    def _shutdown(sig=None):
        log.info("Shutdown requested — disconnecting…")
        stop.set()

    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGTERM, _shutdown)

    ui_server = UIIPCServer()
    await ui_server.start()

    ipc_port_file = str(Path(os.environ.get("CORTEX_STATE_DIR", str(Path.home() / ".cortex"))) / ".ipc_port")
    try:
        LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(ipc_port_file, "w") as f:
            f.write(str(ui_server.port))
        log.info(f"IPC port {ui_server.port} persisted to {ipc_port_file}")
    except Exception as e:
        log.warning(f"Could not persist IPC port: {e}")

    ui_proc = None
    active_state = {'ws': None, 'voice_futures': {}, 'active_cmd_id': None}

    async def on_partial_text(text: str) -> None:
        if not active_state['ws']: return
        if not active_state['active_cmd_id']:
            active_state['active_cmd_id'] = uuid.uuid4().hex
            
        req = {
            "type": "voice_partial",
            "text": text,
            "command_id": active_state['active_cmd_id']
        }
        try:
            await active_state['ws'].send(json.dumps(req))
        except:
            pass

    async def on_text_recognized(text: str, latency_metrics: Optional[dict] = None) -> str:
        log.info(f"Sending text to Hub via WS: {text}")
        if not active_state['ws']:
            return "Not connected to Hub."
        try:
            cmd_id = active_state.get('active_cmd_id') or uuid.uuid4().hex
            active_state['active_cmd_id'] = None # Reset for next utterance
            
            import time
            req_metrics = dict(latency_metrics or {})
            if "speech_end" not in req_metrics:
                req_metrics["speech_end"] = time.monotonic()
            
            req = {
                "type": "voice_request",
                "text": text,
                "command_id": cmd_id,
                "latency_metrics": req_metrics
            }
            
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            active_state['voice_futures'][cmd_id] = fut
            
            await active_state['ws'].send(json.dumps(req))
            
            # Wait for response (with timeout)
            try:
                result = await asyncio.wait_for(fut, timeout=float(os.getenv("CORTEX_HUB_REPLY_TIMEOUT", "15")))
            except asyncio.TimeoutError:
                active_state.setdefault('timed_out_cmd_ids', set()).add(cmd_id)
                raise
            finally:
                active_state['voice_futures'].pop(cmd_id, None)
                
            status = result.get("status")
            if status == "approval_required":
                plan = result.get("action_plan", {})
                ui_server.send({
                    "type": "approval_request",
                    "id": result.get("command_id"),
                    "action": plan.get("action", "Unknown Action"),
                    "device": plan.get("target_device", "Unknown Device"),
                    "summary": plan.get("user_facing_summary", "perform an action"),
                })
            
            msg = result.get("message")
            summary = result.get("summary")
            
            resp_latency = result.get("latency_metrics", {})
            active_state['last_latency_metrics'] = resp_latency
            if "classification" in resp_latency:
                log.info(f"Classification: {resp_latency['classification']}")
            if "stage_durations" in resp_latency:
                log.info(f"[Latency] Hub Response Trace: {resp_latency.get('trace_id')} | durations: {resp_latency['stage_durations']}")
                
            if status == "error":
                res_text = (msg or "").strip() or "An error occurred."
            else:
                res_text = (summary or msg or "").strip() or "I have processed your request."
            return res_text
        except asyncio.TimeoutError:
            log.error("Voice request timed out")
            return "I could not confirm the hub response. Check Activity before retrying; the request may still be running."
        except Exception as e:
            log.error(f"Failed to send text to Hub: {e}")
            return "I'm sorry, I couldn't reach the Hub."

    def on_audio_level(level):
        ui_server.send({"type": "audio_level", "level": level})
        
    def on_voice_state(state):
        ui_server.send({"type": "state", "state": state})

    async def on_sleep_command():
        supervisor_port = int(os.environ.get("CORTEX_SUPERVISOR_PORT", "8008"))
        log.info(f"Sleep command recognized. Requesting runtime sleep from supervisor on port {supervisor_port}...")
        import urllib.request
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{supervisor_port}/sleep",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=2.0)
        except Exception as exc:
            log.warning(f"Could not contact supervisor for sleep: {exc}")

    voice = None
    if not no_ui and os.environ.get("USE_MOCK_VOICE") != "1":
        try:
            from .voice.real_providers import LocalWakeWordProvider, VoskSTTProvider, PyTTSx3Provider
            log.info("Using REAL Voice Providers.")
            provider_name = os.environ.get("CORTEX_STT_PROVIDER", "groq").lower()
            fallback_stt = VoskSTTProvider()


            if provider_name == "deepgram":
                from .voice.deepgram_providers import DeepgramSTTProvider
                stt = DeepgramSTTProvider(fallback_provider=fallback_stt)
            elif provider_name == "groq":
                from .voice.groq_providers import GroqSTTProvider
                stt = GroqSTTProvider(fallback_provider=fallback_stt)
                log.info("Active STT provider: GroqSTTProvider")
            else:
                stt = fallback_stt

            from .voice.polly_providers import AmazonPollyTTSProvider
            log.info("Active TTS provider: AmazonPollyTTSProvider")

            voice = VoiceController(
                wakeword=LocalWakeWordProvider(),
                stt=stt,
                tts=AmazonPollyTTSProvider(fallback_provider=PyTTSx3Provider(on_audio_level=on_audio_level), on_audio_level=on_audio_level),
                on_text_recognized=on_text_recognized,
                on_partial_text=on_partial_text,
                on_state_change=on_voice_state,
                on_sleep_command=on_sleep_command,
                continuous_session=True,
                session_timeout_secs=10.0
            )
        except Exception as e:
            log.warning(f"Failed to initialize real voice providers: {e}. Falling back to mock.")
            voice = None

    if voice is None:
        log.info("Using MOCK Voice Providers (headless/worker mode).")
        voice = VoiceController(
            wakeword=MockWakeWordProvider(),
            stt=MockSTTProvider(),
            tts=MockTTSProvider(),
            on_text_recognized=on_text_recognized,
            on_state_change=on_voice_state,
            on_sleep_command=on_sleep_command,
            continuous_session=True,
            session_timeout_secs=10.0
        )


    try:
        while not stop.is_set():
            if not no_ui:
                if ui_proc is None or ui_proc.poll() is not None:
                    log.info(f"Spawning Cortex Desktop UI... (IPC Port: {ui_server.port})")
                    ui_proc = subprocess.Popen(
                        [sys.executable, "-m", "agent.ui.main", "--ipc-port", str(ui_server.port)],
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    )
                    log.info(f"UI process spawned (PID: {ui_proc.pid})")
                else:
                    log.info(f"Reconnecting to existing UI process (PID: {ui_proc.pid})")

            try:
                await _session(ws_url, hub_base, device_id, secret, stop, ui_server, voice, active_state)
                backoff = INITIAL_BACKOFF  # reset on clean session
            except (OSError, ConnectionRefusedError) as exc:
                if stop.is_set():
                    break
                log.warning(
                    f"Could not reach hub: {exc}. "
                    f"Retrying in {backoff}s…"
                )
            except Exception as exc:
                if stop.is_set():
                    break
                log.error(f"Unexpected error: {exc}. Retrying in {backoff}s…")

            if stop.is_set():
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    except KeyboardInterrupt:
        pass
    finally:
        if active_state.get("command_dispatcher"):
            active_state["command_dispatcher"].close()
        voice.stop()
        if ui_proc and ui_proc.poll() is None:
            log.info(f"Shutting down UI process (PID: {ui_proc.pid})")
            try:
                ui_proc.terminate()
            except Exception:
                pass

    log.info("Agent stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    default_device = platform.node()  # hostname as default device ID

    parser = argparse.ArgumentParser(description="Cortex Device Agent")
    parser.add_argument(
        "--hub",
        default="ws://localhost:8000",
        help="Hub WebSocket base URL (default: ws://localhost:8000)",
    )
    parser.add_argument(
        "--device",
        default=default_device,
        help=f"Unique device ID for this machine (default: hostname = '{default_device}')",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="Device secret for authentication (or use CORTEX_SECRET env var / .cortex_secret file)",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Run the agent in the background",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Do not spawn PyQt UI from agent",
    )
    args = parser.parse_args()

    if args.background:
        from daemon import run_in_background
        # Strip --background from args passed to the child
        child_args = [a for a in sys.argv[1:] if a != "--background"]
        run_in_background(child_args)
        sys.exit(0)

    secret = _load_secret(args.secret, device_id=args.device)

    # SECURITY: Never log or print the secret
    log.info(f"Cortex Agent starting — device_id={args.device}")
    asyncio.run(run_agent(args.hub, args.device, secret, no_ui=args.no_ui))


if __name__ == "__main__":
    main()
