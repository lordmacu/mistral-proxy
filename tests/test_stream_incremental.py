"""Tests for the incremental line-based stream parser in mistral_anon_chat.py.

These pin down the fix for the flattening bug: `_parse_stream` used to buffer
the ENTIRE upstream body (via a blocking `for chunk in resp.iter_content(): raw
+= chunk` loop) before parsing a single line, so a client watching an SSE
stream saw the whole answer arrive at once, at the end. It is now
line-incremental, and it decodes with a proper INCREMENTAL UTF-8 decoder
because `iter_content(chunk_size=None)` can split a chunk anywhere -- including
the middle of a multi-byte character -- and Mistral answers in Spanish
constantly, so accented characters are the common case, not an edge case.

No network, no credentials: `resp` is a fake object exposing only
`iter_content(chunk_size=None)`, which is all `_parse_stream` calls on it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mistral_anon_chat import MistralAnonChat, RateLimitError


class FakeResp:
    """Stands in for the `requests`/`curl_cffi` streaming response.

    `_parse_stream` only ever calls `.iter_content(chunk_size=None)` on it,
    so that is the entire surface this fake needs to provide.
    """

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


def _line(line_type: int, payload: dict) -> str:
    """Build one `<type>:<json>` wire line, matching Mistral's real shape."""
    return f"{line_type}:{json.dumps(payload, ensure_ascii=False)}"


def _body_lines(msg_id: str = "m1", tokens: list[str] | None = None) -> list[str]:
    """A synthetic-but-real-shaped stream: a `bootstrap` line, then a
    `replace` patch that starts the assistant message, then one `append`
    patch per token, then an end-of-stream marker (type 8)."""
    tokens = tokens if tokens is not None else ["Hola ", "mundo"]
    lines = [
        _line(15, {"type": "bootstrap", "chat": {"id": "chat-123"}}),
        _line(15, {
            "type": "message", "messageId": msg_id, "messageVersion": "1",
            "patches": [{"op": "replace", "path": "/", "value": {"role": "assistant"}}],
        }),
    ]
    for tok in tokens:
        lines.append(_line(15, {
            "type": "message", "messageId": msg_id,
            "patches": [{"op": "append", "path": "/contentChunks/0/text", "value": tok}],
        }))
    lines.append(_line(8, {"type": "done"}))
    return lines


def _chunk_bytes(data: bytes, size: int) -> list[bytes]:
    """Split `data` into `size`-byte pieces, regardless of character
    boundaries -- exactly what `iter_content(chunk_size=None)` may hand back
    from a real socket."""
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_one_chunk_and_many_chunks_yield_the_same_tokens():
    body = ("\n".join(_body_lines()) + "\n").encode("utf-8")

    whole = list(MistralAnonChat()._parse_stream(FakeResp([body])))
    byte_at_a_time = list(MistralAnonChat()._parse_stream(FakeResp(_chunk_bytes(body, 1))))

    assert whole == ["Hola ", "mundo"]
    assert byte_at_a_time == whole


def test_multibyte_character_split_across_a_chunk_boundary_is_not_corrupted():
    token = "mañana"  # "ñ" is 2 bytes in UTF-8: 0xC3 0xB1
    body = ("\n".join(_body_lines(tokens=[token])) + "\n").encode("utf-8")

    split_at = body.index(token.encode("utf-8")) + 3  # inside "ñ"'s 2-byte encoding
    assert body[split_at - 1] == 0xC3 and body[split_at] == 0xB1, (
        "test setup bug: split must land inside the 'ñ' byte sequence"
    )

    chunks = [body[:split_at], body[split_at:]]
    tokens = list(MistralAnonChat()._parse_stream(FakeResp(chunks)))

    assert tokens == [token]


def test_a_type_6_error_after_tokens_still_raises():
    lines = _body_lines(tokens=["Hola ", "mundo"])
    lines.append(_line(6, {"internalCode": 6200, "retryAfterSeconds": 42, "message": "rate limited"}))
    body = ("\n".join(lines) + "\n").encode("utf-8")

    gen = MistralAnonChat()._parse_stream(FakeResp([body]))
    seen = []
    raised = None
    try:
        for tok in gen:
            seen.append(tok)
    except RateLimitError as e:
        raised = e

    assert seen == ["Hola ", "mundo"]
    assert raised is not None
    assert raised.retry_after == 42
