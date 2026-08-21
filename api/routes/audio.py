"""
Audio endpoints — OpenAI-compatible + Mistral-native.

POST /v1/audio/speech          — TTS (text → WAV audio, OpenAI-compatible)
POST /v1/audio/transcriptions  — STT (audio → text, OpenAI Whisper-compatible)
POST /v1/audio/speak           — Mistral-native: read-aloud existing message by IDs

TTS discovery (APK F78399 streamTtsAudio):
  GET /api/read-aloud?chatId=...&messageId=...&messageVersion=...
  Auth: Authorization: Bearer <token>
  Response: binary stream, raw PCM float32 @ 24kHz mono (4 bytes/sample)
  Header X-Estimated-Duration: ISO 8601 duration

Transcription discovery (APK F78468 transcribeAudio, F63427):
  1. POST /api/trpc/file.uploadFile?batch=1  {type:"audio",count:1}
     → {uploadURLs:[presigned_sas_url]}
  2. PUT audio bytes to presigned URL (no Authorization header)
  3. POST /api/trpc/message.transcribeFromSignedUrl?batch=1
     {audioUrl: sas_url, recordingId: uuid}
     → {transcript, correlationId, recordingId, url}
"""
from __future__ import annotations

import io
import json
import mimetypes
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from api.client import get_client

router = APIRouter(tags=["audio"])

BASE_URL = "https://chat.mistral.ai"


# ── Schemas ────────────────────────────────────────────────────────────────

class AudioSpeechRequest(BaseModel):
    model: str = "mistral-tts-1"
    input: str
    voice: str = "alloy"
    response_format: str = "mp3"
    speed: float = 1.0


class AudioTranscriptionResponse(BaseModel):
    text: str


class ReadAloudRequest(BaseModel):
    chatId: str
    messageId: str
    messageVersion: str


# ── Helpers ─────────────────────────────────────────────────────────────────

# ── El contrato de audio, compartido por los cinco proxies ───────────────────
# 4096 es el límite de la API de OpenAI y el que perplexity-proxy ya declaraba.
# Se comprueba ANTES de llamar al backend, lo que acá vale doble: la síntesis de
# mistral crea un chat de verdad, así que una petición demasiado larga gastaba
# uno de los 40 `message_send` de la cuenta para después fallar.
MAX_INPUT_CHARS = 4096

# MP3 es el formato por defecto en los cinco proxies. Este devolvía WAV, que es
# el mismo audio a ~4x el tamaño (540 KB contra 129-165 KB medidos sobre la
# misma frase), y obligaba a cualquier cliente a tratar a mistral distinto --
# que es justo lo que el gateway delante existe para evitar.
SUPPORTED_FORMATS = ("mp3", "wav")
MP3_BITRATE_KBPS = 128
SAMPLE_RATE = 24000


