"""
POST /v1/images/generations — OpenAI-compatible image generation.

Discovery (APK analysis):
- No separate image-generation REST endpoint on chat.mistral.ai.
- Image generation is triggered by including "beta-imagegen" in the features array
  of /api/chat. The model responds with a tool call then a {type:"image_url",
  meta:{uri:"..."}} chunk in the stream.
- Flow: create chat via tRPC message.newChat → POST /api/chat with
  features:["beta-imagegen"] → parse stream for image_url chunks → return URI.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Generator

from fastapi import APIRouter, HTTPException

from api.schemas import ImageGenerationRequest, ImageData, ImageGenerationResponse
from api.client import get_client

router = APIRouter(tags=["images"])


def _read_body(resp) -> str:
    """Drain a STREAMED response into text.

    This is the root cause of the empty `502 "API error: "` this endpoint used
    to answer. The request is made with `stream=True`, so `resp.text` and
    `resp.json()` are empty until the body is consumed -- and the error path
    read `resp.text[:200]` directly, which is always "" on an unconsumed stream.
    Every diagnosis of an upstream failure here has to drain it first.
    """
    try:
        raw = b"".join(resp.iter_content(chunk_size=None))
    except Exception:
        raw = b""
    if not raw:
        raw = (getattr(resp, "content", b"") or b"")
    return raw.decode("utf-8", errors="replace").strip()


def _error_detail(resp, fallback: str) -> str:
    """Name what went wrong, never return an empty string.

    Mistral sends {"detail": "...", "code": 6200} for a spent quota. Anything
    unexpected falls back to the raw body, and an empty body to `fallback` --
    the one thing this must never do again is report nothing at all.
    """
    text = _read_body(resp)
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        body = None
    if isinstance(body, dict):
        msg = body.get("detail") or body.get("message")
        code = body.get("code")
        if msg:
            return f"{msg}{f' (code {code})' if code else ''}"
    return text[:200] or fallback


def _generate_image_stream(client, prompt: str) -> tuple[str | None, str | None]:
    """
    Send a prompt to /api/chat with beta-imagegen feature.
    Returns (image_url, revised_prompt).
    """
    # Mistral image generation works through the chat interface: the model uses
    # the generate_image tool when it detects image-generation intent. Wrapping
    # the user's raw prompt ("a red apple") makes that intent unambiguous so the
    # model doesn't answer as a text question instead.
    wrapped = f"Generate an image of: {prompt}"
    trpc_input = {
        "files": [],
        "content": [{"type": "text", "text": wrapped}],
        "transcriptionsMetadata": [],
        "features": ["beta-imagegen"],
        "integrations": [],
        "libraries": [],
        "productType": "chat",
        "projectId": None,
        "incognito": False,
    }
    r = client.session.post(
        "https://chat.mistral.ai/api/trpc/message.newChat?batch=1",
        json={"0": {"json": trpc_input}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"tRPC newChat failed: {r.text[:200]}")

    payload = r.json()
    rdata = payload[0]["result"]["data"]["json"]
    chat_id = rdata.get("chatId")
    if not chat_id:
        raise HTTPException(status_code=502, detail="No chatId from tRPC newChat")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "mode": "start",
        "chatId": chat_id,
        "stableAnonymousIdentifier": client.stable_anon_id,
        "platform": "mobile",
        "clientPromptData": {"currentDate": now},
        "supportedTaskCallbacks": [],
        "features": ["beta-imagegen"],
        "libraries": [],
        "integrations": [],
        "disabledFeatures": ["memory-inference"],
    }

    resp = client.session.post(
        "https://chat.mistral.ai/api/chat",
        json=body,
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        timeout=120,
        stream=True,
    )

    if resp.status_code == 429:
        # Mistral answers a spent quota with a PLAIN JSON 429
        # ({"detail":"Message rate limit reached","code":6200}) -- not an SSE
        # frame. The old code fell through to the type-6 parser below, found
        # nothing (there is no SSE to parse) and raised `502 "API error: "`,
        # with an empty message: the one failure an operator most needs named,
        # reported as an anonymous upstream error. Worse for llm-libre, which
        # classifies a 502 as evidence the ROUTE is broken and a 429 as
        # "rate-limited, back off" -- so a spent quota was punishing the route
        # with the wrong mechanism.
        raise HTTPException(status_code=429, detail=_error_detail(
            resp, "Message rate limit reached on the Mistral account"))
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="API error: " + _error_detail(
            resp, f"HTTP {resp.status_code}"))

    image_url = None
    revised_prompt = None

    raw = b""
    for chunk in resp.iter_content(chunk_size=None):
        raw += chunk

    for line in raw.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        colon = line.index(":")
        try:
            line_type = int(line[:colon])
            json_str = line[colon + 1:]
        except ValueError:
            continue
        if not json_str or json_str == "null":
            continue
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            continue

        j = data.get("json", data) if isinstance(data, dict) else data
        if not isinstance(j, dict):
            continue

        if line_type == 6:
            # `.get("message")` alone returned "" for a frame that carries the
            # text under another key, which is how this endpoint used to answer
            # `502 "API error: "`. Fall back to the whole frame rather than to
            # nothing: an operator can act on an odd payload, never on silence.
            msg = j.get("message") or j.get("detail") or json.dumps(j)[:200]
            raise HTTPException(status_code=502, detail=f"API error: {msg}")

        if j.get("type") == "message":
            for patch in j.get("patches", []):
                val = patch.get("value")
                path = patch.get("path", "")
                op = patch.get("op")

                # Full contentChunks replace
                if op == "replace" and path == "/contentChunks" and isinstance(val, list):
                    for chunk in val:
                        if isinstance(chunk, dict) and chunk.get("type") == "image_url":
                            meta = chunk.get("meta") or {}
                            image_url = meta.get("uri") or chunk.get("url")

                # Individual chunk append/replace
                if "/contentChunks" in path and isinstance(val, dict):
                    if val.get("type") == "image_url":
                        meta = val.get("meta") or {}
                        image_url = meta.get("uri") or val.get("url")
                    elif val.get("type") == "tool_call":
                        args = val.get("publicArguments") or {}
                        if isinstance(args, dict):
                            revised_prompt = args.get("prompt")

    return image_url, revised_prompt


@router.post("/v1/images/generations", response_model=ImageGenerationResponse)
def create_image(req: ImageGenerationRequest):
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")

    results = []
    for _ in range(max(1, req.n)):
        image_url, revised = _generate_image_stream(client, req.prompt)
        if image_url:
            results.append(ImageData(url=image_url, revised_prompt=revised or req.prompt))
        else:
            # 502 is right here and deliberately NOT 200-with-empty-data: an
            # empty `data` array reads as success to an OpenAI client, and
            # llm-libre would record a success for a route that generated
            # nothing. The model returned text instead of using generate_image.
            raise HTTPException(
                status_code=502,
                detail="Model did not generate an image (responded with text). "
                       "The APK bundle confirms code 'did not generate an image' "
                       "is a known case — the model chose not to use the generate_image tool.",
            )

    return ImageGenerationResponse(created=int(time.time()), data=results)
