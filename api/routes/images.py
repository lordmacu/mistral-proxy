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

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Generator

from fastapi import APIRouter, HTTPException

from api.schemas import ImageGenerationRequest, ImageData, ImageGenerationResponse, VisionRequest, VisionResponse
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
    tool_interrupted = False  # isInterrupted:true → server-side quota exceeded

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

                # Full contentChunks replace (tool_call arrives as a list)
                if op == "replace" and path == "/contentChunks" and isinstance(val, list):
                    for chunk in val:
                        if not isinstance(chunk, dict):
                            continue
                        if chunk.get("type") == "image_url":
                            meta = chunk.get("meta") or {}
                            image_url = (
                                chunk.get("imageUrl")       # actual field name from APK SSE
                                or meta.get("uri")
                                or chunk.get("url")
                            )
                        elif chunk.get("type") == "tool_call" and chunk.get("name") == "generate_image":
                            # isInterrupted:true = server blocked the call (quota exceeded)
                            if chunk.get("isInterrupted"):
                                tool_interrupted = True
                            args = chunk.get("publicArguments") or {}
                            if isinstance(args, dict) and args.get("prompt"):
                                revised_prompt = args["prompt"]

                # Individual chunk patch
                if "/contentChunks" in path and isinstance(val, dict):
                    if val.get("type") == "image_url":
                        meta = val.get("meta") or {}
                        image_url = (
                            val.get("imageUrl")             # actual field name from APK SSE
                            or meta.get("uri")
                            or val.get("url")
                        )
                    elif val.get("type") == "tool_call" and val.get("name") == "generate_image":
                        if val.get("isInterrupted"):
                            tool_interrupted = True
                        args = val.get("publicArguments") or {}
                        if isinstance(args, dict):
                            revised_prompt = args.get("prompt")
                        result = val.get("publicResult") or {}
                        if isinstance(result, dict) and result.get("url") and not image_url:
                            image_url = result["url"]

                # success:false on /contentChunks/0/success path also signals interruption
                if path == "/contentChunks/0/success" and val is False:
                    tool_interrupted = True

    if tool_interrupted and not image_url:
        raise HTTPException(
            status_code=429,
            detail="Image generation quota exceeded on the Mistral account. "
                   "The server interrupted the generate_image tool call (isInterrupted=true). "
                   "Try again tomorrow or use a different account.",
        )

    return image_url, revised_prompt


_MAX_ATTEMPTS = 3


@router.post("/v1/images/generations", response_model=ImageGenerationResponse)
def create_image(req: ImageGenerationRequest):
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")

    results = []
    for _ in range(max(1, req.n)):
        image_url: str | None = None
        revised: str | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                image_url, revised = _generate_image_stream(client, req.prompt)
            except HTTPException as exc:
                if exc.status_code == 429:
                    raise  # quota exhausted — retrying won't help
                raise
            if image_url:
                break
            # BFL backend returned success but no image URL yet — retry.
            # Each attempt creates a new chat (2 credits); 3 attempts = 6 credits max.
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(3)
        if image_url:
            results.append(ImageData(url=image_url, revised_prompt=revised or req.prompt))
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Model did not generate an image after {_MAX_ATTEMPTS} attempts "
                       "(BFL backend timed out or model used text response).",
            )

    return ImageGenerationResponse(created=int(time.time()), data=results)


# ── Vision ────────────────────────────────────────────────────────────────────

def _upload_image(client, image_source: str) -> tuple[str, str]:
    """
    Upload a single image to Mistral's Azure Blob storage and return (read_url, filename).

    image_source may be:
      - A public HTTPS URL  → fetched with urllib then uploaded
      - A data URI          → decoded then uploaded

    Flow (Next.js web app chunk 2nqt98tojzakk.js):
      1. file.uploadFile {type:"image", count:1, includeReadUrl:true} → SAS URLs
      2. PUT binary to uploadURL with Content-Type + x-ms-blob-type:BlockBlob
      3. file.confirmUpload {uploadUrl, type:"image"}
      4. Return readURL (has read-only SAS token the model uses to fetch the image)
    """
    BASE = "https://chat.mistral.ai"

    if image_source.startswith("data:"):
        # data:image/jpeg;base64,<data>
        header, _, b64 = image_source.partition(",")
        mime_type = header.split(";")[0].replace("data:", "") or "image/jpeg"
        image_bytes = base64.b64decode(b64)
        ext = mimetypes.guess_extension(mime_type) or ".jpg"
        filename = f"image{ext}"
    else:
        # Public URL — fetch it
        req = urllib.request.Request(image_source, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                image_bytes = resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to fetch image URL: {exc}")
        mime_type = content_type or "image/jpeg"
        filename = image_source.rsplit("/", 1)[-1].split("?")[0] or "image.jpg"
        if "." not in filename:
            ext = mimetypes.guess_extension(mime_type) or ".jpg"
            filename = f"image{ext}"

    # Step 1: get SAS upload + read URLs
    r = client.session.post(
        f"{BASE}/api/trpc/file.uploadFile?batch=1",
        json={"0": {"json": {"type": "image", "count": 1, "includeReadUrl": True}}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"file.uploadFile failed: {r.text[:200]}")

    result = r.json()[0]["result"]["data"]["json"]
    upload_url = result["uploadURLs"][0]
    read_url = result["readURLs"][0]

    # Step 2: PUT to Azure Blob (no Authorization header — SAS token is in the URL)
    put_req = urllib.request.Request(
        upload_url,
        data=image_bytes,
        method="PUT",
        headers={"Content-Type": mime_type, "x-ms-blob-type": "BlockBlob"},
    )
    try:
        with urllib.request.urlopen(put_req, timeout=60) as put_resp:
            if put_resp.status not in (200, 201):
                raise HTTPException(status_code=502, detail=f"Azure Blob PUT returned {put_resp.status}")
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Azure Blob PUT failed: {exc.code} {exc.reason}")

    # Step 3: confirm upload (required by Mistral for images)
    client.session.post(
        f"{BASE}/api/trpc/file.confirmUpload?batch=1",
        json={"0": {"json": {"uploadUrl": upload_url, "type": "image"}}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=10,
    )

    return read_url, filename


def _vision_stream(client, prompt: str, image_sources: list[str]) -> str:
    """Upload images, create a chat, stream the response, return the full text."""
    BASE = "https://chat.mistral.ai"

    # Upload all images
    files = []
    for src in image_sources:
        read_url, filename = _upload_image(client, src)
        files.append({"type": "image", "url": read_url, "name": filename})

    # Create chat with image attachments
    trpc_input = {
        "files": files,
        "content": [{"type": "text", "text": prompt}],
        "transcriptionsMetadata": [],
        "features": [],
        "integrations": [],
        "libraries": [],
        "productType": "chat",
        "projectId": None,
        "incognito": False,
    }
    r = client.session.post(
        f"{BASE}/api/trpc/message.newChat?batch=1",
        json={"0": {"json": trpc_input}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"tRPC newChat failed: {r.text[:200]}")

    chat_id = r.json()[0]["result"]["data"]["json"].get("chatId")
    if not chat_id:
        raise HTTPException(status_code=502, detail="No chatId from tRPC newChat")

    # Stream generation
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body = {
        "mode": "start",
        "chatId": chat_id,
        "stableAnonymousIdentifier": client.stable_anon_id,
        "platform": "mobile",
        "clientPromptData": {"currentDate": now},
        "supportedTaskCallbacks": [],
        "features": [],
        "libraries": [],
        "integrations": [],
        "disabledFeatures": ["memory-inference"],
    }
    resp = client.session.post(
        f"{BASE}/api/chat",
        json=body,
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        timeout=120,
        stream=True,
    )
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail=_error_detail(resp, "Rate limit reached"))
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="API error: " + _error_detail(resp, f"HTTP {resp.status_code}"))

    raw = b"".join(resp.iter_content(chunk_size=None))
    text_parts: list[str] = []

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
            msg = j.get("message") or j.get("detail") or json.dumps(j)[:200]
            raise HTTPException(status_code=502, detail=f"API error: {msg}")

        if j.get("type") == "message":
            for patch in j.get("patches", []):
                val = patch.get("value")
                path = patch.get("path", "")
                # Text arrives as op:append on /contentChunks/N/content
                if isinstance(val, str) and "content" in path:
                    text_parts.append(val)
                elif isinstance(val, dict) and val.get("type") == "text":
                    text_parts.append(val.get("text") or val.get("content") or "")

    return "".join(text_parts), chat_id


@router.post("/v1/vision", response_model=VisionResponse)
def vision(req: VisionRequest):
    """
    Send one or more images + a text prompt to Mistral and get a text response.

    Each image in `images` may be:
      - A public HTTPS URL (fetched and uploaded automatically)
      - A data URI: "data:image/jpeg;base64,..."

    The flow uses the same file upload path as the Mistral Le Chat web app:
    file.uploadFile → Azure Blob PUT → file.confirmUpload → message.newChat with files.
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")
    if not req.images:
        raise HTTPException(status_code=400, detail="Provide at least one image in `images`.")

    text, chat_id = _vision_stream(client, req.prompt, req.images)
    return VisionResponse(text=text, conversation_id=chat_id)