def _pcm_float32_to_mp3(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Encode raw float32 PCM straight to MP3.

    Encoded from the PCM rather than from the WAV on purpose: the WAV is only a
    container this proxy builds itself, so going through it would mean writing a
    header just to parse it back. lameenc is a pip wheel with LAME bundled --
    no ffmpeg, no apt package, nothing new in the image beyond the wheel.
    """
    import lameenc

    n_samples = len(pcm_bytes) // 4
    if n_samples == 0:
        raise HTTPException(status_code=502, detail="Empty audio response from Mistral")
    samples_f32 = struct.unpack(f"<{n_samples}f", pcm_bytes[:n_samples * 4])
    samples_i16 = struct.pack(
        f"<{n_samples}h",
        *[max(-32768, min(32767, int(s * 32767))) for s in samples_f32],
    )

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(MP3_BITRATE_KBPS)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)          # 0 = best/slowest, 9 = worst/fastest
    return bytes(encoder.encode(samples_i16)) + bytes(encoder.flush())


def _pcm_float32_to_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """Convert raw float32 PCM from /api/read-aloud to standard 16-bit PCM WAV."""
    n_samples = len(pcm_bytes) // 4
    if n_samples == 0:
        raise HTTPException(status_code=502, detail="Empty audio response from Mistral")
    samples_f32 = struct.unpack(f"<{n_samples}f", pcm_bytes[:n_samples * 4])
    samples_i16 = struct.pack(
        f"<{n_samples}h",
        *[max(-32768, min(32767, int(s * 32767))) for s in samples_f32],
    )
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(samples_i16)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size,
        b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits,
        b"data", data_size,
    )
    return header + samples_i16


def _fetch_read_aloud(client, chat_id: str, message_id: str, message_version: str) -> bytes:
    """
    Call GET /api/read-aloud and return raw PCM float32 bytes.
    APK F78399: uses standard Authorization: Bearer header (getAuthHeaders).
    """
    import urllib.parse
    params = urllib.parse.urlencode({
        "chatId": chat_id,
        "messageId": message_id,
        "messageVersion": message_version,
    })
    resp = client.session.get(
        f"{BASE_URL}/api/read-aloud?{params}",
        headers={"Accept": "audio/*, */*"},
        timeout=60,
        stream=True,
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"read-aloud HTTP {resp.status_code}: {resp.text[:200]}")
    pcm = b""
    for chunk in resp.iter_content(chunk_size=None):
        pcm += chunk
    return pcm


def _create_tts_chat(client, text: str) -> tuple[str, str, str]:
    """
    Create a chat that echoes the text, stream it, return (chatId, messageId, messageVersion).
    The prompt strongly encourages verbatim repetition so TTS audio matches the input text.
    """
    prompt = (
        "Output ONLY the following text, verbatim, with zero additions, commentary, or changes:\n\n"
        + text
    )
    trpc_input = {
        "files": [],
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
        f"{BASE_URL}/api/trpc/message.newChat?batch=1",
        json={"0": {"json": trpc_input}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"tRPC newChat failed: {r.text[:200]}")

    rdata = r.json()[0]["result"]["data"]["json"]
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
        "features": [],
        "libraries": [],
        "integrations": [],
        "disabledFeatures": ["memory-inference"],
    }
    resp = client.session.post(
        f"{BASE_URL}/api/chat",
        json=body,
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        timeout=120,
        stream=True,
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"api/chat failed: {resp.status_code}")

    # Parse stream to capture messageId + messageVersion
    message_id: Optional[str] = None
    message_version: Optional[str] = None

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
            raise HTTPException(status_code=502, detail=f"Stream error: {msg}")

        if j.get("type") == "message":
            mid = j.get("messageId")
            mver = j.get("messageVersion")
            if not mid:
                continue
            for patch in j.get("patches", []):
                val = patch.get("value")
                if patch.get("op") == "replace" and patch.get("path") == "/":
                    if isinstance(val, dict) and val.get("role") == "assistant":
                        message_id = mid
                        message_version = str(mver) if mver is not None else None

    if not message_id or message_version is None:
        raise HTTPException(status_code=502, detail="Could not capture message IDs from stream")

    return chat_id, message_id, message_version


def _upload_audio_to_blob(client, audio_bytes: bytes, mime_type: str) -> str:
    """
    Upload audio to Azure Blob via file.uploadFile tRPC (type:"audio").
    Returns the full presigned SAS URL (required by transcribeFromSignedUrl).
    """
    r = client.session.post(
        f"{BASE_URL}/api/trpc/file.uploadFile?batch=1",
        json={"0": {"json": {"type": "audio", "count": 1}}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"file.uploadFile (audio) failed: {r.text[:200]}")

    upload_url = r.json()[0]["result"]["data"]["json"]["uploadURLs"][0]

    import urllib.request, urllib.error
    req = urllib.request.Request(
        upload_url,
        data=audio_bytes,
        method="PUT",
        headers={"Content-Type": mime_type, "x-ms-blob-type": "BlockBlob"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Blob PUT (audio) failed: {e.code} {e.reason}")
    if status not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Blob PUT (audio) unexpected status: {status}")

    return upload_url  # full SAS URL for transcribeFromSignedUrl


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/v1/audio/voices")
def list_voices():
    """What this proxy accepts for text-to-speech. Standard across the five.

    `voices` is EMPTY here, and that is the truth rather than an omission:
    Mistral's read-aloud backend takes no voice at all. The `voice` field on
    the request is accepted for OpenAI compatibility and ignored -- which is
    why `alloy` appeared to "work" on this proxy while it broke two others.
    `selection: "none"` says so in a way a client can branch on.
    """
    return {
        "default": None,
        "voices": [],
        "openai_aliases": {},
        "selection": "none",
        "max_input_chars": MAX_INPUT_CHARS,
        "formats": list(SUPPORTED_FORMATS),
        "default_format": "mp3",
    }


@router.post("/v1/audio/speech")
def create_speech(req: AudioSpeechRequest):
    """
    OpenAI TTS-compatible endpoint.

    Internally: creates a chat that echoes the input text verbatim, streams it,
    then calls /api/read-aloud to get audio. Returns WAV (PCM 16-bit, 24kHz mono).

    Note: response_format is accepted but always returns WAV (PCM 16-bit, 24kHz mono).
    The underlying Mistral /api/read-aloud returns raw float32 PCM which is converted here.
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")

    if not req.input.strip():
        raise HTTPException(status_code=400, detail="input text is empty")

    # Checked BEFORE _create_tts_chat, which is the call that spends one of the
    # account's 40 message_send points. Rejecting afterwards would charge the
    # user for a request that was never going to work.
    if len(req.input) > MAX_INPUT_CHARS:
        raise HTTPException(status_code=400, detail={"error": {
            "type": "invalid_request_error",
            "message": f"input is {len(req.input)} characters, the limit is {MAX_INPUT_CHARS}",
            "param": "input",
            "max_input_chars": MAX_INPUT_CHARS}})

    fmt = (req.response_format or "mp3").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail={"error": {
            "type": "invalid_request_error",
            "message": f"unsupported response_format '{req.response_format}'",
            "param": "response_format",
            "supported": list(SUPPORTED_FORMATS)}})

    try:
        chat_id, message_id, message_version = _create_tts_chat(client, req.input)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create TTS chat: {e}")

    try:
        pcm = _fetch_read_aloud(client, chat_id, message_id, message_version)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"read-aloud failed: {e}")

    if fmt == "wav":
        audio, media_type, filename = _pcm_float32_to_wav(pcm), "audio/wav", "speech.wav"
    else:
        audio, media_type, filename = _pcm_float32_to_mp3(pcm), "audio/mpeg", "speech.mp3"
    return Response(
        content=audio,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Chat-Id": chat_id,
            "X-Message-Id": message_id,
            "X-Message-Version": message_version,
        },
    )


