"""What this proxy can actually do right now.

Spec: the proxy capability contract, llm-libre
docs/superpowers/specs/2026-08-20-proxy-capability-contract-design.md

THE RULE: a boolean says what a request sent right now would ACHIEVE, not what
this codebase implements. Where the two differ, the endpoint is the liar and
this module is the correction (spec 3.2).

Where the rule STOPS: a boolean tracks entitlement, not the meter. A quota
running out is a 429 the gateway already handles with a cooldown and recovers
from on its own; it must never flip a capability off. The dividing line is
durability -- if a fresh request tomorrow would still be refused for the same
reason, it belongs in the boolean.

WHY THIS FILE LIVES IN `api/` AND NOT AT THE REPO ROOT: the Dockerfile lists
modules by name (`COPY mistral_anon_chat.py .`) rather than copying the tree, so
a new root-level module is NOT shipped and the container dies on import at boot.
That exact mistake took chatgpt-proxy down for ten minutes on 2026-08-20.
`COPY api/ ./api/` ships this directory whole, so here it is safe.

MISTRAL IS THE ODD ONE OF THE FIVE: it is the only proxy whose chat works with
no account at all. Measured, not assumed -- with every credential stripped from
the environment, `MistralAnonChat().chat(...)` answered. So `snapshot()`
distinguishes the two modes and `effective()` keeps `chat`/`streaming` true in
both, which no other proxy in the fleet does.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

REQUIRED_CAPABILITIES = (
    "chat", "streaming", "tools", "vision", "images",
    "audio_speech", "audio_transcription", "translate",
    "search", "files", "conversations",
)


@dataclass(frozen=True)
class SessionState:
    mode: str          # "account" | "anonymous"


def snapshot() -> SessionState:
    """Read local credentials. No lock, no cache, and above all NO VENDOR CALL.

    That last point is the reason this function exists at all. The old
    `/health` called `get_client()`, which on a cold container runs
    `_authenticate()` and `bootstrap_session()` -- two round trips to Mistral.
    A health endpoint that reaches the vendor cannot answer while the vendor is
    down, which is precisely when the gateway needs an answer (spec 3.1). This
    reads environment variables and nothing else.

    Either credential shape counts as an account: `MISTRAL_SESSION_TOKEN` on its
    own, or `MISTRAL_EMAIL` + `MISTRAL_PASSWORD`, which is what `_authenticate`
    itself accepts. A present-but-expired token still reads as "account",
    deliberately: expiry is recoverable -- `relogin()` swaps in a fresh one on a
    401 -- and the contract must not flip capabilities off for a condition that
    heals itself.
    """
    token = (os.getenv("MISTRAL_SESSION_TOKEN") or "").strip()
    email = (os.getenv("MISTRAL_EMAIL") or "").strip()
    password = (os.getenv("MISTRAL_PASSWORD") or "").strip()
    account = bool(token) or bool(email and password)
    return SessionState(mode="account" if account else "anonymous")


def auth_block(state: SessionState) -> dict:
    """The contract's informational `auth` block.

    `plan` is None rather than a guess. Mistral does sell Pro, and this proxy
    even has a `/v1/billing/pro` route, but calling it is a vendor round trip
    and `/health` is forbidden one. Reporting "free" without asking would be
    the class of lie this contract exists to end.
    """
    return {"mode": state.mode, "plan": None,
            "subscription_active": False, "expires_at": None}


def effective(state: SessionState) -> dict:
    """The eleven booleans, as of 2026-08-20.

    CORRECTED 2026-08-21, and the correction is the point of this comment.
    These were reported True in BOTH modes on the strength of a measurement
    taken at the WRONG LAYER: `MistralAnonChat().chat()` -- the Python class --
    does answer with no credentials at all. But nobody reaches that class over
    HTTP. `POST /v1/chat/completions` checks `client.session_token` first and
    answers 401 without one (api/routes/chat.py:308).

    So an anonymous deployment reported `chat: true` and refused every request.
    That is exactly the failure this contract exists to prevent, produced by
    measuring the library instead of the endpoint. The rule is what a REQUEST
    achieves; the request achieves 401.

    Found by running the installer end to end against a real anonymous
    container, which is the only check that could have caught it.

    READ OFF THE CODE, and gated on an account:
      `vision` -- True. `api/routes/chat.py` pulls `image_url` content parts
        out of the request and uploads them to Mistral's blob storage
        (`upload_image_to_blob`).
      `images` -- True. `/v1/images/generations` is served.
      `audio_speech` -- True. `/v1/audio/speech` is served.
      `audio_transcription` -- True. `/v1/audio/transcriptions` is served.
      `search` -- True. `/v1/search` is served.
      `conversations` -- True, and this proxy is the reference shape for it:
        `/v1/conversations` plus `/{id}`, `/{id}/messages`, `/{id}/search`,
        pin, rename and delete. perplexity-proxy's implementation copies this
        response shape on purpose so the gateway reads one form for all five.

    False everywhere, in both modes:
      `tools` -- no function calling; nothing emits `tool_calls`. Emulation
        lives in the gateway (`emulates_tools`), and claiming it here would
        take credit for the gateway's work.
      `translate` -- no `/v1/translate` route.
      `files` -- no `/v1/files*` route. The `/v1/code/sessions/{id}/files`
        routes belong to the code-session feature, not to a general file API,
        and mapping them onto `files` would promise something else entirely.

    EVERY capability here now requires an account, and that is measured at the
    endpoint rather than inferred: an anonymous container answers 401 to
    /v1/chat/completions. If someone later makes the proxy fall back to the
    anonymous client when no session exists, these two can go back to True --
    but only after a request through the HTTP route returns text.
    """
    account = state.mode == "account"
    return {
        "chat":                account,
        "streaming":           account,
        "tools":               False,
        "vision":              account,
        "images":              account,
        "audio_speech":        account,
        "audio_transcription": account,
        "translate":           False,
        "search":              account,
        "files":               False,
        "conversations":       account,
    }


def require(name: str) -> None:
    """Refuse with 501 when this proxy cannot serve `name` right now (spec §3.4).

    501, not 404 and not 503, and the distinction is load-bearing for the
    gateway in front. A 404 is indistinguishable from a routing mistake. A 503
    says "it broke" -- so the gateway retries, accumulates suspicion against the
    route and fails over, spending attempts on something that was never going to
    work in this configuration. 501 says: this proxy, deliberately, does not do
    this right now.

    Synchronous on purpose: `snapshot()` reads environment variables, with no
    lock and no vendor call. Lives here rather than in api/main.py so the
    routers can gate their own endpoints without importing the app.
    """
    from fastapi import HTTPException

    if not effective(snapshot())[name]:
        raise HTTPException(
            501,
            f"This proxy cannot serve '{name}' in its current configuration "
            f"(see GET /health, capabilities.{name}).")
