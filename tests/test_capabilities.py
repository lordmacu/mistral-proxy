"""Tests del contrato de capacidades.

Todo acá es puro: nada toca la red ni al vendor. Eso es parte de lo que se
está probando -- `/health` no puede depender de Mistral, porque el momento en
que el gateway más necesita una respuesta es justo cuando Mistral está caído.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# `api.client` se importa ACÁ ARRIBA a propósito, aunque solo lo use un test.
# Importar cualquier cosa de `api` arrastra `mistral_anon_chat`, que como EFECTO
# DE IMPORTACIÓN escribe `.env` dentro de `os.environ` (mistral_anon_chat.py:56).
# Si esa importación ocurre dentro de un test, le repone las credenciales que el
# fixture acaba de borrar y el test mide lo contrario de lo que cree medir.
import api.client  # noqa: E402,F401
from api import capabilities  # noqa: E402


ACCOUNT = capabilities.SessionState(mode="account")
ANON = capabilities.SessionState(mode="anonymous")


def test_effective_declares_exactly_the_eleven_capabilities():
    assert set(capabilities.effective(ACCOUNT)) == set(capabilities.REQUIRED_CAPABILITIES)


def test_every_capability_is_a_bool():
    assert all(isinstance(v, bool) for v in capabilities.effective(ACCOUNT).values())


def test_an_account_gains_exactly_the_eight():
    assert {k for k, v in capabilities.effective(ACCOUNT).items() if v} == {
        "chat", "streaming", "vision", "images",
        "audio_speech", "audio_transcription", "search", "conversations",
    }


def test_chat_survives_without_an_account():
    """Medido: con el entorno sin credenciales, un cliente anónimo respondió.

    mistral es el único de los cinco proxies del que esto es cierto, así que es
    la aserción que hay que revisar si alguien "unifica" este módulo con los
    otros cuatro.
    """
    anon = capabilities.effective(ANON)
    assert anon["chat"] is True
    assert anon["streaming"] is True


def test_anonymous_loses_everything_that_needs_the_account():
    anon = capabilities.effective(ANON)
    assert not any(anon[k] for k in
                   ("vision", "images", "audio_speech", "audio_transcription",
                    "search", "conversations"))


@pytest.mark.parametrize("cap", ["tools", "translate", "files"])
def test_the_three_that_no_account_can_turn_on(cap):
    """Necesitan código nuevo, no una cuenta mejor."""
    assert capabilities.effective(ACCOUNT)[cap] is False
    assert capabilities.effective(ANON)[cap] is False


def test_auth_block_reports_plan_as_none_not_a_guess():
    assert capabilities.auth_block(ACCOUNT) == {
        "mode": "account", "plan": None,
        "subscription_active": False, "expires_at": None,
    }


# ── snapshot(): lectura local, nunca una llamada al vendor ────────────────────

@pytest.fixture
def clean_env(monkeypatch):
    for k in ("MISTRAL_SESSION_TOKEN", "MISTRAL_EMAIL", "MISTRAL_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


def test_no_credentials_is_anonymous(clean_env):
    assert capabilities.snapshot().mode == "anonymous"


def test_a_session_token_alone_is_an_account(clean_env, monkeypatch):
    monkeypatch.setenv("MISTRAL_SESSION_TOKEN", "ory_st_whatever")
    assert capabilities.snapshot().mode == "account"


def test_email_and_password_are_an_account(clean_env, monkeypatch):
    """`_authenticate` acepta esta forma, así que el contrato también."""
    monkeypatch.setenv("MISTRAL_EMAIL", "a@b.test")
    monkeypatch.setenv("MISTRAL_PASSWORD", "hunter2")
    assert capabilities.snapshot().mode == "account"


def test_an_email_without_a_password_is_not_an_account(clean_env, monkeypatch):
    monkeypatch.setenv("MISTRAL_EMAIL", "a@b.test")
    assert capabilities.snapshot().mode == "anonymous"


def test_whitespace_is_not_a_credential(clean_env, monkeypatch):
    monkeypatch.setenv("MISTRAL_SESSION_TOKEN", "   ")
    assert capabilities.snapshot().mode == "anonymous"


def test_snapshot_makes_no_vendor_call(clean_env, monkeypatch):
    """Si `snapshot()` alguna vez llama a `get_client()`, este test lo caza.

    Es la regresión que importa: el `/health` viejo SÍ lo llamaba, y por eso no
    podía responder con Mistral caído.
    """
    def explode(*a, **k):
        raise AssertionError("snapshot() reached the vendor")

    monkeypatch.setattr(api.client, "get_client", explode)
    assert capabilities.snapshot().mode == "anonymous"


# ── El gate de §3.4 ───────────────────────────────────────────────────────────

def test_a_false_capability_answers_501_not_404():
    """404 es indistinguible de un error de ruteo, y 503 hace que el gateway
    reintente algo que nunca iba a funcionar en esta configuración."""
    from fastapi.testclient import TestClient
    from api.main import app

    client = TestClient(app)
    for method, path in (("post", "/v1/translate"),
                         ("post", "/v1/files"),
                         ("get", "/v1/files"),
                         ("get", "/v1/files/abc"),
                         ("delete", "/v1/files/abc")):
        assert getattr(client, method)(path).status_code == 501, path


def test_the_gate_names_the_capability_and_where_to_look():
    from fastapi.testclient import TestClient
    from api.main import app

    detail = TestClient(app).post("/v1/translate").json()["detail"]
    assert "translate" in detail and "/health" in detail


def test_require_passes_for_a_capability_this_proxy_has(monkeypatch):
    monkeypatch.setattr(capabilities, "snapshot", lambda: ACCOUNT)
    capabilities.require("conversations")   # no raise


def test_require_refuses_an_account_capability_when_anonymous(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(capabilities, "snapshot", lambda: ANON)
    with pytest.raises(HTTPException) as exc:
        capabilities.require("conversations")
    assert exc.value.status_code == 501


def test_chat_is_never_gated_off(monkeypatch):
    """mistral responde sin cuenta, así que `require("chat")` debe pasar incluso
    en anónimo — el gate no puede apagar lo único que anda siempre."""
    monkeypatch.setattr(capabilities, "snapshot", lambda: ANON)
    capabilities.require("chat")   # no raise


# ── El contrato de audio, común a los cinco proxies ───────────────────────────

def test_the_voices_endpoint_publishes_the_limits():
    """Antes no había forma de preguntar ni las voces ni el máximo de caracteres,
    que es exactamente por qué se mandó una voz inválida."""
    from fastapi.testclient import TestClient
    from api.main import app

    body = TestClient(app).get("/v1/audio/voices").json()
    assert body["max_input_chars"] == 4096
    assert body["default_format"] == "mp3"
    assert "mp3" in body["formats"]


def test_this_backend_admits_it_has_no_voice_selection():
    """`alloy` parecía funcionar acá sólo porque el parámetro se descarta."""
    from fastapi.testclient import TestClient
    from api.main import app

    body = TestClient(app).get("/v1/audio/voices").json()
    assert body["selection"] == "none"
    assert body["voices"] == []


def test_mp3_is_the_default_format():
    from api.routes.audio import AudioSpeechRequest

    assert AudioSpeechRequest(input="hola").response_format == "mp3"


def test_the_encoder_produces_a_real_mp3_frame():
    import math
    import struct

    from api.routes.audio import SAMPLE_RATE, _pcm_float32_to_mp3

    pcm = b"".join(struct.pack("<f", 0.3 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
                   for i in range(SAMPLE_RATE // 4))
    mp3 = _pcm_float32_to_mp3(pcm)
    assert mp3[:2] == b"\xff\xf3"       # MPEG frame sync
    assert len(mp3) > 1000


def test_mp3_is_much_smaller_than_the_wav_it_replaces():
    """El motivo del cambio: mismo audio, ~4x el tamaño medido sobre la misma frase."""
    import math
    import struct

    from api.routes.audio import SAMPLE_RATE, _pcm_float32_to_mp3, _pcm_float32_to_wav

    pcm = b"".join(struct.pack("<f", 0.3 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
                   for i in range(SAMPLE_RATE // 2))
    assert len(_pcm_float32_to_mp3(pcm)) * 2 < len(_pcm_float32_to_wav(pcm))
