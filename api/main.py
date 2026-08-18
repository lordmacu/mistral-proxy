"""
Mistral AI — OpenAI-compatible FastAPI server.
Reverse-engineered from ai.mistral.chat APK v2.8.0.

Endpoints (63 total):
  POST /v1/auth/login             — obtain session token
  POST /v1/auth/register          — create account + get token
  GET  /v1/auth/me                — current auth status
  GET  /v1/models                 — list available models (20+ models)
  POST /v1/chat/completions       — OpenAI-compatible chat (stream, vision, audio)
  POST /v1/images/generations     — OpenAI-compatible image generation (text → image via beta-imagegen)
  POST /v1/vision                 — send image(s) + prompt → text (URL or data URI input)
  POST /v1/audio/speech           — TTS: text → WAV 24kHz mono
  POST /v1/audio/transcriptions   — STT: audio → text (Whisper-compatible)
  POST /v1/audio/speak            — read-aloud existing message by IDs (SSE)
  GET  /v1/conversations          — list conversations
  GET  /v1/conversations/{id}     — get conversation metadata
  GET  /v1/conversations/{id}/messages — get messages
  GET  /v1/conversations/{id}/search  — search messages in conversation
  POST /v1/conversations/{id}/cancel  — cancel active generation
  POST /v1/conversations/{id}/share   — share conversation publicly → returns share URL
  POST /v1/conversations/{id}/pin     — pin/unpin conversation {pinned: bool}
  DELETE /v1/conversations/{id}   — delete conversation
  PATCH  /v1/conversations/{id}   — rename conversation
  GET  /v1/users/me               — current user session + limits
  GET  /v1/users/limits           — rate limits and quotas
  GET  /v1/users/settings         — 50+ org feature flags (scheduledTasksEnabled etc.)
  GET  /v1/search?q=              — search across chats, messages, workspace
  POST /v1/messages/reactions     — add/remove message reaction
  GET  /v1/memory                 — memory feature status (enabled/disabled)
  POST /v1/memory/toggle          — toggle memory on/off
  GET  /v1/connectors             — list 16 connectors (14 OAuth2 + 2 MCP)
  GET  /v1/connectors/{id}        — get connector details
  GET  /v1/connectors/{id}/auth-url — OAuth URL for connector authorization
  POST /v1/connectors/permissions — grant connector function permissions (post ask_enable_connector)
  GET  /v1/canvas/{chat_id}/{canvas_id} — get canvas (web-only: svg/html/react/mermaid/etc.)
  POST /v1/afp/articles           — fetch AFP (Agence France-Presse) partner article
  GET  /v1/research/{task_id}     — poll Deep Research task status (steps, references)
  GET  /v1/skills                 — list Vibe Work skills (12 available)
  GET  /v1/skills/{id}            — get skill body (markdown system prompt)
  GET  /v1/tasks                  — list scheduled tasks (status: scheduled|paused|completed)
  GET  /v1/tasks/{id}             — get scheduled task
  GET  /v1/tasks/{id}/executions  — get task execution history
  POST /v1/tasks/{id}/trigger     — manually trigger a task
  POST /v1/tasks/{id}/pause       — pause a scheduled task
  POST /v1/tasks/{id}/resume      — resume a paused task
  DELETE /v1/tasks/{id}           — delete a scheduled task
  GET  /v1/workflows              — list workflows
  GET  /v1/workflows/{id}         — get workflow
  POST /v1/workflows/start        — start a workflow execution
  POST /v1/workflows/{id}/input   — send user input to running workflow
  GET  /v1/projects               — list projects (free: max 2)
  GET  /v1/projects/limits        — get project limits
  GET  /v1/projects/{id}          — get project
  POST /v1/projects/{id}/chats    — add chat to project
  DELETE /v1/projects/{id}/chats/{chat_id} — remove chat from project
  DELETE /v1/projects/{id}        — delete project
  GET  /v1/organizations          — list user organizations + org tier
  GET  /v1/billing/pro            — Le Chat Pro plan description + features
  GET  /v1/code/projects          — list Vibe Code projects
  POST /v1/code/projects          — create Vibe Code project
  GET  /v1/code/projects/{id}/sessions
  POST /v1/code/projects/{id}/sessions
  GET  /v1/code/sessions/{id}
  GET  /v1/code/sessions/{id}/messages
  GET  /v1/code/sessions/{id}/files
  GET  /v1/code/sessions/{id}/artifacts
  GET  /v1/code/sessions/{id}/stream  (SSE)
  POST /v1/code/sessions/{id}/messages
  POST /v1/code/sessions/{id}/respond

Auth:
  Set MISTRAL_SESSION_TOKEN env var, or POST /v1/auth/login.
  Token is an Ory session token (ory_st_...).
  All /v1/* requests automatically use the token via curl_cffi (bypasses Cloudflare).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import auth, models, chat, conversations, vibe, images, audio, users, skills
from api.client import get_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up client on startup
    client = get_client()
    client.bootstrap_session()
    yield


app = FastAPI(
    title="Mistral AI Proxy",
    description=__doc__,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(models.router)
app.include_router(chat.router)
app.include_router(images.router)
app.include_router(audio.router)
app.include_router(conversations.router)
app.include_router(users.router)
app.include_router(skills.router)
app.include_router(vibe.router)


@app.get("/health")
def health():
    client = get_client()
    return {
        "status": "ok",
        "authenticated": bool(client.session_token),
    }


@app.get("/")
def root():
    return {
        "service": "mistral-proxy",
        "version": "1.0.0",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }
