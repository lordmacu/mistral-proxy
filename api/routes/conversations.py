"""
/v1/conversations — native Mistral conversation history via tRPC.chat.last + message.all.
No local storage needed: history lives on Mistral's servers.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from api.schemas import ConversationItem, ConversationList, ConversationMessage, ConversationMessages
from api.client import get_client

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


def _trpc_get(client, proc: str, payload: dict):
    inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
    r = client.session.get(
        f"https://chat.mistral.ai/api/trpc/{proc}?batch=1&input={inp}",
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"tRPC {proc}: {r.text[:200]}")
    return r.json()[0]["result"]["data"]["json"]


def _trpc_post(client, proc: str, payload: dict):
    r = client.session.post(
        f"https://chat.mistral.ai/api/trpc/{proc}?batch=1",
        json={"0": {"json": payload}},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=f"tRPC {proc}: {r.text[:200]}")
    return r.json()[0]["result"]["data"]["json"]


@router.get("", response_model=ConversationList)
def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    product_type: str = Query("chat"),
):
    """List recent conversations from Mistral's server (tRPC chat.last)."""
    client = get_client()
    payload: dict = {
        "chatVisibility": "private",
        "chatPermission": "write",
        "includeProjectChats": True,
        "includeScheduledTaskChats": False,
        "productType": product_type,
    }
    if cursor:
        payload["cursor"] = cursor

    data = _trpc_get(client, "chat.last", payload)
    items = [
        ConversationItem(
            id=c["id"],
            title=c.get("userTitle") or c.get("generatedTitle") or c.get("title"),
            generated_title=c.get("generatedTitle"),
            updated_at=c.get("updatedAt"),
            pinned=c.get("pinned", False),
            project_id=c.get("projectId"),
        )
        for c in data.get("items", [])[:limit]
    ]
    return ConversationList(data=items, next_cursor=data.get("nextCursor"))


@router.get("/{conversation_id}", response_model=ConversationItem)
def get_conversation(conversation_id: str):
    """Get conversation metadata (tRPC chat.byId)."""
    client = get_client()
    data = _trpc_get(client, "chat.byId", {"id": conversation_id})
    return ConversationItem(
        id=data["id"],
        title=data.get("userTitle") or data.get("generatedTitle") or data.get("title"),
        generated_title=data.get("generatedTitle"),
        updated_at=data.get("updatedAt"),
        pinned=data.get("pinned", False),
        project_id=data.get("projectId"),
    )


@router.get("/{conversation_id}/messages", response_model=ConversationMessages)
def get_messages(
    conversation_id: str,
    cursor: str | None = Query(None),
):
    """Get all messages in a conversation (tRPC message.all)."""
    client = get_client()
    payload: dict = {"chatId": conversation_id}
    if cursor:
        payload["cursor"] = cursor

    data = _trpc_get(client, "message.all", payload)
    items_raw = data.get("items", data) if isinstance(data, dict) else data
    if isinstance(items_raw, dict):
        items_raw = items_raw.get("items", [])

    messages = []
    for m in (items_raw or []):
        role = m.get("role", "assistant")
        # Extract text from contentChunks array
        chunks = m.get("contentChunks") or []
        text_parts = [c.get("text", "") for c in chunks if c and c.get("type") == "text"]
        content = "".join(text_parts) or m.get("content", "")
        messages.append(ConversationMessage(
            role=role,
            content=content,
            id=m.get("id"),
        ))

    return ConversationMessages(
        conversation_id=conversation_id,
        data=messages,
        next_cursor=data.get("nextCursor") if isinstance(data, dict) else None,
    )


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: str):
    """Delete a conversation (tRPC chat.delete)."""
    client = get_client()
    try:
        _trpc_post(client, "chat.delete", {"id": conversation_id})
        return {"deleted": True, "id": conversation_id}
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail="Conversation not found")
        raise


@router.patch("/{conversation_id}")
def update_conversation(conversation_id: str, body: dict):
    """Rename a conversation (tRPC chat.updateTitle). Body: {"title": "..."}"""
    client = get_client()
    result = _trpc_post(client, "chat.updateTitle", {
        "chatId": conversation_id,
        "userTitle": body.get("title", ""),
    })
    return {"updated": True, "id": conversation_id, "result": result}


@router.post("/{conversation_id}/share")
def share_conversation(conversation_id: str):
    """
    Share a conversation publicly (tRPC chat.share).
    Returns {id: shareUUID} — public URL: https://chat.mistral.ai/share/{id}
    Feature flag: publicChatSharingEnabled=true required.
    """
    client = get_client()
    result = _trpc_post(client, "chat.share", {"id": conversation_id})
    share_id = result.get("id") if isinstance(result, dict) else None
    return {
        "share_id": share_id,
        "url": f"https://chat.mistral.ai/share/{share_id}" if share_id else None,
        "raw": result,
    }


class PinBody(BaseModel):
    pinned: bool


@router.post("/{conversation_id}/pin")
def pin_conversation(conversation_id: str, body: PinBody):
    """
    Pin or unpin a conversation (tRPC chat.setPinned).
    Body: {"pinned": true|false}
    Returns full chat object with updated pinned field.
    """
    client = get_client()
    return _trpc_post(client, "chat.setPinned", {
        "chatId": conversation_id,
        "pinned": body.pinned,
    })
