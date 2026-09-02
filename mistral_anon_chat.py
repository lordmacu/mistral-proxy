#!/usr/bin/env python3
"""
Mistral chat + Vibe Code — reverse-engineered from ai.mistral.chat APK v2.8.0.

=== Chat flow (from Hermes bytecode decompilation) ===
  1. Bootstrap: GET auth.mistral.ai/self-service/registration/api  → Kratos cookies
  2. Create chat: POST /api/trpc/message.newChat?batch=1 (tRPC v11, superjson)
  3. Stream: POST /api/chat {mode: "start", chatId, platform: "mobile", ...}
  4. Follow-up: POST /api/chat {mode: "append", chatId, messageId, messageInput}

=== Vibe Code flow (code-trpc, discovered via APK RE) ===
  1. Mark intro as seen:  POST /api/trpc/featureMetadata.update
  2. Create project:      POST /api/code-trpc/projects.create
  3. Create session:      POST /api/code-trpc/sessions.create  (starts cloud agent)
  4. Subscribe to SSE:    GET  /api/code-trpc/sessions.subscribe?input={"json":{...}}
     Events: JSON-patch stream — op:replace/add/append on session state tree.
  5. Respond to agent:    POST /api/code-trpc/sessions.respondCallback

Key APK sources:
  - F4601 (AppUserAgent), F33184 (createNewChatFromUserVariables),
  - F62487 (t3/POST /api/chat body), F7591 (codeTrpcClient definition),
  - F42324 (useCreateSession), F42581 (useSessionSubscription),
  - F32115 (useListProjects), F42404 (useCreateProject)

Dependencies: pip install curl-cffi
"""

import base64
import codecs
import json
import mimetypes
import os
import sys
import uuid
import argparse
from datetime import datetime, timezone
from typing import Generator, Optional


