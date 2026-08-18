"""
/v1/code/* — Vibe Code endpoints exposed via FastAPI.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import json

from api.schemas import VibeProjectCreate, VibeSessionCreate, VibeSessionSend, VibeCallbackRespond
from api.client import get_client, get_vibe

router = APIRouter(prefix="/v1/code", tags=["vibe-code"])


# ── Projects ─────────────────────────────────────────────────────────────────

@router.get("/projects")
def list_projects():
    return {"object": "list", "data": get_vibe().list_projects()}


@router.post("/projects")
def create_project(req: VibeProjectCreate):
    return get_vibe().create_project(req.name)


# ── Sessions ─────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/sessions")
def list_sessions(project_id: str):
    return {"object": "list", "data": get_vibe().list_sessions(project_id)}


@router.post("/projects/{project_id}/sessions")
def create_session(project_id: str, req: VibeSessionCreate):
    get_vibe().init_feature()
    return get_vibe().create_session(project_id, req.prompt)


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    return get_vibe().get_session(session_id)


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str):
    msgs = get_vibe().get_messages(session_id)
    return {"object": "list", "session_id": session_id, "data": msgs}


@router.get("/sessions/{session_id}/artifacts")
def list_artifacts(session_id: str):
    return {"object": "list", "session_id": session_id, "data": get_vibe().list_artifacts(session_id)}


@router.get("/sessions/{session_id}/files")
def get_session_files(session_id: str):
    """Extract files written by the agent (from write_file tool call args)."""
    files = get_vibe().get_files_from_messages(session_id)
    return {"object": "list", "session_id": session_id, "data": files}


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: str, req: VibeSessionSend):
    return get_vibe().send_message(session_id, req.message)


@router.post("/sessions/{session_id}/respond")
def respond_callback(session_id: str, req: VibeCallbackRespond):
    result = {
        "answers": req.answers,
        "canceled": req.canceled,
    }
    return get_vibe().respond_callback(session_id, req.callback_id, result)


@router.get("/sessions/{session_id}/stream")
def stream_session(session_id: str):
    """SSE stream of session events. Reconnects to a running session."""
    vibe = get_vibe()

    def event_generator():
        for ev in vibe.subscribe(session_id):
            yield f"data: {json.dumps(ev)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
