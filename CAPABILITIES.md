# Capacidades de mistral-proxy

Este documento acompaña al contrato que publica `GET /health`
(llm-libre, `docs/superpowers/specs/2026-08-20-proxy-capability-contract-design.md`).
El código vive en [`api/capabilities.py`](api/capabilities.py); acá está el
**porqué** de cada booleano.

**La regla:** un booleano dice qué **lograría** una petición mandada ahora, ya
resuelta contra la cuenta — no qué implementa este código. Y no sigue al
medidor: una cuota agotada es un 429 con cooldown, que se recupera solo, y nunca
apaga una capacidad.

## La tabla

| Capacidad | Con cuenta | Anónimo | Por qué |
|---|:--:|:--:|---|
| `chat` | ✅ | ✅ | **Medido sin credenciales.** Ver abajo — mistral es el único de los cinco proxies del que esto es cierto. |
| `streaming` | ✅ | ✅ | Lo mismo, y por construcción: `chat()` itera `send_message()`, un generador que emite tokens según llegan. |
| `tools` | ❌ | ❌ | No hay function calling; nada emite `tool_calls`. La emulación por inyección de prompt vive en el **gateway** (`emulates_tools`) — reportar `true` acá sería atribuirse trabajo ajeno. |
| `vision` | ✅ | ❌ | `api/routes/chat.py` extrae las partes `image_url` y las sube al blob storage de Mistral (`upload_image_to_blob`). |
| `images` | ✅ | ❌ | `/v1/images/generations`. |
| `audio_speech` | ✅ | ❌ | `/v1/audio/speech`. |
| `audio_transcription` | ✅ | ❌ | `/v1/audio/transcriptions`. |
| `translate` | ❌ | ❌ | No existe ruta `/v1/translate`. |
| `search` | ✅ | ❌ | `/v1/search`. |
| `files` | ❌ | ❌ | No existe `/v1/files*`. Las rutas `/v1/code/sessions/{id}/files` son de la función de sesiones de código, no un API de archivos general; mapearlas acá prometería otra cosa. |
| `conversations` | ✅ | ❌ | Este proxy es **la forma de referencia** — ver abajo. |

Ocho de once con cuenta. Los tres `❌` permanentes necesitan código nuevo, no una
cuenta mejor.

## El chat anónimo

mistral es **el único de los cinco proxies cuyo chat funciona sin cuenta**, y no
es una suposición sacada del nombre del módulo: con `MISTRAL_SESSION_TOKEN`,
`MISTRAL_EMAIL` y `MISTRAL_PASSWORD` borradas del entorno, un
`MistralAnonChat()` con solo `bootstrap_session()` respondió a un prompt.

Por eso `effective()` deja `chat` y `streaming` en `true` en los dos modos —
una asimetría deliberada respecto de los otros cuatro proxies, y la aserción que
hay que revisar si alguien intenta "unificar" este módulo con ellos.

**Límite honesto del modo anónimo:** solo `chat` y `streaming` se midieron sin
credenciales. Las seis capacidades atadas a la cuenta se reportan `false` en
anónimo porque **no se midieron ahí**, no porque una medición las haya visto
fallar. La dirección del error es deliberada — subdeclarar le cuesta al gateway
una ruta que podría haber usado, sobredeclarar le cuesta al usuario una petición
rota — y la instancia desplegada tiene credenciales, así que hoy esa rama es
teoría. Medir antes de aflojarla.

## Conversaciones: la forma de referencia

`/v1/conversations`, `/{id}`, `/{id}/messages`, `/{id}/search`, más pin, rename,
delete y cancel. El historial vive en los servidores de Mistral (tRPC
`chat.last` / `chat.byId` / `message.all`), sin almacenamiento local.

Esta es la superficie que los demás proxies copian: la implementación de
**perplexity-proxy** replica esta forma de respuesta a propósito
(`ConversationItem` / `ConversationList` / `ConversationMessages`), para que el
gateway lea una sola forma en los cinco.

## `GET /health`

```json
{
  "status": "ok",
  "authenticated": true,
  "version": "1.0.0",
  "contract": 1,
  "provider": "mistral",
  "auth": {"mode": "account", "plan": null,
           "subscription_active": false, "expires_at": null},
  "capabilities": { ... los once booleanos ... }
}
```

**Un cambio real de comportamiento, no solo campos nuevos:** este handler
llamaba a `get_client()`, que en un contenedor frío ejecuta `_authenticate()` y
`bootstrap_session()` — dos viajes a Mistral. Un `/health` que necesita al
vendor no puede responder cuando el vendor está caído, que es exactamente el
momento en que el sweep del gateway y el healthcheck del contenedor necesitan
una respuesta. Ahora es una lectura local de variables de entorno y nada más;
hay un test que lo fija (`test_snapshot_makes_no_vendor_call`).

`status` y `authenticated` se mantuvieron: eran toda la respuesta antes del
contrato, y lo que ya apuntara acá no debe romperse. `authenticated` ahora sale
de la misma lectura local que el resto.

`plan` es `null` a propósito. Mistral **sí** vende Pro, y este proxy hasta tiene
una ruta `/v1/billing/pro`, pero llamarla es un viaje al vendor y `/health` lo
tiene prohibido. Poner `"free"` sin preguntar sería la clase de mentira que este
contrato existe para terminar.

## Dónde vive `capabilities.py`, y por qué importa

En `api/`, **no** en la raíz del repo. El Dockerfile lista módulos por nombre
(`COPY mistral_anon_chat.py .`) en vez de copiar el árbol, así que un módulo
nuevo en la raíz **no se embarca** y el contenedor muere al importar en el
arranque. Ese error exacto tumbó a chatgpt-proxy por diez minutos el
2026-08-20. `COPY api/ ./api/` copia este directorio entero.

Relacionado, y capaz de arruinar un test: importar cualquier cosa de `api`
arrastra `mistral_anon_chat`, que **como efecto de importación** escribe `.env`
dentro de `os.environ` (`mistral_anon_chat.py:56`). Un test que borre esas
variables y después importe algo de `api` se las repone solo.

## Lo que falta (§3.4)

La spec exige que un endpoint cuya capacidad es `false` responda **`501 Not
Implemented`**, no `404`. Este proxy **todavía no**: `/v1/translate` y
`/v1/files*` no existen como rutas, así que FastAPI devuelve su `404` genérico.
Los booleanos ya son correctos; el código de estado todavía no cumple la letra
de §3.4. Cerrarlo es trabajo aparte, igual que en perplexity y deepseek.