def _load_dotenv(path: str = ".env"):
    """Load key=value pairs from a .env file into os.environ (no override)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CURL_CFFI = False
    print("[WARN] curl_cffi not installed — Cloudflare will likely block requests.", file=sys.stderr)
    print("       pip install curl-cffi", file=sys.stderr)

BASE_URL = "https://chat.mistral.ai"
AUTH_URL = "https://auth.mistral.ai"

# AppUserAgent (F4601): "le-chat-mobile/" + nativeApplicationVersion + " (" + items.join("; ") + ")"
# versionName=2.8.0, versionCode=20800191 (from AndroidManifest.xml)
APP_UA = (
    "le-chat-mobile/2.8.0 "
    "(build:20800191; os_name:android; device_category:smartphone; "
    "device_model:unknown; device_manufacturer:unknown)"
)

DEFAULT_MODEL = "mistral-small-latest"


class RateLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s (~{retry_after//3600}h)")


class MistralAnonChat:
    """
    Anonymous Mistral chat client replicating the APK's request flow.

    No Authorization header — uses Ory Kratos + Cloudflare cookies only.
    """

    def __init__(self, debug: bool = False):
        self.debug = debug
        if HAS_CURL_CFFI:
            self.session = cffi_requests.Session(impersonate="chrome131")
        else:
            self.session = cffi_requests.Session()

        self.session.headers.update({
            "User-Agent": APP_UA,
            "Accept-Language": "en-US",
        })

        self.chat_id: Optional[str] = None
        self.last_assistant_msg_id: Optional[str] = None
        self.last_assistant_msg_version: Optional[str] = None
        self.session_token: Optional[str] = None
        # Limits.stableAnonymousId — persistent anonymous device ID
        self.stable_anon_id: str = str(uuid.uuid4())
        self._bootstrapped = False

    def _log(self, msg: str):
        if self.debug:
            print(f"[DEBUG] {msg}", file=sys.stderr)

    def bootstrap_session(self):
        """Warm up Cloudflare + Kratos cookies (fetchWithAnonymousCookieBootstrap)."""
        if self._bootstrapped:
            return
        self._log("Bootstrapping Kratos session...")
        try:
            r = self.session.get(
                f"{AUTH_URL}/self-service/registration/api",
                headers={"Accept": "application/json"},
                timeout=15,
            )
            self._log(f"Kratos: {r.status_code} | cookies: {list(self.session.cookies.keys())}")
        except Exception as e:
            self._log(f"Bootstrap error (non-fatal): {e}")
        self._bootstrapped = True

    def login(self, email: str, password: str) -> str:
        """
        Authenticate with email+password via Ory Kratos native flow (F82068).

        Flow:
          1. GET /self-service/login/api → native flow with {id: flow_id}
          2. POST /self-service/login?flow=<id> with {method: "password", identifier, password, csrf_token: ""}
          → {session_token: "..."} used as Authorization: Bearer for all subsequent requests

        Returns the session_token.
        """
        self.bootstrap_session()

        # Step 1: create native login flow (orySdk.createNativeLoginFlow({}))
        resp = self.session.get(
            f"{AUTH_URL}/self-service/login/api",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Could not create login flow: HTTP {resp.status_code}")

        flow = resp.json()
        flow_id = flow["id"]
        self._log(f"Login flow ID: {flow_id}")

        # Step 2: submit password (orySdk.updateLoginFlow)
        # csrf_token is empty for native API flows (no browser CSRF needed)
        resp2 = self.session.post(
            f"{AUTH_URL}/self-service/login?flow={flow_id}",
            json={
                "method": "password",
                "identifier": email,
                "password": password,
                "csrf_token": "",
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )

        self._log(f"Login submit status: {resp2.status_code}")

        if resp2.status_code not in (200, 201):
            # Extract Kratos error messages from ui.nodes / ui.messages
            try:
                err_data = resp2.json()
                errors = []
                for msg in err_data.get("ui", {}).get("messages", []):
                    if msg.get("type") == "error":
                        errors.append(msg.get("text", ""))
                for node in err_data.get("ui", {}).get("nodes", []):
                    for msg in node.get("messages", []):
                        if msg.get("type") == "error":
                            errors.append(msg.get("text", ""))
                if errors:
                    raise RuntimeError(f"Login failed: {'; '.join(errors)}")
            except RuntimeError:
                raise
            except Exception:
                pass
            raise RuntimeError(f"Login HTTP {resp2.status_code}: {resp2.text[:200]}")

        data = resp2.json()
        token = data.get("session_token")
        if not token:
            raise RuntimeError("Login succeeded but no session_token in response")

        self.session_token = token
        # getAuthHeaders (F33259): Authorization: "Bearer " + sessionToken
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._log(f"Logged in — token: {token[:20]}...")
        return token

    def register(
        self,
        email: str,
        password: str,
        first_name: str = "User",
        last_name: str = "",
    ) -> str:
        """
        Register a new account via Ory Kratos native registration flow (F82070).

        Flow:
          1. GET /self-service/registration/api → native flow with {id: flow_id}
          2. POST /self-service/registration?flow=<id> with
             {method:"password", password, traits:{email, name:{first, last}}, csrf_token:""}
          → {session_token: "..."} stores as Authorization: Bearer

        Returns the session_token.
        """
        self.bootstrap_session()

        # Step 1: create native registration flow (F66386: createNativeRegistrationFlow)
        resp = self.session.get(
            f"{AUTH_URL}/self-service/registration/api",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Could not create registration flow: HTTP {resp.status_code}")

        flow = resp.json()
        flow_id = flow["id"]
        self._log(f"Registration flow ID: {flow_id}")

        # Step 2: submit registration (orySdk.updateRegistrationFlow — F82070)
        # traits.email, traits.name.first/last from F82070 closure_0 shape
        resp2 = self.session.post(
            f"{AUTH_URL}/self-service/registration?flow={flow_id}",
            json={
                "method": "password",
                "password": password,
                "traits": {
                    "email": email,
                    "name": {"first": first_name, "last": last_name},
                },
                "csrf_token": "",
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )

        self._log(f"Register submit status: {resp2.status_code}")

        if resp2.status_code not in (200, 201):
            try:
                err_data = resp2.json()
                errors = []
                for msg in err_data.get("ui", {}).get("messages", []):
                    if msg.get("type") == "error":
                        errors.append(msg.get("text", ""))
                for node in err_data.get("ui", {}).get("nodes", []):
                    for msg in node.get("messages", []):
                        if msg.get("type") == "error":
                            errors.append(msg.get("text", ""))
                if errors:
                    raise RuntimeError(f"Registration failed: {'; '.join(errors)}")
            except RuntimeError:
                raise
            except Exception:
                pass
            raise RuntimeError(f"Register HTTP {resp2.status_code}: {resp2.text[:200]}")

        data = resp2.json()
        token = data.get("session_token")
        if not token:
            raise RuntimeError("Registration succeeded but no session_token in response")

        self.session_token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        identity = data.get("identity", {})

        # Kratos returns the verification flow_id in continue_with so callers can
        # verify the email without triggering a second email send.
        verif_flow_id = None
        for step in data.get("continue_with") or []:
            if step.get("action") == "show_verification_ui":
                verif_flow_id = step.get("flow", {}).get("id")
                break

        self._log(f"Registered — identity: {identity.get('id')} | token: {token[:20]}...")
        if verif_flow_id:
            self._log(f"Verification flow_id: {verif_flow_id}")
        return token, verif_flow_id

    def verify_email(self, flow_id: str, code: str) -> bool:
        """
        Submit a verification code using the flow_id from register().

        Returns True if passed_challenge, raises RuntimeError otherwise.
        """
        resp = self.session.post(
            f"{AUTH_URL}/self-service/verification?flow={flow_id}",
            json={"method": "code", "code": code},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()
        state = data.get("state")
        if state == "passed_challenge":
            self._log("Email verified successfully")
            return True
        msgs = [m.get("text", "") for m in data.get("ui", {}).get("messages", [])]
        raise RuntimeError(f"Verification failed (state={state}): {'; '.join(msgs)}")

    def logout(self):
        """Remove the session token (revert to anonymous mode)."""
        self.session_token = None
        self.session.headers.pop("Authorization", None)
        self.chat_id = None
        self.last_assistant_msg_id = None
        self.last_assistant_msg_version = None

    def _create_chat_trpc(self, message: str) -> str:
        """
        Create a new chat via tRPC procedure message.newChat (F91373).

        Input shape: createNewChatFromUserVariables (F33184)
          + createNewChatContentParams (F33188)

        Returns the server-assigned chatId.
        """
        # content: [{type: "text", text: ...}] per createNewChatContentParams (F33188)
        # productType: "chat" (not "le-chat" — Zod enum on server is "chat"|"work")
        # agentId/agentsApiAgentId: omitted (undefined in JS, null fails server validation)
        # transcriptionsMetadata: [] not null (server expects array)
        trpc_input = {
            "files": [],
            "content": [{"type": "text", "text": message}],
            "transcriptionsMetadata": [],
            "features": [],
            "integrations": [],
            "libraries": [],
            "productType": "chat",
            "projectId": None,
            "incognito": False,
        }

        # tRPC v11 httpBatchStreamLink single-item batch format
        body = {"0": {"json": trpc_input}}

        self._log(f"tRPC POST /api/trpc/message.newChat: {json.dumps(trpc_input)[:300]}")

        r = self.session.post(
            f"{BASE_URL}/api/trpc/message.newChat?batch=1",
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=30,
        )

        self._log(f"tRPC status: {r.status_code}")
        self._log(f"tRPC body: {r.text[:600]}")

        if r.status_code not in (200, 201):
            raise RuntimeError(f"tRPC {r.status_code}: {r.text[:300]}")

        # tRPC v11 batch response: [{"result": {"data": {"json": <data>}}}]
        # With superjson transformer: data is wrapped in {"json": ..., "meta": ...}
        payload = r.json()
        if isinstance(payload, list):
            payload = payload[0]

        result = payload.get("result", payload)
        rdata = result.get("data", result)
        if isinstance(rdata, dict) and "json" in rdata:
            rdata = rdata["json"]

        chat_id = (
            rdata.get("chatId")
            or rdata.get("id")
            or rdata.get("chat", {}).get("id")
        )
        if not chat_id:
            raise RuntimeError(f"No chatId in tRPC response: {json.dumps(rdata)[:300]}")

        self._log(f"Chat created: {chat_id}")
        return chat_id

    def _parse_line(self, line: str, current_chunks: dict) -> Generator[str, None, None]:
        """
        Parse one already-stripped `<type_num>:<json>` line and yield any text
        tokens it carries.

        Extracted verbatim out of `_parse_stream` so the wire format can be fed
        to it incrementally, line by line, instead of only after the whole
        body has been buffered. Every branch below does exactly what it did
        before the extraction; only the early-exit `continue`s (this no longer
        loops over lines itself) became `return`s.
        """
        if not line or ":" not in line:
            return

        colon = line.index(":")
        try:
            line_type = int(line[:colon])
            json_str = line[colon + 1:]
        except ValueError:
            return

        if not json_str or json_str == "null":
            return

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            self._log(f"Bad JSON on line type {line_type}: {json_str[:100]}")
            return

        j = data.get("json", data) if isinstance(data, dict) else data
        if not isinstance(j, dict):
            return

        if line_type == 6:
            msg = j.get("message", str(j))
            code = j.get("internalCode", 0)
            retry = j.get("retryAfterSeconds", 0)
            if code == 6200 or retry:
                raise RateLimitError(retry)
            raise RuntimeError(f"API error {code}: {msg}")

        msg_type = j.get("type")

        if msg_type == "bootstrap":
            chat = j.get("chat", {})
            if chat.get("id"):
                self.chat_id = chat["id"]
                self._log(f"Stream confirmed chatId: {self.chat_id}")
            return

        if msg_type == "message":
            msg_id = j.get("messageId")
            if not msg_id:
                return
            msg_version = j.get("messageVersion")

            for patch in j.get("patches", []):
                op = patch.get("op")
                path = patch.get("path", "")
                val = patch.get("value")

                if op == "replace" and path == "/":
                    if isinstance(val, dict) and val.get("role") == "assistant":
                        self.last_assistant_msg_id = msg_id
                        self.last_assistant_msg_version = str(msg_version) if msg_version is not None else None
                        current_chunks[msg_id] = ""
                    continue

                if op == "replace" and "/contentChunks" in path:
                    if isinstance(val, list):
                        for ci in val:
                            if isinstance(ci, dict) and ci.get("type") == "text":
                                token = ci.get("text", "")
                                current_chunks[msg_id] = token
                                if token:
                                    yield token
                    continue

                if op == "append" and "/contentChunks" in path:
                    if isinstance(val, str) and val:
                        current_chunks[msg_id] = current_chunks.get(msg_id, "") + val
                        yield val

    def _parse_stream(self, resp) -> Generator[str, None, None]:
        """
        Parse Mistral's streaming format: <type_num>:<json>\\n per line.

          Type 15 — data (bootstrap / message patches)
          Type 16 — metadata (disclaimers)
          Type  6 — error
          Type  8 — end of stream

        Line-incremental: yields tokens as each complete line arrives instead
        of after the whole body has been read, so a client watching the SSE
        response actually sees a stream. Updates self.chat_id and
        self.last_assistant_msg_id as a side effect, same as before.

        Uses an INCREMENTAL utf-8 decoder (not `chunk.decode()` per chunk):
        `iter_content(chunk_size=None)` splits at arbitrary byte offsets,
        including the middle of a multi-byte character, and Mistral answers
        in Spanish constantly -- a per-chunk `.decode()` would corrupt any
        `ñ` or accent that straddles a chunk boundary.
        """
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        total = 0
        current_chunks: dict = {}

        for chunk in resp.iter_content(chunk_size=None):
            total += len(chunk)
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield from self._parse_line(line.strip(), current_chunks)

        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            yield from self._parse_line(buffer.strip(), current_chunks)

        self._log(f"Stream raw ({total} bytes)")

    def _rotate_anonymous_id(self) -> None:
        """Generate a new stableAnonymousIdentifier (resets the 5-msg daily quota)."""
        self.stable_anon_id = str(uuid.uuid4())
        self._log(f"Rotated anonymous UUID → {self.stable_anon_id}")

    def send_message(
        self,
        message: str,
        model: str = DEFAULT_MODEL,
        auto_rotate: bool = True,
    ) -> Generator[str, None, None]:
        """
        Send a message and yield response tokens as they stream.

        First message:
          1. tRPC message.newChat → get chatId
          2. POST /api/chat mode=start → stream

        Follow-up messages:
          POST /api/chat mode=append → stream

        If auto_rotate=True (default, anonymous only), on RateLimitError the UUID is
        rotated and the request retried once in a fresh chat.
        """
        self.bootstrap_session()

        if self.chat_id is None:
            self.chat_id = self._create_chat_trpc(message)

            # Body for mode=start (F62487, t3 function, "start" branch)
            # clientPromptData: object required (not null)
            # supportedTaskCallbacks: array required (not null)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            body = {
                "mode": "start",
                "chatId": self.chat_id,
                "stableAnonymousIdentifier": self.stable_anon_id,
                "platform": "mobile",
                "clientPromptData": {"currentDate": now},
                "supportedTaskCallbacks": [],
                "features": [],
                "libraries": [],
                "integrations": [],
                "disabledFeatures": ["memory-inference"],
            }
            self._log(f"POST /api/chat mode=start chatId={self.chat_id}")
        else:
            # mode=append: add a user message to existing chat
            msg_id = str(uuid.uuid4())
            body = {
                "mode": "append",
                "chatId": self.chat_id,
                "messageId": msg_id,
                "messageInput": [{"type": "text", "text": message}],
            }
            self._log(f"POST /api/chat mode=append chatId={self.chat_id} msgId={msg_id}")

        resp = self.session.post(
            f"{BASE_URL}/api/chat",
            json=body,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
            },
            timeout=120,
            stream=True,
        )

        if resp.status_code not in (200, 201):
            raw_err = b""
            for _ch in resp.iter_content(chunk_size=None):
                raw_err += _ch
            try:
                err = json.loads(raw_err) if raw_err else {}
                code = err.get("code", 0)
                detail = err.get("detail", raw_err.decode("utf-8", errors="replace")[:400])
            except Exception:
                code = 0
                detail = raw_err.decode("utf-8", errors="replace")[:400]
            # HTTP-level 429 before stream starts: rotate UUID and retry once
            if resp.status_code == 429 and code == 6200 and auto_rotate and not self.session_token:
                self._log("HTTP 429 rate limit — rotating UUID and retrying")
                self._rotate_anonymous_id()
                self.chat_id = None
                self.last_assistant_msg_id = None
                self.last_assistant_msg_version = None
                yield from self.send_message(message, model, auto_rotate=False)
                return
            raise RuntimeError(f"HTTP {resp.status_code}: {detail}")

        try:
            yield from self._parse_stream(resp)
        except RateLimitError:
            if auto_rotate and not self.session_token:
                self._log("Stream 6200 rate limit — rotating UUID and retrying")
                self._rotate_anonymous_id()
                self.chat_id = None
                self.last_assistant_msg_id = None
                self.last_assistant_msg_version = None
                yield from self.send_message(message, model, auto_rotate=False)
            else:
                raise

    def chat(self, message: str, model: str = DEFAULT_MODEL) -> str:
        """Send message, print tokens as they stream, return full response."""
        tokens = []
        print("Assistant: ", end="", flush=True)
        try:
            for token in self.send_message(message, model):
                print(token, end="", flush=True)
                tokens.append(token)
        except RateLimitError as e:
            print(f"\n[RATE LIMITED] {e}", file=sys.stderr)
            raise
        print()
        return "".join(tokens)

    def reset(self):
        """Start a fresh chat (forget current conversation)."""
        self.chat_id = None
        self.last_assistant_msg_id = None
        self.last_assistant_msg_version = None

    # ── Vision / file upload ──────────────────────────────────────────────────

    def upload_image_to_blob(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        """
        Upload an image to Mistral's Azure Blob storage.

        Flow (Next.js web app chunk 2nqt98tojzakk.js — uploadFiles function):
          1. POST /api/trpc/file.uploadFile {type:"image", count:1, includeReadUrl:true}
             → {uploadURLs: [write_sas_url], readURLs: [read_sas_url]}
          2. PUT binary to uploadURLs[0] with Content-Type + x-ms-blob-type:BlockBlob
          3. POST /api/trpc/file.confirmUpload {uploadUrl, type:"image"} (required for images)
          4. Return readURLs[0] (read-only SAS URL) — this is what the AI uses to read the image.

        The raw blob URL without SAS (upload_url.split("?")[0]) does NOT work because
        the blob container is private. The readURL includes a short-lived read SAS token.

        Returns the read-SAS URL string.
        """
        self.bootstrap_session()

        r = self.session.post(
            f"{BASE_URL}/api/trpc/file.uploadFile?batch=1",
            json={"0": {"json": {"type": "image", "count": 1, "includeReadUrl": True}}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"file.uploadFile tRPC failed: {r.status_code} {r.text[:200]}")

        result = r.json()[0]["result"]["data"]["json"]
        upload_url = result["uploadURLs"][0]
        read_url = result["readURLs"][0]

        # Azure Blob SAS URLs use the SAS token in the URL for auth.
        # Use urllib directly to avoid sending the session's Authorization: Bearer header,
        # which Azure rejects when combined with a SAS URL.
        import urllib.request, urllib.error
        req = urllib.request.Request(
            upload_url,
            data=image_bytes,
            method="PUT",
            headers={"Content-Type": mime_type, "x-ms-blob-type": "BlockBlob"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Azure Blob PUT failed: {e.code} {e.reason}")
        if status not in (200, 201):
            raise RuntimeError(f"Azure Blob PUT unexpected status: {status}")

        # Confirm the upload to Mistral's backend (required for images per web app source)
        self.session.post(
            f"{BASE_URL}/api/trpc/file.confirmUpload?batch=1",
            json={"0": {"json": {"uploadUrl": upload_url, "type": "image"}}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=10,
        )

        self._log(f"Image uploaded → {read_url[:80]}...")
        return read_url

    def _create_chat_trpc_with_files(self, message: str, files: list) -> str:
        """Like _create_chat_trpc but with files attached."""
        trpc_input = {
            "files": files,
            "content": [{"type": "text", "text": message}],
            "transcriptionsMetadata": [],
            "features": [],
            "integrations": [],
            "libraries": [],
            "productType": "chat",
            "projectId": None,
            "incognito": False,
        }
        body = {"0": {"json": trpc_input}}
        self._log(f"tRPC newChat with {len(files)} file(s)")
        r = self.session.post(
            f"{BASE_URL}/api/trpc/message.newChat?batch=1",
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"tRPC {r.status_code}: {r.text[:300]}")
        rdata = r.json()[0]["result"]["data"]["json"]
        chat_id = rdata.get("chatId")
        if not chat_id:
            raise RuntimeError(f"No chatId in tRPC response: {json.dumps(rdata)[:200]}")
        self._log(f"Chat with image created: {chat_id}")
        return chat_id

    def send_image(
        self,
        image_path_or_bytes,
        message: str = "Describe this image",
        mime_type: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """
        Send an image + text message to Mistral (vision).

        image_path_or_bytes: file path string OR raw bytes.
        Returns generator yielding text tokens.
        """
        if isinstance(image_path_or_bytes, (str, bytes.__class__)) and not isinstance(image_path_or_bytes, bytes):
            path = image_path_or_bytes
            if mime_type is None:
                mime_type = mimetypes.guess_type(path)[0] or "image/jpeg"
            with open(path, "rb") as f:
                image_bytes = f.read()
        else:
            image_bytes = image_path_or_bytes
            if mime_type is None:
                mime_type = "image/jpeg"

        read_url = self.upload_image_to_blob(image_bytes, mime_type)
        # name is required: web app source shows {type, url, name} in files array
        import os as _os
        fname = _os.path.basename(image_path_or_bytes) if isinstance(image_path_or_bytes, str) else "image.jpg"
        files = [{"url": read_url, "type": "image", "name": fname}]

        self.chat_id = self._create_chat_trpc_with_files(message, files)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "mode": "start",
            "chatId": self.chat_id,
            "stableAnonymousIdentifier": self.stable_anon_id,
            "platform": "mobile",
            "clientPromptData": {"currentDate": now},
            "supportedTaskCallbacks": [],
            "features": [],
            "libraries": [],
            "integrations": [],
            "disabledFeatures": ["memory-inference"],
        }

        resp = self.session.post(
            f"{BASE_URL}/api/chat",
            json=body,
            headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
            timeout=120,
            stream=True,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")

        yield from self._parse_stream(resp)

    def generate_image(self, prompt: str) -> str:
        """
        Generate an image from a text prompt using beta-imagegen feature.

        Discovery (APK F78776, F4558):
          - Feature "beta-imagegen" is sent in features[] of /api/chat.
          - Model responds with a tool_call chunk (generate_image) then
            an image_url chunk with {meta: {uri: "<url>"}}.

        Returns the generated image URL.
        """
        self.bootstrap_session()
        if not self.session_token:
            raise RuntimeError("Authentication required for image generation.")

        # Wrap with explicit intent so the model uses generate_image tool
        wrapped_prompt = f"Generate an image of: {prompt}"
        trpc_input = {
            "files": [],
            "content": [{"type": "text", "text": wrapped_prompt}],
            "transcriptionsMetadata": [],
            "features": ["beta-imagegen"],
            "integrations": [],
            "libraries": [],
            "productType": "chat",
            "projectId": None,
            "incognito": False,
        }
        r = self.session.post(
            f"{BASE_URL}/api/trpc/message.newChat?batch=1",
            json={"0": {"json": trpc_input}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"tRPC {r.status_code}: {r.text[:300]}")

        chat_id = r.json()[0]["result"]["data"]["json"]["chatId"]

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "mode": "start",
            "chatId": chat_id,
            "stableAnonymousIdentifier": self.stable_anon_id,
            "platform": "mobile",
            "clientPromptData": {"currentDate": now},
            "supportedTaskCallbacks": [],
            "features": ["beta-imagegen"],
            "libraries": [],
            "integrations": [],
            "disabledFeatures": ["memory-inference"],
        }

        resp = self.session.post(
            f"{BASE_URL}/api/chat",
            json=body,
            headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
            timeout=120,
            stream=True,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")

        # Parse stream for image_url chunks
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
                raise RuntimeError(f"API error: {j.get('message', str(j))}")

            if j.get("type") == "message":
                for patch in j.get("patches", []):
                    val = patch.get("value")
                    path = patch.get("path", "")
                    op = patch.get("op")

                    if op == "replace" and path == "/contentChunks" and isinstance(val, list):
                        for chunk in val:
                            if isinstance(chunk, dict) and chunk.get("type") == "image_url":
                                meta = chunk.get("meta") or {}
                                url = meta.get("uri") or chunk.get("url") or chunk.get("imageUrl")
                                if url:
                                    return url

                    if "/contentChunks" in path and isinstance(val, dict):
                        if val.get("type") == "image_url":
                            meta = val.get("meta") or {}
                            url = meta.get("uri") or val.get("url") or val.get("imageUrl")
                            if url:
                                return url

        raise RuntimeError("No image URL found in model response. Image generation may not be enabled for this account.")

    # ── Text-to-Speech (read-aloud) ───────────────────────────────────────────

    def read_aloud(self, chat_id: str, message_id: str, message_version: str) -> bytes:
        """
        Fetch raw PCM float32 audio for a message via /api/read-aloud.

        Discovery (APK F78399 streamTtsAudio):
          GET /api/read-aloud?chatId=...&messageId=...&messageVersion=...
          Auth: Authorization: Bearer <token> (standard auth header)
          Response: binary stream of PCM float32 @ 24kHz mono (4 bytes/sample)
          Header X-Estimated-Duration: ISO 8601 duration (e.g. PT3.2S)

        Returns raw PCM bytes (convert with pcm_float32_to_wav() for playback).
        """
        self.bootstrap_session()
        import urllib.parse
        params = urllib.parse.urlencode({
            "chatId": chat_id,
            "messageId": message_id,
            "messageVersion": message_version,
        })
        resp = self.session.get(
            f"{BASE_URL}/api/read-aloud?{params}",
            headers={"Accept": "audio/*, */*"},
            timeout=60,
            stream=True,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"read-aloud HTTP {resp.status_code}: {resp.text[:200]}")
        pcm = b""
        for chunk in resp.iter_content(chunk_size=None):
            pcm += chunk
        self._log(f"read-aloud: got {len(pcm)} bytes PCM float32")
        return pcm

    def tts(self, text: str) -> bytes:
        """
        Convert text to speech by:
          1. Creating a chat where the assistant echoes the text verbatim.
          2. Streaming to capture messageId + messageVersion.
          3. Calling /api/read-aloud to get PCM audio.
        Returns raw PCM float32 bytes (24kHz mono). Use pcm_float32_to_wav() to get WAV.
        """
        if not self.session_token:
            raise RuntimeError("TTS requires authentication.")

        prompt = (
            "Output ONLY the following text, verbatim, with zero additions or changes:\n\n"
            + text
        )

        old_chat_id = self.chat_id
        old_msg_id = self.last_assistant_msg_id
        old_msg_ver = self.last_assistant_msg_version
        self.chat_id = None
        self.last_assistant_msg_id = None
        self.last_assistant_msg_version = None

        try:
            # Consume stream — side effect: sets last_assistant_msg_id and last_assistant_msg_version
            for _ in self.send_message(prompt):
                pass
        finally:
            chat_id = self.chat_id
            msg_id = self.last_assistant_msg_id
            msg_ver = self.last_assistant_msg_version
            # Restore original conversation state
            self.chat_id = old_chat_id
            self.last_assistant_msg_id = old_msg_id
            self.last_assistant_msg_version = old_msg_ver

        if not chat_id or not msg_id or msg_ver is None:
            raise RuntimeError(f"TTS: failed to capture message IDs (chatId={chat_id}, msgId={msg_id}, version={msg_ver})")

        return self.read_aloud(chat_id, msg_id, msg_ver)

    # ── Audio transcription ───────────────────────────────────────────────────

    def upload_audio_to_blob(self, audio_bytes: bytes, mime_type: str = "audio/m4a") -> str:
        """
        Upload audio to Azure Blob via file.uploadFile tRPC (type: "audio").
        Returns the full presigned SAS URL (needed by transcribeFromSignedUrl).
        """
        self.bootstrap_session()
        r = self.session.post(
            f"{BASE_URL}/api/trpc/file.uploadFile?batch=1",
            json={"0": {"json": {"type": "audio", "count": 1}}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"file.uploadFile (audio) failed: {r.status_code} {r.text[:200]}")

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
            raise RuntimeError(f"Azure Blob PUT (audio) failed: {e.code} {e.reason}")
        if status not in (200, 201):
            raise RuntimeError(f"Azure Blob PUT (audio) unexpected status: {status}")

        self._log(f"Audio uploaded → {upload_url[:80]}...")
        return upload_url  # full SAS URL needed for transcribeFromSignedUrl

    def transcribe(self, audio_path_or_bytes, mime_type: Optional[str] = None) -> str:
        """
        Transcribe audio via message.transcribeFromSignedUrl tRPC.

        Discovery (APK F78468 transcribeAudio generator, F63427):
          1. file.uploadFile {type:"audio",count:1} → presigned SAS URL
          2. PUT audio bytes to SAS URL (no Auth header)
          3. message.transcribeFromSignedUrl {audioUrl: sas_url, recordingId: uuid}
             → {transcript, correlationId, recordingId, url}

        audio_path_or_bytes: file path string OR raw bytes.
        mime_type: MIME type, auto-detected from path if not given.
        Returns transcript string.
        """
        if not self.session_token:
            raise RuntimeError("Transcription requires authentication.")

        if isinstance(audio_path_or_bytes, (str,)) and not isinstance(audio_path_or_bytes, bytes):
            path = audio_path_or_bytes
            if mime_type is None:
                mime_type = mimetypes.guess_type(path)[0] or "audio/m4a"
            with open(path, "rb") as f:
                audio_bytes = f.read()
        else:
            audio_bytes = audio_path_or_bytes
            if mime_type is None:
                mime_type = "audio/m4a"

        sas_url = self.upload_audio_to_blob(audio_bytes, mime_type)
        recording_id = str(uuid.uuid4())

        r = self.session.post(
            f"{BASE_URL}/api/trpc/message.transcribeFromSignedUrl?batch=1",
            json={"0": {"json": {"audioUrl": sas_url, "recordingId": recording_id}}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=60,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"transcribeFromSignedUrl failed: {r.status_code} {r.text[:300]}")

        result = r.json()[0]["result"]["data"]["json"]
        transcript = result.get("transcript", "")
        self._log(f"Transcription: {transcript[:80]}...")
        return transcript


def pcm_float32_to_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """
    Convert raw PCM float32 audio (from /api/read-aloud) to 16-bit PCM WAV bytes.
    Audio format discovered from APK: {sampleRate:24000, channels:1, bytesPerSample:4}.
    """
    import struct
    n_samples = len(pcm_bytes) // 4
    if n_samples == 0:
        raise ValueError("Empty PCM audio data")
    samples_f32 = struct.unpack(f"<{n_samples}f", pcm_bytes[:n_samples * 4])
    samples_i16 = struct.pack(
        f"<{n_samples}h",
        *[max(-32768, min(32767, int(s * 32767))) for s in samples_f32],
    )
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(samples_i16)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size,
        b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    return header + samples_i16


# ─────────────────────────────────────────────────────────────────────────────
# Vibe Code — /api/code-trpc  (discovered via APK reverse engineering)
# ─────────────────────────────────────────────────────────────────────────────

class VibeCode:
    """
    Vibe Code client for /api/code-trpc.

    All endpoints use tRPC v11 superjson format:
      GET  ?batch=1&input={"0":{"json":{...}}}   (queries)
      POST ?batch=1  body={"0":{"json":{...}}}   (mutations)

    SSE subscriptions use a different format (httpSubscriptionLink):
      GET  ?input={"json":{...}}                 (no "0" wrapper)

    Sources: F42324 (useCreateSession), F42581 (useSessionSubscription),
             F32115 (useListProjects), F42404 (useCreateProject),
             F32120 (useCodeCacheUtils), F50132 (FeatureIntroductionRoute)
    """

    CODE_TRPC = f"{BASE_URL}/api/code-trpc"
    TRPC = f"{BASE_URL}/api/trpc"

    def __init__(self, chat_client: "MistralAnonChat"):
        self.c = chat_client  # shares the same requests.Session (auth + cookies)

    def _log(self, msg: str):
        self.c._log(msg)

    def _get(self, proc: str, payload: dict) -> dict:
        """tRPC GET batch request → unwrapped JSON result."""
        import urllib.parse
        inp = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
        r = self.c.session.get(
            f"{self.CODE_TRPC}/{proc}?batch=1&input={inp}",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        self._log(f"GET {proc} → {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"code-trpc GET {proc} {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data[0]["result"]["data"]["json"]

    def _post(self, proc: str, payload: dict) -> dict:
        """tRPC POST batch mutation → unwrapped JSON result."""
        r = self.c.session.post(
            f"{self.CODE_TRPC}/{proc}?batch=1",
            json={"0": {"json": payload}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20,
        )
        self._log(f"POST {proc} → {r.status_code}")
        if r.status_code not in (200, 201):
            raise RuntimeError(f"code-trpc POST {proc} {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data[0]["result"]["data"]["json"]

    def _trpc_post(self, proc: str, payload) -> dict:
        """Main-trpc POST (for featureMetadata.update etc.)."""
        r = self.c.session.post(
            f"{self.TRPC}/{proc}?batch=1",
            json={"0": {"json": payload}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=20,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"trpc POST {proc} {r.status_code}: {r.text[:300]}")
        return r.json()[0]["result"]["data"]["json"]

    # ── Feature metadata ──────────────────────────────────────────────────────

    def init_feature(self):
        """
        Mark the Vibe Code intro modal as seen (featureMetadata.update).
        Safe to call multiple times. Called automatically by create_session().

        Source: F50132 (FeatureIntroductionRoute) onContinue handler.
        Schema: {feature: discriminator, data: {<feature-specific fields>}}
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        result = self._trpc_post("featureMetadata.update", {
            "feature": "vibe",
            "data": {"hasSeenCodeIntroductionModalV1At": now},
        })
        self._log(f"featureMetadata.update → {result}")

    def get_feature_metadata(self) -> dict:
        """Returns {mcp, memories, vibe, inputV2, tts, connectors, vibeCodeWorkflow}."""
        import urllib.parse
        inp = urllib.parse.quote(json.dumps({"0": {"json": None}}))
        r = self.c.session.get(
            f"{self.TRPC}/featureMetadata.all?batch=1&input={inp}",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        return r.json()[0]["result"]["data"]["json"]

    # ── Projects ──────────────────────────────────────────────────────────────

    def list_projects(self) -> list:
        """List all Vibe Code projects. Source: F32115 (useListProjects)."""
        result = self._get("projects.list", {})
        return result.get("items", [])

    def create_project(self, name: str = "default") -> dict:
        """
        Create a new Vibe Code project. Source: F42404 (useCreateProject).
        Returns {id, name, createdAt, repositories, isReadOnly}.
        """
        return self._post("projects.create", {"name": name, "repositories": []})

    def get_or_create_project(self, name: str = "default") -> dict:
        """Return existing project by name or create it."""
        for p in self.list_projects():
            if p.get("name") == name:
                self._log(f"Reusing project '{name}': {p['id']}")
                return p
        self._log(f"Creating project '{name}'...")
        return self.create_project(name)

    # ── Sessions ──────────────────────────────────────────────────────────────

    def list_sessions(self, project_id: str) -> list:
        """List sessions for a project. Source: F32189 (useListSessions)."""
        result = self._get("sessions.list", {"projectId": project_id})
        return result.get("items", [])

    def create_session(self, project_id: str, prompt: str) -> dict:
        """
        Create a new coding session (starts the cloud AI agent).
        Source: F42324 (useCreateSession).
        Returns {id, externalSessionId, status, currentHostKind, title, ...}.
        """
        payload = {
            "projectId": project_id,
            "idempotencyKey": str(uuid.uuid4()),
            "content": [{"type": "text", "text": prompt}],
        }
        return self._post("sessions.create", payload)

    def get_session(self, session_id: str) -> dict:
        """Get session metadata. Source: F42580 (useGetSession)."""
        return self._get("sessions.byId", {"id": session_id})

    def get_messages(self, session_id: str) -> list:
        """Get persisted messages for a session. Source: sessions.messages."""
        result = self._get("sessions.messages", {"sessionId": session_id})
        return result.get("messages", [])

    # ── SSE Subscription ──────────────────────────────────────────────────────

    def subscribe(self, session_id: str):
        """
        Subscribe to session events via SSE (httpSubscriptionLink format).

        URL format: GET /api/code-trpc/sessions.subscribe?input={"json":{...}}
        Note: NO batch wrapper — this uses httpSubscriptionLink, not httpBatchLink.

        Source: F42581 (useSessionSubscription), F7591 (codeTrpcClient):
          codeTRPC.sessions.subscribe.subscriptionOptions({sessionId, lastEventId})

        Yields dicts:
          {"type": "status",   "value": "running"|"waiting"|"complete"|"failed"}
          {"type": "text",     "value": "<token>",    "role": "assistant", "done": bool}
          {"type": "tool",     "name": "<name>",      "done": bool, "success": bool}
          {"type": "callback", "callbackId": "...",   "callbackName": "...", "input": {...}}
          {"type": "error",    "value": "<message>"}
        """
        import urllib.parse

        inp = urllib.parse.quote(json.dumps({"json": {"sessionId": session_id}}))
        url = f"{self.CODE_TRPC}/sessions.subscribe?input={inp}"
        self._log(f"SSE subscribe → {url}")

        resp = self.c.session.get(
            url,
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            stream=True,
            timeout=300,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"SSE {resp.status_code}: {resp.text[:200]}")

        # Track per-session state so we can detect status transitions
        state: dict = {"messages": [], "status": None}
        msg_text: dict = {}  # msg_id → accumulated text

        current_event: dict = {}

        for raw in resp.iter_lines():
            line = raw.decode("utf-8", errors="replace") if raw else ""

            if not line:
                # Blank line = end of SSE event
                if "data" not in current_event:
                    current_event = {}
                    continue

                ev_name = current_event.get("event", "")
                data_str = current_event.get("data", "")
                current_event = {}

                if ev_name == "connected":
                    continue
                if ev_name == "serialized-error":
                    try:
                        err = json.loads(data_str)
                        msg = err.get("json", {}).get("message", data_str)
                    except Exception:
                        msg = data_str
                    yield {"type": "error", "value": msg}
                    return

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                patches = data.get("json", [])
                if not isinstance(patches, list):
                    continue

                for patch in patches:
                    op = patch.get("op")
                    path = patch.get("path", "")
                    val = patch.get("value")

                    # Root state replacement (bootstrap)
                    if op == "replace" and path == "/":
                        if isinstance(val, dict):
                            new_status = val.get("status")
                            if new_status and new_status != state["status"]:
                                state["status"] = new_status
                                yield {"type": "status", "value": new_status}
                        continue

                    # Status field update
                    if op == "replace" and path == "/status":
                        if val != state["status"]:
                            state["status"] = val
                            yield {"type": "status", "value": val}
                        continue

                    # New message added
                    if op == "add" and path.startswith("/messages/"):
                        if isinstance(val, dict):
                            role = val.get("role", "")
                            mid = val.get("id", path)
                            for chunk in val.get("contentChunks", []):
                                yield from self._yield_chunk(chunk, role, mid, msg_text)
                        continue

                    # Append to an existing contentChunk (streaming text)
                    if op == "append" and "/contentChunks/" in path:
                        # path like /messages/0/contentChunks/1/text
                        if isinstance(val, str) and "/text" in path:
                            parts = path.split("/")
                            # msg index → messages list
                            try:
                                msg_idx = int(parts[2])
                                msgs = state.get("messages", [])
                                mid = msgs[msg_idx]["id"] if msg_idx < len(msgs) else path
                            except (IndexError, ValueError, KeyError):
                                mid = path
                            token = val
                            msg_text[mid] = msg_text.get(mid, "") + token
                            yield {"type": "text", "value": token, "role": "assistant", "done": False}
                        continue

                    # Replace on a full messages array element
                    if op == "replace" and "/contentChunks" in path and "/text" not in path:
                        if isinstance(val, list):
                            parts = path.split("/")
                            try:
                                msg_idx = int(parts[2])
                                msgs = state.get("messages", [])
                                mid = msgs[msg_idx]["id"] if msg_idx < len(msgs) else path
                                role = msgs[msg_idx].get("role", "") if msg_idx < len(msgs) else ""
                            except (IndexError, ValueError, KeyError):
                                mid = path
                                role = ""
                            for chunk in val:
                                yield from self._yield_chunk(chunk, role, mid, msg_text)
                        continue

                # Detect terminal states
                if state["status"] in ("complete", "failed", "canceled"):
                    return

            else:
                if line.startswith("event:"):
                    current_event["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    current_event["data"] = line[5:].strip()
                elif line.startswith("id:"):
                    current_event["id"] = line[3:].strip()

    def _yield_chunk(self, chunk: dict, role: str, mid: str, msg_text: dict):
        """Convert a contentChunk into yielded events."""
        ctype = chunk.get("type")
        if ctype == "text":
            text = chunk.get("text", "")
            if text:
                msg_text[mid] = msg_text.get(mid, "") + text
                yield {"type": "text", "value": text, "role": role, "done": True}
        elif ctype == "tool_call":
            yield {
                "type": "tool",
                "name": chunk.get("name", ""),
                "done": chunk.get("isDone", False),
                "success": chunk.get("success", True),
                "args": chunk.get("publicArguments", ""),
            }
        elif ctype == "task_callback":
            yield {
                "type": "callback",
                "callbackId": chunk.get("callbackId", ""),
                "callbackName": chunk.get("callbackName", ""),
                "input": chunk.get("input", {}),
            }

    # ── Send follow-up message ────────────────────────────────────────────────

    def send_message(self, session_id: str, text: str) -> dict:
        """
        Send a follow-up message to a running Vibe Code session.
        Source: F42623 (useSendMessage) → sessions.sendMessage

        Input: { sessionId, idempotencyKey, content: [{type,text}] }
        Use after creating a session to continue the conversation.
        """
        return self._post("sessions.sendMessage", {
            "sessionId": session_id,
            "idempotencyKey": str(uuid.uuid4()),
            "content": [{"type": "text", "text": text}],
        })

    # ── Respond to agent callbacks ────────────────────────────────────────────

    def respond_callback(self, session_id: str, callback_id: str, result: dict):
        """
        Respond to a task_callback (e.g. ask_user_question).
        Source: F42624 (useRespondCallback) → sessions.respondCallback

        result union (from F4428 schema closure_37):
          • ask_user_question: {"answers": [{"question": q, "answer": a, "selected": [a]?}], "canceled": false}
          • confirmation:      {"confirmed": true} or {"confirmed": false, "reason": "..."}
          • connector auth:    {"connectedConnectorIds": [...], "deniedConnectorIds": [...]}
        """
        return self._post("sessions.respondCallback", {
            "sessionId": session_id,
            "callbackId": callback_id,
            "result": result,
        })

    # ── Artifacts ─────────────────────────────────────────────────────────────

    def list_artifacts(self, session_id: str) -> list:
        """
        List files/artifacts produced by the agent in a session.
        Source: F46758 (applyNotification), F4428 (schema closure_41+42).

        Returns list of {id, sessionId, externalKey, externalUrl, displayTitle, ...}.
          externalKey: path in the cloud sandbox
          externalUrl: download URL (presigned or direct)
        """
        result = self._get("sessions.listArtifacts", {"sessionId": session_id})
        return result.get("artifacts", [])

    def mark_artifacts_viewed(self, artifact_ids: list) -> None:
        """Mark artifact(s) as viewed. Source: F4428 schema closure_43."""
        self._post("sessions.markArtifactsViewed", {"artifactIds": artifact_ids})

    def download_artifact(self, artifact: dict) -> bytes:
        """
        Download artifact content using its externalUrl.
        The externalUrl is a direct or presigned URL to the file.
        """
        url = artifact.get("externalUrl", "")
        if not url:
            raise ValueError(f"Artifact {artifact.get('id')} has no externalUrl")
        r = self.c.session.get(url, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Download failed {r.status_code}: {r.text[:200]}")
        return r.content

    def get_files_from_messages(self, session_id: str) -> list:
        """
        Extract files written by the agent from write_file tool call args.

        The agent writes files via write_file tool calls whose publicArguments
        contain {path, content}. This is the most reliable way to get file
        content — available even before the session completes and without
        needing artifacts to be published.

        Returns list of {path, content, size} dicts.
        """
        msgs = self.get_messages(session_id)
        files = []
        for msg in msgs:
            for chunk in msg.get("contentChunks", []):
                if chunk.get("type") == "tool_call" and chunk.get("name") == "write_file":
                    args = chunk.get("publicArguments", {})
                    if isinstance(args, dict) and "path" in args and "content" in args:
                        files.append({
                            "path": args["path"],
                            "content": args["content"],
                            "size": len(args.get("content", "")),
                        })
        return files

    def get_api_key(self) -> Optional[str]:
        """
        Get the Vibe Code API key (vibeApiKey).
        Only available when vibeCodeLocalEnabled=true in account settings.
        Returns None for cloud-only accounts (default).
        """
        result = self._get("apiKey.getApiKey", {})
        return result.get("vibeApiKey")

    # ── High-level: create project + session + stream ─────────────────────────

    def run(
        self,
        prompt: str,
        project_name: str = "default",
        existing_session_id: Optional[str] = None,
        interactive: bool = True,
    ):
        """
        Full Vibe Code flow:
          1. Ensure feature seen
          2. Get/create project
          3. Create session (or reuse existing_session_id)
          4. Stream SSE events to stdout

        Handles tool progress, text, and interactive task_callback questions.
        """
        self.c.bootstrap_session()
        self.init_feature()

        if existing_session_id:
            session_id = existing_session_id
            print(f"[Vibe Code] Reconnecting to session {session_id}", file=sys.stderr)
        else:
            project = self.get_or_create_project(project_name)
            project_id = project["id"]
            print(f"[Vibe Code] Project: {project['name']} ({project_id})", file=sys.stderr)

            session = self.create_session(project_id, prompt)
            session_id = session["id"]
            print(f"[Vibe Code] Session: {session_id}", file=sys.stderr)
            print(f"[Vibe Code] Host: {session.get('currentHostKind', '?')}", file=sys.stderr)

        print(f"[Vibe Code] Streaming... (Ctrl-C to stop)\n", file=sys.stderr)

        for ev in self.subscribe(session_id):
            etype = ev.get("type")

            if etype == "status":
                status = ev["value"]
                print(f"\n[{status.upper()}]", file=sys.stderr)
                if status in ("complete", "failed", "canceled"):
                    break

            elif etype == "tool":
                name = ev.get("name", "")
                done = ev.get("done", False)
                ok = ev.get("success", True)
                if done:
                    mark = "✓" if ok else "✗"
                    print(f"\n[{mark} {name}]", file=sys.stderr)
                else:
                    print(f"\n[…] {name}", file=sys.stderr)

            elif etype == "text":
                print(ev["value"], end="", flush=True)

            elif etype == "callback":
                cb_name = ev.get("callbackName", "")
                cb_id = ev.get("callbackId", "")
                cb_input = ev.get("input", {})

                if cb_name == "ask_user_question" and interactive and sys.stdin.isatty():
                    print()  # newline after any streaming text
                    # Schema (F4428 closure_37): result = {answers: [{question, answer, selected?}], canceled: bool}
                    answer_items = []
                    for q in cb_input.get("questions", []):
                        question_text = q.get("question", "")
                        options = q.get("options", [])
                        print(f"\n? {question_text}")
                        for i, opt in enumerate(options, 1):
                            print(f"  {i}. {opt}")
                        raw = ""
                        while True:
                            try:
                                raw = input("  Answer (number or text): ").strip()
                            except (EOFError, KeyboardInterrupt):
                                break
                            if not raw:
                                continue
                            break
                        if raw:
                            try:
                                idx = int(raw) - 1
                                if 0 <= idx < len(options):
                                    chosen = options[idx]
                                    answer_items.append({
                                        "question": question_text,
                                        "answer": chosen,
                                        "selected": [chosen],
                                    })
                                    continue
                            except ValueError:
                                pass
                            answer_items.append({"question": question_text, "answer": raw})
                    if answer_items:
                        try:
                            self.respond_callback(session_id, cb_id, {
                                "answers": answer_items,
                                "canceled": False,
                            })
                        except Exception as e:
                            print(f"\n[callback error: {e}]", file=sys.stderr)
                else:
                    print(f"\n[Q: {cb_name}] {json.dumps(cb_input)[:200]}", file=sys.stderr)

            elif etype == "error":
                print(f"\n[ERROR] {ev['value']}", file=sys.stderr)
                break

        print()  # trailing newline


def main():
    parser = argparse.ArgumentParser(
        description="Mistral chat + Vibe Code — APK v2.8.0 reverse-engineered",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Anonymous chat
  %(prog)s "explain quantum computing"
  %(prog)s --interactive

  # Authenticated chat (reads .env automatically)
  %(prog)s --email user@example.com --password pass "hello"
  %(prog)s --token ory_st_...  "hello"

  # Vision — describe or analyze an image
  %(prog)s --token ory_st_... --image photo.jpg "What's in this image?"
  %(prog)s --image screenshot.png  # uses default "Describe this image"

  # Image generation (requires account, beta-imagegen feature)
  %(prog)s --token ory_st_... --generate-image "a red sunset over the ocean"

  # Register a new account
  %(prog)s --register --email user@example.com --password pass

  # Text-to-speech (requires auth + beta-audio-transcription feature on account)
  %(prog)s --token ory_st_... --tts "Hello, world!" --output hello.wav
  %(prog)s --token ory_st_... --tts "Hello" | aplay -f FLOAT_LE -r 24000 -c 1

  # Audio transcription
  %(prog)s --token ory_st_... --transcribe recording.m4a

  # Direct read-aloud (Mistral-native: chatId:messageId:messageVersion)
  %(prog)s --token ory_st_... --speak "CHATID:MSGID:VERSION" --output out.wav

  # Vibe Code — create project + session and stream agent output
  %(prog)s --vibe "build a todo app in React"
  %(prog)s --vibe "build a REST API" --vibe-project my-api

  # Vibe Code — reconnect to an existing session
  %(prog)s --vibe-session SESSION_ID

  # Vibe Code — list projects / sessions / artifacts
  %(prog)s --vibe-projects
  %(prog)s --vibe-sessions PROJECT_ID
  %(prog)s --vibe-artifacts SESSION_ID

  # Vibe Code — download all artifacts from a session
  %(prog)s --vibe-download SESSION_ID

  # Vibe Code — send follow-up message to existing session
  %(prog)s --vibe-send SESSION_ID "add unit tests"

  # Vibe Code — extract & save files written by agent (from write_file tool calls)
  %(prog)s --vibe-files SESSION_ID
""",
    )

    # Chat args
    parser.add_argument("message", nargs="?", help="Chat message to send")
    parser.add_argument("--debug", "-d", action="store_true", help="Show debug output")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help=f"Model (default: {DEFAULT_MODEL})")
    parser.add_argument("--interactive", "-i", action="store_true", help="REPL mode")

    # Vision / image generation
    parser.add_argument("--image", metavar="PATH",
                        help="Image file to send for vision analysis (requires auth)")
    parser.add_argument("--generate-image", metavar="PROMPT",
                        help="Generate an image from text prompt (requires auth + beta-imagegen)")

    # Audio
    parser.add_argument("--tts", metavar="TEXT",
                        help="Convert text to speech (requires auth). Outputs WAV.")
    parser.add_argument("--transcribe", metavar="FILE",
                        help="Transcribe an audio file (requires auth).")
    parser.add_argument("--speak", metavar="CHATID:MSGID:VERSION",
                        help="Read aloud an existing message (direct TTS, requires auth).")
    parser.add_argument("--output", "-o", metavar="FILE",
                        help="Output file for --tts / --speak (default: stdout raw PCM)")

    # Auth args
    parser.add_argument("--email", "-e", help="Email for login/registration")
    parser.add_argument("--password", "-p", help="Password for login/registration")
    parser.add_argument("--token", "-t", help="Use existing Ory session token directly")
    parser.add_argument("--register", "-r", action="store_true", help="Register a new account")
    parser.add_argument("--first-name", default="User", help="First name (registration)")
    parser.add_argument("--last-name", default="", help="Last name (registration)")

    # Vibe Code args
    parser.add_argument("--vibe", metavar="PROMPT", help="Start a Vibe Code session with this prompt")
    parser.add_argument("--vibe-project", default="default", metavar="NAME",
                        help="Project name to use/create for Vibe Code (default: default)")
    parser.add_argument("--vibe-session", metavar="SESSION_ID",
                        help="Reconnect to an existing Vibe Code session")
    parser.add_argument("--vibe-projects", action="store_true",
                        help="List all Vibe Code projects")
    parser.add_argument("--vibe-sessions", metavar="PROJECT_ID",
                        help="List sessions for a Vibe Code project")
    parser.add_argument("--vibe-artifacts", metavar="SESSION_ID",
                        help="List artifacts produced by a Vibe Code session")
    parser.add_argument("--vibe-download", metavar="SESSION_ID",
                        help="Download all artifacts from a session to current directory")
    parser.add_argument("--vibe-send", nargs=2, metavar=("SESSION_ID", "MESSAGE"),
                        help="Send a follow-up message to an existing Vibe Code session")
    parser.add_argument("--vibe-files", metavar="SESSION_ID",
                        help="List and save files written by the agent (from write_file tool calls)")
    parser.add_argument("--no-interactive", action="store_true",
                        help="Disable interactive task_callback prompts in Vibe Code")

    args = parser.parse_args()

    # ── Auth ──────────────────────────────────────────────────────────────────
    token = args.token or os.environ.get("MISTRAL_SESSION_TOKEN")
    email = args.email or os.environ.get("MISTRAL_EMAIL")
    password = args.password or os.environ.get("MISTRAL_PASSWORD")

    client = MistralAnonChat(debug=args.debug)

    if token and not args.register:
        client.session_token = token
        client.session.headers["Authorization"] = f"Bearer {token}"
        client._log(f"Using session token: {token[:20]}...")
    elif args.register:
        if not (email and password):
            print("[Error] --register requires --email and --password", file=sys.stderr)
            sys.exit(1)
        try:
            token = client.register(email, password, args.first_name, args.last_name)
            print(f"[Registered] token: {token}")
        except Exception as e:
            print(f"[Registration failed: {e}]", file=sys.stderr)
            sys.exit(1)
        if not args.message and not args.interactive and not args.vibe:
            sys.exit(0)
    elif email and password:
        try:
            tok = client.login(email, password)
            print(f"[Logged in] token: {tok}")
        except Exception as e:
            print(f"[Login failed: {e}]", file=sys.stderr)
            sys.exit(1)

    # ── Audio transcription ───────────────────────────────────────────────────
    if args.transcribe:
        if not client.session_token:
            print("[Error] --transcribe requires authentication.", file=sys.stderr)
            sys.exit(1)
        print(f"Transcribing: {args.transcribe} ...", file=sys.stderr)
        try:
            text = client.transcribe(args.transcribe)
            print(text)
        except Exception as e:
            print(f"[Error] {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ── Direct read-aloud (--speak CHATID:MSGID:VERSION) ─────────────────────
    if args.speak:
        if not client.session_token:
            print("[Error] --speak requires authentication.", file=sys.stderr)
            sys.exit(1)
        try:
            parts = args.speak.split(":", 2)
            if len(parts) != 3:
                raise ValueError("--speak expects CHATID:MSGID:VERSION")
            chat_id, msg_id, msg_ver = parts
        except ValueError as e:
            print(f"[Error] {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Reading aloud: chatId={chat_id}, msgId={msg_id}, version={msg_ver}", file=sys.stderr)
        try:
            pcm = client.read_aloud(chat_id, msg_id, msg_ver)
            if args.output:
                wav = pcm_float32_to_wav(pcm)
                with open(args.output, "wb") as fh:
                    fh.write(wav)
                print(f"WAV saved: {args.output} ({len(wav)} bytes)", file=sys.stderr)
            else:
                sys.stdout.buffer.write(pcm)
        except Exception as e:
            print(f"[Error] {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ── TTS ───────────────────────────────────────────────────────────────────
    if args.tts:
        if not client.session_token:
            print("[Error] --tts requires authentication.", file=sys.stderr)
            sys.exit(1)
        print(f"TTS: {args.tts!r}", file=sys.stderr)
        try:
            pcm = client.tts(args.tts)
            if args.output:
                wav = pcm_float32_to_wav(pcm)
                with open(args.output, "wb") as fh:
                    fh.write(wav)
                print(f"WAV saved: {args.output} ({len(wav)} bytes)", file=sys.stderr)
            else:
                sys.stdout.buffer.write(pcm)
        except Exception as e:
            print(f"[Error] {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ── Image generation ─────────────────────────────────────────────────────
    if args.generate_image:
        if not client.session_token:
            print("[Error] --generate-image requires authentication (--token or --email+password).", file=sys.stderr)
            sys.exit(1)
        print(f"Generating image: {args.generate_image!r} ...", file=sys.stderr)
        try:
            url = client.generate_image(args.generate_image)
            print(url)
        except Exception as e:
            print(f"[Error] {e}", file=sys.stderr)
            sys.exit(1)
        return

    # ── Vision ───────────────────────────────────────────────────────────────
    if args.image:
        vision_message = args.message or "Describe this image"
        print(f"Image: {args.image}", file=sys.stderr)
        print(f"Question: {vision_message}", file=sys.stderr)
        print("Assistant: ", end="", flush=True)
        try:
            for token in client.send_image(args.image, vision_message):
                print(token, end="", flush=True)
        except Exception as e:
            print(f"\n[Error] {e}", file=sys.stderr)
            sys.exit(1)
        print()
        return

    # ── Vibe Code commands ────────────────────────────────────────────────────
    is_vibe_cmd = (args.vibe or args.vibe_session or args.vibe_projects
                   or args.vibe_sessions or args.vibe_artifacts
                   or args.vibe_download or args.vibe_send or args.vibe_files)
    if is_vibe_cmd:
        if not client.session_token:
            print("[Error] Vibe Code requires authentication.", file=sys.stderr)
            print("        Set MISTRAL_SESSION_TOKEN in .env or pass --token / --email+password", file=sys.stderr)
            sys.exit(1)

        client.bootstrap_session()
        vibe = VibeCode(client)

        if args.vibe_projects:
            projects = vibe.list_projects()
            if not projects:
                print("No projects found.")
            for p in projects:
                print(f"{p['id']}  {p['name']}  ({p.get('createdAt','?')[:10]})")
            return

        if args.vibe_sessions:
            sessions = vibe.list_sessions(args.vibe_sessions)
            if not sessions:
                print("No sessions found.")
            for s in sessions:
                print(f"{s['id']}  {s.get('status','?'):10s}  {s.get('title','(no title)')}")
            return

        if args.vibe_artifacts:
            artifacts = vibe.list_artifacts(args.vibe_artifacts)
            if not artifacts:
                print("No artifacts found. (Session may still be running or nothing was built.)")
            for a in artifacts:
                print(f"{a['id']}  {a.get('externalKey','?')}")
                if a.get('displayTitle'):
                    print(f"     title: {a['displayTitle']}")
                if a.get('externalUrl'):
                    print(f"     url:   {a['externalUrl']}")
            return

        if args.vibe_download:
            session_id = args.vibe_download
            artifacts = vibe.list_artifacts(session_id)
            if not artifacts:
                print("No artifacts to download.")
                return
            import os as _os
            for a in artifacts:
                key = a.get("externalKey", a["id"])
                filename = _os.path.basename(key) or a["id"]
                print(f"Downloading {key} → {filename} ...", end=" ", flush=True)
                try:
                    data = vibe.download_artifact(a)
                    with open(filename, "wb") as fh:
                        fh.write(data)
                    print(f"OK ({len(data)} bytes)")
                except Exception as e:
                    print(f"FAILED: {e}")
            return

        if args.vibe_files:
            files = vibe.get_files_from_messages(args.vibe_files)
            if not files:
                print("No files found in session messages.")
                return
            import os as _os
            for f in files:
                path = f["path"]
                filename = _os.path.basename(path) or "output.txt"
                print(f"{path}  ({f['size']} bytes)  → ./{filename}")
                with open(filename, "w", encoding="utf-8") as fh:
                    fh.write(f["content"])
            print(f"\n{len(files)} file(s) saved.")
            return

        if args.vibe_send:
            session_id, message = args.vibe_send
            result = vibe.send_message(session_id, message)
            print(f"Message sent. Streaming response...")
            try:
                for ev in vibe.subscribe(session_id):
                    etype = ev.get("type")
                    if etype == "text":
                        print(ev["value"], end="", flush=True)
                    elif etype == "status":
                        print(f"\n[{ev['value'].upper()}]", file=sys.stderr)
                        if ev["value"] in ("complete", "failed"):
                            break
                    elif etype == "error":
                        print(f"\n[ERROR] {ev['value']}", file=sys.stderr)
                        break
            except KeyboardInterrupt:
                pass
            print()
            return

        try:
            vibe.run(
                prompt=args.vibe or "",
                project_name=args.vibe_project,
                existing_session_id=args.vibe_session,
                interactive=not args.no_interactive,
            )
        except KeyboardInterrupt:
            print("\n[Interrupted]", file=sys.stderr)
        except Exception as e:
            print(f"[Vibe Code error: {e}]", file=sys.stderr)
            sys.exit(1)
        return

    # ── Regular chat ──────────────────────────────────────────────────────────
    if args.interactive:
        print(f"Mistral anonymous chat — model: {args.model}")
        print("Commands: 'new' = new chat, 'exit' = quit\n")
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                break
            if user_input.lower() in ("new", "reset"):
                client.reset()
                print("[New chat started]")
                continue
            try:
                client.chat(user_input, args.model)
            except RateLimitError:
                break
            except Exception as e:
                print(f"[Error: {e}]", file=sys.stderr)
        return

    if not args.message:
        parser.print_help()
        sys.exit(1)

    try:
        client.chat(args.message, args.model)
    except RateLimitError as e:
        print(f"\n[Rate limit. Retry in ~{e.retry_after // 3600}h]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
