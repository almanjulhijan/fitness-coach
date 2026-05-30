import json
import os
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Event
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

TOKENS_FILE = ".tokens.json"
REDIRECT_URI = "http://localhost:8765/callback"
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"


class _CallbackHandler(BaseHTTPRequestHandler):
    code = None  # type: Optional[str]
    got_code = Event()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            if "code" in params:
                _CallbackHandler.code = params["code"][0]
                body = b"<html><body><h1>Authorization successful!</h1><p>You can close this tab and return to the terminal.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                _CallbackHandler.got_code.set()
            else:
                error = params.get("error", ["unknown"])[0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write("Authorization failed: {}".format(error).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress server logs


def _load_tokens():
    # type: () -> Optional[dict]
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return None


def _save_tokens(tokens):
    # type: (dict) -> None
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def _refresh_tokens(client_id, client_secret, refresh_token):
    # type: (str, str, str) -> dict
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=30)
    if not resp.ok:
        print("Strava token error {}: {}".format(resp.status_code, resp.text))
    resp.raise_for_status()
    tokens = resp.json()
    _save_tokens(tokens)
    return tokens


def _run_oauth_flow(client_id, client_secret):
    # type: (str, str) -> dict
    auth_url = (
        "{base}?client_id={cid}&response_type=code"
        "&redirect_uri={redir}&approval_prompt=force&scope=activity:read_all,read"
    ).format(base=STRAVA_AUTH_URL, cid=client_id, redir=REDIRECT_URI)

    _CallbackHandler.code = None
    _CallbackHandler.got_code.clear()

    server = HTTPServer(("localhost", 8765), _CallbackHandler)
    thread = Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    print("\nOpening Strava authorization in your browser...")
    webbrowser.open(auth_url)
    print("If the browser didn't open, visit:\n  {}\n".format(auth_url))
    print("Waiting for authorization (timeout: 2 minutes)...")

    _CallbackHandler.got_code.wait(timeout=120)
    server.shutdown()

    if not _CallbackHandler.code:
        raise RuntimeError("Authorization timed out or was denied. Please try again.")

    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _CallbackHandler.code,
        "grant_type": "authorization_code",
    }, timeout=30)
    resp.raise_for_status()
    tokens = resp.json()
    _save_tokens(tokens)
    print("Authorization successful! Tokens saved.\n")
    return tokens


def get_valid_token(client_id, client_secret):
    # type: (str, str) -> dict

    # On Railway (or any server), STRAVA_REFRESH_TOKEN env var skips the OAuth browser flow.
    # We always do a fresh token refresh on startup so we don't rely on file persistence.
    env_refresh_token = os.getenv("STRAVA_REFRESH_TOKEN", "").strip()
    if env_refresh_token:
        print("Using STRAVA_REFRESH_TOKEN from environment...")
        return _refresh_tokens(client_id, client_secret, env_refresh_token)

    # Local development: use token file with auto-refresh
    tokens = _load_tokens()

    if not tokens:
        return _run_oauth_flow(client_id, client_secret)

    if tokens.get("expires_at", 0) <= time.time() + 60:
        print("Refreshing Strava access token...")
        return _refresh_tokens(client_id, client_secret, tokens["refresh_token"])

    return tokens
