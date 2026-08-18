"""
Singleton wrapper around MistralAnonChat + VibeCode.
Loaded once at startup from environment variables.
"""
from __future__ import annotations

import os
import sys
import threading

# Add parent dir so we can import mistral_anon_chat
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from mistral_anon_chat import MistralAnonChat, VibeCode

_client: MistralAnonChat | None = None
_vibe: VibeCode | None = None
_lock = threading.Lock()


def _credentials() -> tuple[str, str]:
    return os.getenv("MISTRAL_EMAIL", "").strip(), os.getenv("MISTRAL_PASSWORD", "")


def _authenticate(client: MistralAnonChat) -> None:
    """Give the client a session token: the configured one, or a fresh login.

    MISTRAL_SESSION_TOKEN is an Ory session token and it EXPIRES. Until now it
    was read once at startup and never revisited, so the day it expired every
    request would 401 for as long as the container kept running -- the failure
    stays invisible from inside (the process is healthy, it just cannot talk to
    Mistral) and only a manual restart with a freshly pasted token fixed it.

    So the token is now optional: when MISTRAL_EMAIL and MISTRAL_PASSWORD are
    configured, the client logs in on its own. A deployment holding the
    credentials recovers by itself; one holding only a token behaves exactly as
    before.
    """
    token = os.getenv("MISTRAL_SESSION_TOKEN", "").strip()
    if token:
        client.session_token = token
        client.session.headers["Authorization"] = f"Bearer {token}"
        return
    email, password = _credentials()
    if email and password:
        fresh = client.login(email, password)
        os.environ["MISTRAL_SESSION_TOKEN"] = fresh


def get_client() -> MistralAnonChat:
    global _client
    with _lock:
        if _client is None:
            client = MistralAnonChat(debug=os.getenv("DEBUG", "").lower() in ("1", "true"))
            _authenticate(client)
            client.bootstrap_session()
            _client = client
    return _client


def relogin() -> bool:
    """Log in again with the configured credentials and swap in the new token.

    Called when an upstream call comes back 401/403, i.e. the session token
    expired or was revoked mid-flight. Returns False when no credentials are
    configured, so the caller can surface the original error instead of
    pretending it recovered.

    The stale token is cleared FIRST: leaving it in the environment would make
    `_authenticate` prefer it again and the retry would fail the same way.
    """
    email, password = _credentials()
    if not (email and password):
        return False
    with _lock:
        os.environ.pop("MISTRAL_SESSION_TOKEN", None)
        client = MistralAnonChat(debug=os.getenv("DEBUG", "").lower() in ("1", "true"))
        token = client.login(email, password)
        os.environ["MISTRAL_SESSION_TOKEN"] = token
        client.bootstrap_session()
        globals()["_client"] = client
        globals()["_vibe"] = None
    return True


def get_vibe() -> VibeCode:
    global _vibe
    if _vibe is None:
        _vibe = VibeCode(get_client())
    return _vibe


def reset_client():
    """Force re-init (e.g. after login)."""
    global _client, _vibe
    with _lock:
        _client = None
        _vibe = None
