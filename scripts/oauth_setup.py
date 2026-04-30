"""One-time tastytrade OAuth2 setup.

Walks you through registering an app at https://developer.tastytrade.com/
and exchanging an authorization code for a long-lived refresh token. Run
this on your laptop once; copy the resulting values into your DigitalOcean
droplet's environment.

Usage:
    python scripts/oauth_setup.py

You will be prompted for the values from your tastytrade developer page.
At the end, this script prints the three secrets to stdout and exits.

If any of the URLs below have changed in the tastytrade developer portal,
copy the new ones from there — the parameter names are stable.
"""
from __future__ import annotations

import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from urllib.request import Request, urlopen

AUTHORIZE_URL = "https://my.tastytrade.com/auth.html"
TOKEN_URL = "https://api.tastytrade.com/oauth/token"
DEFAULT_REDIRECT = "http://localhost:8765/callback"


def _capture_code(redirect_port: int, expected_state: str) -> str:
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = dict(urllib.parse.parse_qsl(parsed.query))
            captured.update(params)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>You can close this tab.</h1>")

        def log_message(self, *args, **kwargs):
            pass

    server = http.server.HTTPServer(("127.0.0.1", redirect_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Listening on http://127.0.0.1:{redirect_port} for the OAuth callback...")
    while "code" not in captured and "error" not in captured:
        pass
    server.shutdown()
    if "error" in captured:
        raise SystemExit(f"OAuth error: {captured['error']}")
    if captured.get("state") != expected_state:
        raise SystemExit("OAuth state mismatch — possible CSRF, aborting.")
    return captured["code"]


def main() -> int:
    print("\n=== Tastytrade OAuth2 Setup ===\n")
    print("Step 1: Register an OAuth app at https://developer.tastytrade.com/")
    print("        Set the Redirect URI to:  " + DEFAULT_REDIRECT)
    print("        Save the Client ID and Client Secret it gives you.\n")

    client_id = input("Client ID:     ").strip()
    client_secret = input("Client Secret: ").strip()
    if not client_id or not client_secret:
        print("Both fields are required.", file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    auth_params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": DEFAULT_REDIRECT,
        "response_type": "code",
        "scope": "read trade openid",
        "state": state,
    })
    auth_url = f"{AUTHORIZE_URL}?{auth_params}"

    print("\nStep 2: Authorize this app in your browser...")
    print(f"        Opening: {auth_url}\n")
    webbrowser.open(auth_url)

    code = _capture_code(8765, state)
    print("Got authorization code, exchanging for tokens...\n")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": DEFAULT_REDIRECT,
    }).encode()
    req = Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req) as resp:
        import json
        token_data = json.loads(resp.read().decode())

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print("No refresh_token in response. Full response:", token_data)
        return 1

    account_id = input(
        "Step 3: Enter your tastytrade account number\n"
        "        (visible at the top of https://trade.tastytrade.com/index.html)\n"
        "Account ID: "
    ).strip()

    print("\n=== Done — copy these into your droplet's environment ===\n")
    print(f"TASTYTRADE_CLIENT_SECRET={client_secret}")
    print(f"TASTYTRADE_REFRESH_TOKEN={refresh_token}")
    print(f"TASTYTRADE_ACCOUNT_ID={account_id}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
