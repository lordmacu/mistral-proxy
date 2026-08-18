"""
/v1/users — user session, limits, and settings.
/v1/search — search across chats, messages, and workspace.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from api.client import get_client

router = APIRouter(tags=["users"])


def _trpc_get(client, proc: str, payload: dict) -> Any:
    inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
    r = client.session.get(
        f"https://chat.mistral.ai/api/trpc/{proc}?batch=1&input={inp}",
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"tRPC {proc}: {r.text[:300]}")
    return r.json()[0]["result"]["data"]["json"]


def _trpc_post(client, proc: str, payload: dict) -> Any:
    r = client.session.post(
        f"https://chat.mistral.ai/api/trpc/{proc}?batch=1",
        json={"0": {"json": payload}},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=r.status_code, detail=f"tRPC {proc}: {r.text[:300]}")
    return r.json()[0]["result"]["data"]["json"]


# ── User Info ─────────────────────────────────────────────────────────────────

@router.get("/v1/users/me")
def get_me():
    """Get current user session and limits (tRPC user.session + user.limits)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Fetch session and limits in parallel requests
    try:
        session = _trpc_get(client, "user.session", {})
    except Exception:
        session = {}

    try:
        limits = _trpc_get(client, "user.limits", {"stableAnonymousIdentifier": None})
    except Exception:
        limits = {}

    return {
        "object": "user",
        "session": session,
        "limits": limits,
        "authenticated": True,
    }


@router.get("/v1/users/limits")
def get_limits():
    """Get current user rate limits (tRPC user.limits)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_get(client, "user.limits", {"stableAnonymousIdentifier": None})


@router.get("/v1/users/settings")
def get_settings():
    """Get user settings (tRPC settings.get)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_get(client, "settings.get", {})


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/v1/search")
def search(
    q: str = Query(..., description="Search query"),
    scope: str = Query("all", description="Search scope: all, chats, messages, workspace"),
):
    """Search across conversations, messages, and workspace."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    results = {}

    if scope in ("all", "chats"):
        try:
            results["chats"] = _trpc_get(client, "chat.search", {"query": q})
        except Exception as e:
            results["chats"] = {"error": str(e)}

    if scope in ("all", "messages"):
        try:
            results["messages"] = _trpc_get(client, "message.search", {"query": q})
        except Exception as e:
            results["messages"] = {"error": str(e)}

    if scope in ("all", "workspace"):
        try:
            results["workspace"] = _trpc_get(client, "workspace.search", {"query": q})
        except Exception as e:
            results["workspace"] = {"error": str(e)}

    return {"object": "search_results", "query": q, "scope": scope, **results}


@router.get("/v1/conversations/{conversation_id}/search")
def search_in_conversation(conversation_id: str, q: str = Query(...)):
    """Search messages within a specific conversation."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_get(client, "message.search", {"query": q, "chatId": conversation_id})


# ── Reactions ─────────────────────────────────────────────────────────────────

class ReactionRequest(BaseModel):
    message_id: str
    chat_id: str
    version: int = 0
    reaction: Optional[str] = None  # "neutral" | "like" | "dislike" | None → "neutral"


@router.post("/v1/messages/reactions")
def update_reaction(body: ReactionRequest):
    """
    Add or remove a reaction from a message (tRPC message.updateReaction).
    reaction must be "neutral" (remove), "like", or "dislike".
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_post(client, "message.updateReaction", {
        "id": body.message_id,
        "chatId": body.chat_id,
        "version": body.version,
        "reaction": body.reaction or "neutral",
    })


# ── Cancel Generation ─────────────────────────────────────────────────────────

@router.post("/v1/conversations/{conversation_id}/cancel")
def cancel_generation(conversation_id: str):
    """Cancel an in-progress AI generation (tRPC chat.cancelGeneration)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = _trpc_post(client, "chat.cancelGeneration", {"chatId": conversation_id})
    return {"canceled": True, "conversation_id": conversation_id, "result": result}


# ── Memory ────────────────────────────────────────────────────────────────────

