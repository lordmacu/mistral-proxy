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
