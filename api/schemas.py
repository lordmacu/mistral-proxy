"""
OpenAI-compatible + Mistral-extension Pydantic schemas.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field


# ── Auth ─────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    email: str
    password: str
    first_name: str = "User"
    last_name: str = ""

class AuthResponse(BaseModel):
    token: str
    token_type: str = "bearer"


# ── OpenAI-compatible chat ────────────────────────────────────────────────────

class ContentPart(BaseModel):
    type: Literal["text", "image_url"] = "text"
    text: Optional[str] = None
    image_url: Optional[dict] = None

class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, list[ContentPart]]
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: str = "mistral-small-latest"
    messages: list[Message]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: int = 1
    stop: Optional[Union[str, list[str]]] = None
    # Mistral-specific extensions
    conversation_id: Optional[str] = Field(
        None,
        description="Mistral chatId — if provided, appends to existing conversation. "
                    "On first call, omit to get a new conversation_id in the response.",
    )
    incognito: bool = False
    features: list[str] = Field(
        default_factory=list,
        description="Mistral features: 'beta-reasoning' (think mode), 'agentic-fast', etc.",
    )
    product_type: Literal["chat", "work"] = "chat"

class Delta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None

class Choice(BaseModel):
    index: int
    message: Optional[Message] = None
    delta: Optional[Delta] = None
    finish_reason: Optional[str] = None

class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion", "chat.completion.chunk"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Optional[Usage] = None
    # Mistral extension: returned so the client can send follow-up messages
    conversation_id: Optional[str] = None


# ── Models ───────────────────────────────────────────────────────────────────

class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "mistral"
    description: Optional[str] = None

class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard]


# ── Conversations ─────────────────────────────────────────────────────────────

class ConversationItem(BaseModel):
    id: str
    title: Optional[str] = None
    generated_title: Optional[str] = None
    updated_at: Optional[str] = None
    pinned: bool = False
    project_id: Optional[str] = None

class ConversationList(BaseModel):
    object: str = "list"
    data: list[ConversationItem]
    next_cursor: Optional[str] = None

class ConversationMessage(BaseModel):
    role: str
    content: str
    id: Optional[str] = None

class ConversationMessages(BaseModel):
    object: str = "list"
    conversation_id: str
    data: list[ConversationMessage]
    next_cursor: Optional[str] = None


# ── Vibe Code ─────────────────────────────────────────────────────────────────

class VibeProjectCreate(BaseModel):
    name: str = "default"

class VibeSessionCreate(BaseModel):
    prompt: str

class VibeSessionSend(BaseModel):
    message: str

class VibeCallbackRespond(BaseModel):
    callback_id: str
    answers: list[dict]
    canceled: bool = False


# ── Images ────────────────────────────────────────────────────────────────────

class ImageGenerationRequest(BaseModel):
    prompt: str
    n: int = 1
    size: Optional[str] = None
    response_format: Literal["url", "b64_json"] = "url"
    model: Optional[str] = None

class ImageData(BaseModel):
    url: Optional[str] = None
    b64_json: Optional[str] = None
    revised_prompt: Optional[str] = None

class ImageGenerationResponse(BaseModel):
    created: int
    data: list[ImageData]


# ── Vision (image input) ──────────────────────────────────────────────────────

class VisionRequest(BaseModel):
    """Send one or more images + a text prompt to Mistral vision."""
    prompt: str = "Describe this image."
    # Each image: either a public URL or a data URI (data:image/...;base64,...)
    images: list[str] = Field(default_factory=list)

class VisionResponse(BaseModel):
    text: str
    conversation_id: Optional[str] = None
