"""
POST /v1/chat/completions — OpenAI-compatible endpoint.

Mapping:
  messages[-1] (user)  → Mistral newChat content (first message)
                        → or /api/chat mode=append (follow-up)
  conversation_id      → Mistral chatId (pass in request body or X-Conversation-Id header)
  stream               → SSE response or full JSON

Vision: image_url content parts (data: URIs or https:// URLs) are uploaded to
Mistral's Azure Blob storage via file.uploadFile tRPC and passed in the files array.

Returns conversation_id so clients can continue the conversation.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
import uuid
from datetime import datetime, timezone
from typing import Generator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from api.schemas import (
    ChatCompletionRequest, ChatCompletionResponse,
    Choice, Delta, Message, Usage,
)
from api.client import get_client

router = APIRouter(tags=["chat"])


def _upload_image_to_blob(client, image_bytes: bytes, mime_type: str) -> str:
    """
    Upload image bytes to Mistral's Azure Blob storage via file.uploadFile tRPC.

    Flow (APK F78458/requestUploadUrl, createFileUploadTask):
      1. POST /api/trpc/file.uploadFile?batch=1 {type:"image", count:1}
         → {uploadURLs: [presigned_azure_url]}
      2. PUT binary to presigned URL with Content-Type + x-ms-blob-type: BlockBlob
      3. Return the base blob URL (without SAS params) for use in files[] array.
    """
    r = client.session.post(
        "https://chat.mistral.ai/api/trpc/file.uploadFile?batch=1",
        json={"0": {"json": {"type": "image", "count": 1}}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"file.uploadFile failed: {r.text[:200]}")

    upload_url = r.json()[0]["result"]["data"]["json"]["uploadURLs"][0]
    blob_base_url = upload_url.split("?")[0]

    # Azure Blob SAS URLs use the SAS token in the URL for auth.
    # Use urllib directly to avoid sending the session's Authorization: Bearer header,
    # which Azure rejects when combined with a SAS URL.
    import urllib.request
    req = urllib.request.Request(
        upload_url,
        data=image_bytes,
        method="PUT",
        headers={"Content-Type": mime_type, "x-ms-blob-type": "BlockBlob"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Blob PUT failed: {e.code} {e.reason}")

    if status not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Blob PUT unexpected status: {status}")

    return blob_base_url


def _extract_images_from_messages(messages: list[Message]) -> tuple[str, list[dict]]:
    """
    Extract text and image files from the last user message.
    Images are base64 data: URIs or https:// URLs.
    Returns (text, [{"url": blob_url, "type": "image"}, ...]).
    """
    for m in reversed(messages):
        if m.role != "user":
            continue
        if isinstance(m.content, str):
            return m.content, []
        text_parts = []
        image_parts = []
        for part in m.content:
            if part.type == "text":
                text_parts.append(part.text or "")
            elif part.type == "image_url" and part.image_url:
                image_parts.append(part.image_url)
        return "".join(text_parts), image_parts
    return "", []


def _upload_message_images(client, image_parts: list[dict]) -> list[dict]:
    """
    Upload images from OpenAI image_url content parts to Mistral blob storage.
    Handles data: URIs (base64) and https:// URLs.
    Returns list of {url, type} dicts for the files[] array.
    """
    files = []
    for img in image_parts:
        url = img.get("url", "")
        if url.startswith("data:"):
            # data:image/jpeg;base64,<b64>
            header, _, b64 = url.partition(",")
            mime = header.split(";")[0].replace("data:", "") or "image/jpeg"
            image_bytes = base64.b64decode(b64)
            blob_url = _upload_image_to_blob(client, image_bytes, mime)
            files.append({"url": blob_url, "type": "image"})
        elif url.startswith("https://") or url.startswith("http://"):
            # Remote URL — pass directly (Mistral server will fetch it)
            files.append({"url": url, "type": "image"})
    return files


def _get_last_user_message(messages: list[Message]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            if isinstance(m.content, str):
                return m.content
            return "".join(p.text or "" for p in m.content if p.type == "text")
    return ""


def _get_system_message(messages: list[Message]) -> str | None:
    for m in messages:
        if m.role == "system":
            return m.content if isinstance(m.content, str) else None
    return None


def _post_chat(client, body: dict):
    """POST to /api/chat and return the raw streaming response."""
    return client.session.post(
        "https://chat.mistral.ai/api/chat",
        json=body,
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        timeout=120,
        stream=True,
    )


def _is_rate_limit_6200(resp) -> bool:
    """Return True if the response is a 429 with Mistral error code 6200."""
    if resp.status_code != 429:
        return False
    try:
        raw = b""
        for ch in resp.iter_content(chunk_size=None):
            raw += ch
        return json.loads(raw).get("code") == 6200
    except Exception:
        return False


def _make_anon_client():
    """Create a temporary anonymous MistralAnonChat (no auth, fresh UUID)."""
    from mistral_anon_chat import MistralAnonChat
    c = MistralAnonChat()
    c.bootstrap_session()
    return c


def _stream_tokens(client, resp, completion_id: str, chat_id: str, model: str) -> Generator[str, None, None]:
    """Yield OpenAI SSE chunks from an already-open /api/chat response."""
    created = int(time.time())

    def make_chunk(delta: dict, finish_reason: str | None = None) -> str:
        return f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}], 'conversation_id': chat_id})}\n\n"

    yield make_chunk({"role": "assistant"})

    if resp.status_code not in (200, 201):
        raw = b""
        for ch in resp.iter_content(chunk_size=None):
            raw += ch
        detail = raw.decode("utf-8", errors="replace")[:300] or f"HTTP {resp.status_code}"
        yield f"data: {json.dumps({'error': {'message': detail, 'type': 'api_error', 'code': resp.status_code}})}\n\n"
        yield "data: [DONE]\n\n"
        return

    for token in client._parse_stream(resp):
        yield make_chunk({"content": token})

    yield make_chunk({}, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _inject_system_prompt(user_text: str, system_prompt: str | None) -> str:
    """
    Prepend system prompt to user message.
    /api/chat has no native system prompt field (confirmed by APK analysis:
    clientPromptData only accepts {currentDate, userTimezone}).
    Uses XML delimiters so the model clearly distinguishes instructions from input.
    """
    if not system_prompt:
        return user_text
    return f"<system>\n{system_prompt}\n</system>\n\n{user_text}"


def _create_chat_with_files(
    client, text: str, files: list[dict],
    features: list | None = None,
    incognito: bool = False,
) -> str:
    """
    Create a new chat via tRPC message.newChat, including optional image files.
    files: [{"url": blob_url, "type": "image"}]
    features must match those sent to /api/chat (e.g. ["beta-imagegen"]).
    Returns chatId.
    """
    trpc_input = {
        "files": files,
        "content": [{"type": "text", "text": text}],
        "transcriptionsMetadata": [],
        "features": features or [],
        "integrations": [],
        "libraries": [],
        "productType": "chat",
        "projectId": None,
        "incognito": incognito,
    }
    r = client.session.post(
        "https://chat.mistral.ai/api/trpc/message.newChat?batch=1",
        json={"0": {"json": trpc_input}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"tRPC {r.status_code}: {r.text[:300]}")
    rdata = r.json()[0]["result"]["data"]["json"]
    chat_id = rdata.get("chatId")
    if not chat_id:
        raise RuntimeError(f"No chatId in tRPC response: {json.dumps(rdata)[:200]}")
    return chat_id


def _build_chat_body(client, req: ChatCompletionRequest, chat_id: str, mode: str, user_text: str) -> dict:
    """Build the /api/chat POST body depending on mode."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    if mode == "start":
        return {
            "mode": "start",
            "chatId": chat_id,
            "stableAnonymousIdentifier": client.stable_anon_id,
            "platform": "mobile",
            "clientPromptData": {"currentDate": now},
            "supportedTaskCallbacks": [],
            "features": req.features,
            "libraries": [],
            "integrations": [],
            "disabledFeatures": ["memory-inference"],
        }
    else:  # append
        return {
            "mode": "append",
            "chatId": chat_id,
            "messageId": str(uuid.uuid4()),
            "messageInput": [{"type": "text", "text": user_text}],
        }


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    client = get_client()

    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")

    conv_id = req.conversation_id or request.headers.get("X-Conversation-Id")

    user_text, image_parts = _extract_images_from_messages(req.messages)
    if not user_text and not image_parts:
        raise HTTPException(status_code=400, detail="No user message found in messages array.")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    system_prompt = _get_system_message(req.messages)

    files: list[dict] = []
    if conv_id:
        chat_id = conv_id
        mode = "append"
        effective_user_text = user_text or " "
    else:
        effective_user_text = _inject_system_prompt(user_text, system_prompt)
        try:
            files = _upload_message_images(client, image_parts) if image_parts else []
            chat_id = _create_chat_with_files(client, effective_user_text, files, features=req.features, incognito=req.incognito)
            mode = "start"
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to create chat: {e}")

    body = _build_chat_body(client, req, chat_id, mode, effective_user_text)
    resp = _post_chat(client, body)

    # Auth account rate-limited (6200) → fall back to anonymous with fresh UUID
    if _is_rate_limit_6200(resp):
        anon = _make_anon_client()
        try:
            anon_chat_id = _create_chat_with_files(
                anon, effective_user_text, files if not conv_id else [],
                features=req.features, incognito=req.incognito,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Anon fallback chat creation failed: {e}")
        anon_body = _build_chat_body(anon, req, anon_chat_id, "start", effective_user_text)
        resp = _post_chat(anon, anon_body)
        client, chat_id = anon, anon_chat_id

    if req.stream:
        return StreamingResponse(
            _stream_tokens(client, resp, completion_id, chat_id, req.model),
            media_type="text/event-stream",
            headers={
                "X-Conversation-Id": chat_id,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # Non-streaming: resp is already open, consume it
    if resp.status_code not in (200, 201):
        raw = b""
        for ch in resp.iter_content(chunk_size=None):
            raw += ch
        raise HTTPException(status_code=502, detail=raw.decode("utf-8", errors="replace")[:300])

    tokens = list(client._parse_stream(resp))
    content = "".join(tokens)

    return ChatCompletionResponse(
        id=completion_id,
        object="chat.completion",
        created=created,
        model=req.model,
        choices=[
            Choice(
                index=0,
                message=Message(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=0,
            completion_tokens=len(content.split()),
            total_tokens=len(content.split()),
        ),
        conversation_id=chat_id,
    )
