"""
GET /v1/models — dynamic list with static fallback.

Discovery strategy (APK analysis):
- No tRPC or REST model-list endpoint exists on chat.mistral.ai for session tokens.
- The model list is embedded in the app's JS bundle (static).
- If MISTRAL_API_KEY is set, we fetch live from api.mistral.ai/v1/models.
- Otherwise we return the hardcoded list extracted from the APK string table.
"""
from __future__ import annotations

import os
import time
from typing import Optional
from fastapi import APIRouter
from api.schemas import ModelCard, ModelList

router = APIRouter(tags=["models"])

_FALLBACK_MODELS = [
    ModelCard(id="mistral-small-latest",   description="Mistral Small — fast and efficient"),
    ModelCard(id="mistral-medium-latest",  description="Mistral Medium 3.5 — balanced (default for chat)"),
    ModelCard(id="mistral-large-latest",   description="Mistral Large — most capable"),
    ModelCard(id="codestral-latest",       description="Codestral — code specialist"),
    ModelCard(id="pixtral-large-latest",   description="Pixtral Large — vision + text"),
    ModelCard(id="mistral-nemo",           description="Mistral Nemo — lightweight"),
    ModelCard(id="open-mistral-7b",        description="Open Mistral 7B"),
    ModelCard(id="open-mixtral-8x7b",      description="Open Mixtral 8x7B"),
    ModelCard(id="open-mixtral-8x22b",     description="Open Mixtral 8x22B"),
    ModelCard(id="open-codestral-mamba",   description="Open Codestral Mamba"),
]

_cache: dict = {"models": None, "expires": 0}
_CACHE_TTL = 3600  # 1 hour


def _fetch_from_api(api_key: str) -> Optional[list[ModelCard]]:
    """Fetch live model list from api.mistral.ai using an API key."""
    try:
        from api.client import get_client
        client = get_client()
        r = client.session.get(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        cards = []
        for m in data.get("data", []):
            cards.append(ModelCard(
                id=m["id"],
                description=m.get("description", ""),
                owned_by=m.get("owned_by", "mistral"),
                created=m.get("created", int(time.time())),
            ))
        return cards or None
    except Exception:
        return None


def _get_models() -> list[ModelCard]:
    now = int(time.time())
    if _cache["models"] and now < _cache["expires"]:
        return _cache["models"]

    api_key = os.getenv("MISTRAL_API_KEY", "")
    if api_key:
        models = _fetch_from_api(api_key)
        if models:
            _cache["models"] = models
            _cache["expires"] = now + _CACHE_TTL
            return models

    # Static fallback — no expiry needed
    return _FALLBACK_MODELS


@router.get("/v1/models", response_model=ModelList)
def list_models():
    now = int(time.time())
    models = [m.model_copy(update={"created": m.created or now}) for m in _get_models()]
    return ModelList(data=models)


@router.get("/v1/models/{model_id}", response_model=ModelCard)
def get_model(model_id: str):
    from fastapi import HTTPException
    for m in _get_models():
        if m.id == model_id:
            return m.model_copy(update={"created": m.created or int(time.time())})
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