@router.get("/v1/memory")
def get_memory_status():
    """Check if memory feature is enabled (tRPC memory.getIsFeatureEnabled)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_get(client, "memory.getIsFeatureEnabled", {})


class MemoryToggle(BaseModel):
    enabled: bool


@router.post("/v1/memory/toggle")
def toggle_memory(body: MemoryToggle):
    """Enable or disable memory feature (tRPC memory.toggleFeatureActivation)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_post(client, "memory.toggleFeatureActivation", {"isEnabled": body.enabled})


# ── Connectors ────────────────────────────────────────────────────────────────

@router.get("/v1/connectors")
def list_connectors(chat_id: Optional[str] = Query(None)):
    """List available connectors/integrations (tRPC integrations.listForChat)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = {}
    if chat_id:
        payload["chatId"] = chat_id
    return _trpc_get(client, "integrations.listForChat", payload)


@router.get("/v1/connectors/{connector_id}")
def get_connector(connector_id: str, chat_id: Optional[str] = Query(None)):
    """Get connector details (tRPC integrations.byIdForChat)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload: dict = {"connectorId": connector_id}
    if chat_id:
        payload["chatId"] = chat_id
    return _trpc_get(client, "integrations.byIdForChat", payload)


@router.get("/v1/connectors/{connector_id}/auth-url")
def get_connector_auth_url(connector_id: str):
    """Get OAuth authorization URL for a connector (tRPC integrations.getAuthUrl — POST mutation)."""
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_post(client, "integrations.getAuthUrl", {"integrationId": connector_id})


class PermissionUpdate(BaseModel):
    function_name: str
    integration_id: str
    integration_name: str
    permission: str = "allow"  # "allow" | "ask" (server rejects anything else)


class PermissionsBody(BaseModel):
    updates: list[PermissionUpdate]


@router.post("/v1/connectors/permissions")
def update_connector_permissions(body: PermissionsBody):
    """
    Update connector function permissions (tRPC user.integrationFunctionPermissions.update).
    Called after the AI emits an ask_enable_connector task_callback.
    Each update: {function_name, integration_id, integration_name, permission: 'allow'|'deny'}
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    updates = [
        {
            "functionName": u.function_name,
            "integrationId": u.integration_id,
            "integrationName": u.integration_name,
            "permission": u.permission,
        }
        for u in body.updates
    ]
    return _trpc_post(client, "user.integrationFunctionPermissions.update", {"updates": updates})


# ── Canvas (web-only) ─────────────────────────────────────────────────────────

@router.get("/v1/canvas/{chat_id}/{canvas_id}")
def get_canvas(chat_id: str, canvas_id: str):
    """
    Get a canvas by ID (tRPC canva.byId).
    Canvas is a WEB-ONLY feature — not supported in the mobile APK.
    canvas_id: UUID of the canvas (embedded in message parts by the AI).
    Types: svg, markdown, html, react, code, mermaid, slides, table, spreadsheet.
    Share visibilities: private | workspace | organization | public.
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_get(client, "canva.byId", {"chatId": chat_id, "canvasId": canvas_id})


# ── AFP Articles (partner news) ───────────────────────────────────────────────

class AFPArticleBody(BaseModel):
    article_id: str


@router.post("/v1/afp/articles")
def get_afp_article(body: AFPArticleBody):
    """
    Fetch a full AFP (Agence France-Presse) article (tRPC reference.getAFPArticle).
    This is a MUTATION (POST), not a query.
    AFP articles appear in web search results; this fetches the full content.
    There is a daily article limit per account.
    article_id: the AFP article identifier from search results.
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_post(client, "reference.getAFPArticle", {"articleId": body.article_id})


# ── Deep Research (polling) ───────────────────────────────────────────────────

@router.get("/v1/research/{task_id}")
def get_research_task(
    task_id: str,
    message_id: str = Query(..., description="Message ID containing the research task"),
    message_version: int = Query(0, description="Message version"),
    chat_id: str = Query(..., description="Chat/conversation ID"),
):
    """
    Poll a Deep Research task (tRPC research.getResearchTask).
    Deep Research is an AI mode that does multi-step web research.
    Poll this endpoint every ~3s until status='completed'.
    task_id comes from the AI's message part (type: 'research_task').
    Returns: {status, steps: [{title, status, results:[]}], referencesCount}
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return _trpc_get(client, "research.getResearchTask", {
        "taskId": task_id,
        "message": {
            "id": message_id,
            "version": message_version,
            "chatId": chat_id,
        },
    })