@router.post("/v1/audio/transcriptions", response_model=AudioTranscriptionResponse)
async def create_transcription(
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    language: Optional[str] = Form(default=None),
    prompt: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
):
    """
    OpenAI Whisper-compatible transcription endpoint.

    Internally:
      1. Upload audio to Mistral's Azure Blob (type: "audio") via file.uploadFile.
      2. Call message.transcribeFromSignedUrl tRPC with the presigned SAS URL.
      3. Return {text: transcript} (OpenAI format).
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="audio file is empty")

    mime_type = file.content_type or mimetypes.guess_type(file.filename or "audio.m4a")[0] or "audio/m4a"

    try:
        sas_url = _upload_audio_to_blob(client, audio_bytes, mime_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Audio upload failed: {e}")

    recording_id = str(uuid.uuid4())
    r = client.session.post(
        f"{BASE_URL}/api/trpc/message.transcribeFromSignedUrl?batch=1",
        json={"0": {"json": {"audioUrl": sas_url, "recordingId": recording_id}}},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"transcribeFromSignedUrl failed: {r.status_code} {r.text[:300]}")

    result = r.json()[0]["result"]["data"]["json"]
    transcript = result.get("transcript", "")

    return AudioTranscriptionResponse(text=transcript)


@router.post("/v1/audio/speak")
def read_aloud_message(req: ReadAloudRequest):
    """
    Mistral-native TTS: read aloud an existing chat message by its IDs.

    Unlike /v1/audio/speech, this reads the actual stored message without
    creating a new chat. Useful when you already have a chatId + messageId
    from a previous conversation.

    Returns WAV audio (PCM 16-bit, 24kHz mono).
    """
    client = get_client()
    if not client.session_token:
        raise HTTPException(status_code=401, detail="Not authenticated. POST /v1/auth/login first.")

    try:
        pcm = _fetch_read_aloud(client, req.chatId, req.messageId, req.messageVersion)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"read-aloud failed: {e}")

    wav = _pcm_float32_to_wav(pcm)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'attachment; filename="speech.wav"',
        },
    )
