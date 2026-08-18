from fastapi import APIRouter, HTTPException
from api.schemas import LoginRequest, RegisterRequest, AuthResponse
from api.client import get_client, reset_client
import os

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
def login(req: LoginRequest):
    client = get_client()
    try:
        token = client.login(req.email, req.password)
        # Persist token in env so subsequent requests use it
        os.environ["MISTRAL_SESSION_TOKEN"] = token
        reset_client()
        return AuthResponse(token=token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.post("/register", response_model=AuthResponse)
def register(req: RegisterRequest):
    client = get_client()
    try:
        token = client.register(req.email, req.password, req.first_name, req.last_name)
        os.environ["MISTRAL_SESSION_TOKEN"] = token
        reset_client()
        return AuthResponse(token=token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/me")
def me():
    client = get_client()
    token = client.session_token
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"token": token[:20] + "...", "authenticated": True}
