# Handoff — Mistral APK anonymous chat

**Objetivo**: Script Python que hace chat anónimo contra `chat.mistral.ai` sin cuenta, replicando el APK `ai.mistral.chat v2.8.0`.

**REGLA DURA**: Solo usar información del APK en `/Users/cristian/mistral/jadx-out/`. No mirar la web.

**ESTADO: COMPLETADO** ✓ El script funciona.

---

## Herramientas disponibles

```bash
BUNDLE=/Users/cristian/mistral/jadx-out/resources/assets/index.android.bundle
TOOL=/Users/cristian/mistral/hermes-decomp/target/release/hermes-decomp

"$TOOL" xref --query "STRING" "$BUNDLE"           # buscar string
"$TOOL" xref --query N --kind function "$BUNDLE"  # buscar quién llama a función N
"$TOOL" decompile --function N --expand --resolve-closures -o out.js "$BUNDLE"
"$TOOL" closures --function N "$BUNDLE"            # ver qué son los closure_X
"$TOOL" info "$BUNDLE"                             # info del bundle
```

---

## Flujo completo (implementado)

```
1. Bootstrap sesión anónima:
   GET https://auth.mistral.ai/self-service/registration/api
   → cookies: __cf_bm, __cflb (Cloudflare)

2. Crear chat vía tRPC message.newChat:
   POST https://chat.mistral.ai/api/trpc/message.newChat?batch=1
   Content-Type: application/json
   Body: {"0": {"json": {
     "files": [],
     "content": [{"type": "text", "text": "<mensaje>"}],
     "transcriptionsMetadata": [],
     "features": [],
     "integrations": [],
     "libraries": [],
     "productType": "chat",    ← servidor valida "chat"|"work" (no "le-chat")
     "projectId": null,
     "incognito": false,
     // NO incluir agentId ni agentsApiAgentId (undefined en JS, null falla validación)
   }}}
   Respuesta: [{"result":{"data":{"json":{"chatId": "<uuid>", "messages": {...}}}}}]

3. Iniciar streaming:
   POST https://chat.mistral.ai/api/chat
   Accept: text/event-stream
   Content-Type: application/json
   Body: {
     "mode": "start",
     "chatId": "<uuid-del-paso-2>",
     "stableAnonymousIdentifier": "<uuid-local>",
     "platform": "mobile",
     "clientPromptData": {"currentDate": "2026-08-18T09:26:00.000Z"},
     "supportedTaskCallbacks": [],   ← array, no null
     "features": [],
     "libraries": [],
     "integrations": [],
     "disabledFeatures": ["memory-inference"],
   }

4. Parsear SSE:
   Formato: <tipo>:<json>\n
   Tipo 15 → datos (mensajes con patches JSON)
   Tipo 16 → metadata (disclaimers)
   Tipo 6  → error
   Tipo 8  → null (end of stream)
```

---

## AppUserAgent (F4601)

```
le-chat-mobile/2.8.0 (build:20800191; os_name:android; device_category:smartphone; device_model:unknown; device_manufacturer:unknown)
```

- `nativeApplicationVersion = "2.8.0"` (versionName en AndroidManifest.xml)
- `nativeBuildVersion = "20800191"` (versionCode en AndroidManifest.xml)

---

## Funciones clave decompiladas

| Función | Nombre | Descripción |
|---------|--------|-------------|
| F4601 | AppUserAgent | Construye el User-Agent de la app |
| F33188 | createNewChatContentParams | Crea el array `content` para tRPC |
| F33184 | createNewChatFromUserVariables | Construye el input completo de tRPC |
| F91373 | (mutationFn) | Llama `tRPCClient.message.newChat.mutate(input, {signal})` |
| F33113 | useNewChat | Hook React que orquesta la creación de chat |
| F33102 | _temp | onSuccess: navega a `/chat/[id]` con `arg0.chatId` |
| F62487 | t3 | Construye el body de POST /api/chat según `mode` |
| F77931 | postChatRequest | POST /api/chat |
| F33259 | getAuthHeaders | Headers: UA, Accept-Language, sin Bearer para anónimos |
| F5186 | trpcClient | Configura tRPC con httpBatchStreamLink en /api/trpc |

---

## Script

`/Users/cristian/mistral/mistral_anon_chat.py`

```bash
python3 mistral_anon_chat.py "Tu mensaje"
python3 mistral_anon_chat.py --debug "Hola"
python3 mistral_anon_chat.py --interactive
```

---

## Login con email/contraseña (implementado)

Flujo Ory Kratos nativo (F82068 = mutationFn del login):

```
1. GET https://auth.mistral.ai/self-service/login/api
   Accept: application/json
   → {id: "<flow_id>", ui: {nodes: [...]}}
   El csrf_token está vacío en flows nativos API (no CSRF para mobile)

2. POST https://auth.mistral.ai/self-service/login?flow=<flow_id>
   Content-Type: application/json
   Body: {
     "method": "password",
     "identifier": "email@example.com",
     "password": "contraseña",
     "csrf_token": ""
   }
   → {session_token: "<token>", session: {...}}

3. Usar en todas las requests:
   Authorization: Bearer <session_token>
```

Errores de Kratos: en `response.ui.messages[].text` y `response.ui.nodes[].messages[].text`

```bash
# CLI con login:
python3 mistral_anon_chat.py --email user@example.com --password "pass" "¿Qué puedes hacer?"
```

**getAuthHeaders (F33259)**: con sessionToken → `Authorization: Bearer <token>`, sin token → anónimo (sin Authorization).

---

## Modo append (conversación seguida)

Para seguir en el mismo chat (aún no probado con el script):
```json
{
  "mode": "append",
  "chatId": "<uuid>",
  "messageId": "<nuevo-uuid>",
  "messageInput": [{"type": "text", "text": "<mensaje>"}]
}
```
