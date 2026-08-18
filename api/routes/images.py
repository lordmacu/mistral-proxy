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


def _generate_image_stream(client, prompt: str) -> tuple[str | None, str | None]:
    """
    Send a prompt to /api/chat with beta-imagegen feature.
    Returns (image_url, revised_prompt).
    """
    # Create a new chat with the prompt
    trpc_input = {
        "files": [],
        "content": [{"type": "text", "text": prompt}],
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

    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"API error: {resp.text[:200]}")

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
            msg = j.get("message", str(j))
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
            raise HTTPException(status_code=502, detail="No image URL in model response")

    return ImageGenerationResponse(created=int(time.time()), data=results)
