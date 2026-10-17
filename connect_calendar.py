"""
connect_calendar.py
-------------------
One-step CLI tool to connect Google Calendar to Cortex using the existing Phase 5 OAuth flow.

Usage:
    python connect_calendar.py
"""

import sys
import os
import asyncio
import webbrowser
import logging
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Ensure cortex root is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from hub.integrations.calendar import CalendarIntegration, _get_client_id, _get_client_secret
from hub.world_state import CalendarSignalAdapter, WorldStateManager
import hub.database as database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cortex.connect_calendar")

REDIRECT_URI = "http://localhost:8000/integrations/callback"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    auth_state = None
    error = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/integrations/callback":
            qs = parse_qs(parsed.query)
            if "code" in qs:
                OAuthCallbackHandler.auth_code = qs["code"][0]
                OAuthCallbackHandler.auth_state = qs.get("state", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = """
                <html>
                <body style="font-family: sans-serif; text-align: center; padding: 50px; background: #0f172a; color: #f8fafc;">
                    <h1 style="color: #38bdf8;">✓ Google Calendar Connected to Cortex</h1>
                    <p>Authorization successful! You can now close this tab and return to the terminal.</p>
                </body>
                </html>
                """
                self.wfile.write(html.encode("utf-8"))
            elif "error" in qs:
                OAuthCallbackHandler.error = qs["error"][0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"OAuth error: {OAuthCallbackHandler.error}".encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


async def main():
    print("=" * 65)
    print(" CORTEX GOOGLE CALENDAR OAUTH CONNECTOR ")
    print("=" * 65)

    client_id = _get_client_id()
    client_secret = _get_client_secret()

    if not client_id or not client_secret:
        print("\n[!] GOOGLE OAUTH CREDENTIALS MISSING IN .env\n")
        print("To connect Google Calendar, please follow these steps:")
        print("1. Open Google Cloud Console: https://console.cloud.google.com/")
        print("2. Enable the 'Google Calendar API' in your project.")
        print("3. Go to 'APIs & Services' > 'Credentials' > 'Create Credentials' > 'OAuth client ID'.")
        print("4. Application type: 'Web application'")
        print("5. Name: 'Cortex Desktop'")
        print(f"6. Authorized redirect URIs: {REDIRECT_URI}")
        print("7. Copy the Client ID and Client Secret.")
        print("8. Add them to your local .env file:")
        print("   GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com")
        print("   GOOGLE_CLIENT_SECRET=your_client_secret")
        print("\nOnce saved, run this script again: python connect_calendar.py\n")
        return

    print("[*] Found GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.")
    plugin = CalendarIntegration()
    account_id = "primary"

    url_info = await plugin.get_authorization_url(account_id, REDIRECT_URI)
    auth_url = url_info["url"]
    expected_state = url_info["state"]
    code_verifier = url_info["code_verifier"]

    # Start loopback callback server
    server = HTTPServer(("127.0.0.1", 8000), OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    print(f"\n[*] Starting local listener on port 8000 for callback...")
    print(f"[*] Opening browser to Google OAuth consent screen:")
    print(f"    {auth_url}\n")
    webbrowser.open(auth_url)

    print("[*] Waiting for authorization callback in browser (Ctrl+C to abort)...")
    while OAuthCallbackHandler.auth_code is None and OAuthCallbackHandler.error is None:
        await asyncio.sleep(0.5)

    if OAuthCallbackHandler.error:
        print(f"[x] Authorization failed: {OAuthCallbackHandler.error}")
        return

    code = OAuthCallbackHandler.auth_code
    print(f"[*] Authorization code received! Exchanging for tokens...")

    # Initialize production database connection
    await database.init_db()

    success = await plugin.exchange_code(
        account_id=account_id,
        code=code,
        redirect_uri=REDIRECT_URI,
        metadata={"code_verifier": code_verifier}
    )

    if not success:
        print("[x] Failed to exchange authorization code for tokens.")
        return

    print("[✓] OAuth tokens stored securely in OS Keyring!")
    print(f"[✓] Integration 'calendar_{account_id}' registered in Cortex database.")

    # Validate live connection
    status_info = await CalendarSignalAdapter.get_connection_status(account_id)
    print("\n=== CALENDAR CONNECTION STATUS ===")
    print(f"Connected:  {status_info.get('connected')}")
    print(f"Status:     {status_info.get('status')}")
    print(f"Account ID: {status_info.get('account_id')}")

    # Fetch real live events from Google Calendar API
    print("\n[*] Fetching live events from Google Calendar API...")
    try:
        res = await plugin.execute_action(account_id, "calendar.read")
        items = res.get("items", [])
        print(f"[✓] Successfully retrieved {len(items)} events from Google Calendar!")
        
        parsed = CalendarSignalAdapter.parse_events(items)
        cur = parsed.get("current_event")
        nxt = parsed.get("next_event")
        print("\n=== LIVE PARSED EVENTS ===")
        print(f"Current Event: {cur.get('title') if cur else 'None'}")
        print(f"Next Event:    {nxt.get('title') if nxt else 'None'} ({nxt.get('start') if nxt else 'N/A'})")
        print(f"Time Until:    {parsed.get('time_until_next_event') or 'N/A'}")
        print(f"Busy Status:   {parsed.get('busy')}")
        print(f"Free Window:   {parsed.get('free_busy_window')}")
    except Exception as e:
        print(f"[!] Warning fetching live events: {e}")

    print("\n" + "=" * 65)
    print(" GOOGLE CALENDAR IS NOW FULLY CONNECTED TO CORTEX ")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
