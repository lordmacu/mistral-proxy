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

    MEASURED, with every credential stripped from the environment:
      `chat` / `streaming` -- True in BOTH modes, and mistral is the only
        proxy of the five for which that holds. An anonymous
        `MistralAnonChat` answered a prompt after nothing but
        `bootstrap_session()`. It is also the streaming path by
        construction: `chat()` iterates `send_message()`, a generator that
        yields tokens as they arrive, so the measurement exercised the SSE
        path rather than a batch endpoint.

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

    HONEST LIMIT ON THE ANONYMOUS BRANCH: only `chat` and `streaming` were
    measured without credentials. The six account-gated booleans above are
    reported False in anonymous mode because they were NOT measured there, not
    because a measurement showed them failing. That direction is deliberate --
    under-claiming costs a route the gateway could have used, over-claiming
    costs a user a broken request -- and the deployed instance holds
    credentials, so the branch is theory today. Measure before loosening it.
    """
    account = state.mode == "account"
    return {
        "chat":                True,
        "streaming":           True,
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
