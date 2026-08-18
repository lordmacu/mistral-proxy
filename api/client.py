"""
Singleton wrapper around MistralAnonChat + VibeCode.
Loaded once at startup from environment variables.
"""
from __future__ import annotations

import os
import sys

# Add parent dir so we can import mistral_anon_chat
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from mistral_anon_chat import MistralAnonChat, VibeCode

_client: MistralAnonChat | None = None
_vibe: VibeCode | None = None


def get_client() -> MistralAnonChat:
    global _client
    if _client is None:
        _client = MistralAnonChat(debug=os.getenv("DEBUG", "").lower() in ("1", "true"))
        token = os.getenv("MISTRAL_SESSION_TOKEN", "")
        if token:
            _client.session_token = token
            _client.session.headers["Authorization"] = f"Bearer {token}"
        _client.bootstrap_session()
    return _client


def get_vibe() -> VibeCode:
    global _vibe
    if _vibe is None:
        _vibe = VibeCode(get_client())
    return _vibe


def reset_client():
    """Force re-init (e.g. after login)."""
    global _client, _vibe
    _client = None
    _vibe = None
