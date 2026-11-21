"""
YouTube Data API authentication helpers.

This module owns everything related to connecting a Google account to the
clipper:

* Google OAuth 2.0 (installed / "web application" flow for a localhost app)
* Local token storage (never sent to the browser, never logged)
* Connected-channel identity lookup for the YouTube Data API v3
* Disconnect (removes the stored token)

Design notes
------------
* The Google client libraries are imported *lazily* inside functions so the
  rest of the clipper keeps working (clip generation, preview, download) even
  when the optional YouTube dependencies are not installed.
* Only the two scopes the upload feature actually needs are requested:
    - youtube.upload   -> upload videos
    - youtube.readonly -> read the connected channel's name for the UI
* Credentials live in a git-ignored ``credentials/`` folder next to this file.
* Nothing here ever logs tokens or client secrets.
"""

import json
import os
import threading

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

CRED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials")
CLIENT_SECRET_FILE = os.path.join(CRED_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(CRED_DIR, "token.json")

# The minimum set of scopes the upload feature needs.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# The OAuth redirect URI. The server binds to 127.0.0.1, so this must match the
# "Authorized redirect URI" registered in the Google Cloud OAuth client.
REDIRECT_URI = "http://127.0.0.1:5000/youtube/oauth2callback"

# In-memory store of in-flight OAuth flows keyed by their anti-CSRF state token.
# Lives only for the lifetime of the process; consumed once on callback.
_PENDING_FLOWS = {}

# Serialises reads/writes of the token file across request threads.
_TOKEN_LOCK = threading.Lock()


class YouTubeAuthError(Exception):
    """Raised for user-facing YouTube authentication problems.

    ``args[0]`` is always a short, safe machine code (never a secret).
    """


# ---------------------------------------------------------------------------
# Lazy Google imports
# ---------------------------------------------------------------------------

def _import_google():
    """Import the Google client libraries lazily.

    Returns (Flow, Credentials, Request). Raises YouTubeAuthError("not_installed")
    when the optional dependencies are missing.
    """
    try:
        from google_auth_oauthlib.flow import Flow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except Exception as exc:  # ImportError or any transitive failure
        raise YouTubeAuthError("not_installed") from exc
    return Flow, Credentials, Request


def _import_discovery():
    """Lazily import googleapiclient.discovery.build."""
    try:
        from googleapiclient.discovery import build
    except Exception as exc:
        raise YouTubeAuthError("not_installed") from exc
    return build


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def is_configured():
    """True when a client_secret.json file is present."""
    return os.path.isfile(CLIENT_SECRET_FILE)


def load_client_config():
    """Load and return the parsed client_secret.json contents."""
    if not is_configured():
        raise YouTubeAuthError("not_configured")
    try:
        with open(CLIENT_SECRET_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise YouTubeAuthError("bad_client_secret") from exc


def _ensure_cred_dir():
    try:
        os.makedirs(CRED_DIR, exist_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def save_credentials(creds):
    """Persist credentials to TOKEN_FILE (atomic-ish, locked)."""
    _ensure_cred_dir()
    payload = creds.to_json() if hasattr(creds, "to_json") else json.dumps(creds)
    with _TOKEN_LOCK:
        tmp = TOKEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, TOKEN_FILE)


def load_credentials():
    """Load stored credentials, refreshing the access token if needed.

    Returns a Credentials object, or None when not connected / not installed.
    """
    if not os.path.isfile(TOKEN_FILE):
        return None

    _, Credentials, Request = _import_google()

    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except (OSError, ValueError):
        # Corrupt or unreadable token file: treat as disconnected.
        return None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                save_credentials(creds)
            except Exception:
                # Refresh failed (revoked/expired offline). Treat as disconnected
                # so the UI prompts a fresh connect instead of erroring forever.
                return None
        else:
            return None

    return creds


def clear_credentials():
    """Disconnect: delete the stored token file."""
    with _TOKEN_LOCK:
        try:
            if os.path.isfile(TOKEN_FILE):
                os.remove(TOKEN_FILE)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def start_oauth_flow():
    """Begin the OAuth flow.

    Returns (authorization_url, state). The flow is stashed in-process so the
    callback can complete the token exchange.
    """
    Flow, _, _ = _import_google()
    if not is_configured():
        raise YouTubeAuthError("not_configured")

    client_config = load_client_config()

    try:
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )
        auth_url, state = flow.authorization_url(
            access_type="offline",       # request a refresh token
            include_granted_scopes="true",
            prompt="consent",            # force consent so we always get a refresh token
        )
    except Exception as exc:
        raise YouTubeAuthError("flow_init_failed") from exc

    if not state:
        raise YouTubeAuthError("flow_init_failed")

    _PENDING_FLOWS[state] = flow
    return auth_url, state


def complete_oauth_flow(state, code):
    """Complete the OAuth flow using the callback's state + authorization code."""
    if not state or not code:
        raise YouTubeAuthError("invalid_callback")

    flow = _PENDING_FLOWS.pop(state, None)
    if flow is None:
        # Unknown/expired state (e.g. server restarted, or CSRF attempt).
        raise YouTubeAuthError("invalid_state")

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise YouTubeAuthError("token_exchange_failed") from exc

    creds = getattr(flow, "credentials", None)
    if not creds or not getattr(creds, "token", None):
        raise YouTubeAuthError("token_exchange_failed")

    save_credentials(creds)
    return creds


# ---------------------------------------------------------------------------
# YouTube Data API
# ---------------------------------------------------------------------------

def build_youtube_service(credentials):
    """Build an authenticated YouTube Data API v3 client."""
    build = _import_discovery()
    try:
        return build("youtube", "v3", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise YouTubeAuthError("service_build_failed") from exc


def get_connected_channel(credentials):
    """Return {id, title, custom_url} for the authenticated user's channel."""
    youtube = build_youtube_service(credentials)
    try:
        response = youtube.channels().list(part="snippet", mine=True).execute()
    except Exception as exc:
        raise YouTubeAuthError("channel_lookup_failed") from exc

    items = (response or {}).get("items", [])
    if not items:
        return None
    snippet = items[0].get("snippet", {}) or {}
    return {
        "id": items[0].get("id"),
        "title": snippet.get("title"),
        "custom_url": snippet.get("customUrl"),
    }