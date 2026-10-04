"""Bounded Socket.IO sessions for RomM's verified main and netplay events."""

import asyncio
import base64
import binascii
import json
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import socketio
from mcp.server.mcpserver.exceptions import ToolError

MAX_BYTES = 16_384
MAX_SESSIONS = 4
MAX_PENDING_ACKS = 20
SESSION_TTL = 300
PATHS = {"main": "ws/socket.io", "netplay": "netplay/socket.io"}
CALL_EVENTS = {
    "main": {
        "scan": "Object: platforms, platform_fs_slugs, type, roms_ids, apis, "
        "launchbox_remote_enabled. Starts a background scan.",
        "scan:stop": "No payload. Stops running and queued scans.",
        "activity:start": "Object: rom_id (integer), device_id (string).",
        "activity:heartbeat": "Object: rom_id (integer), device_id (string).",
        "activity:stop": "Optional object: device_id; otherwise uses this session's device.",
    },
    "netplay": {
        "open-room": "Object: extra {sessionid, userid or playerId, optional room_name, "
        "game_id, domain, player_name, room_password}; optional maxPlayers.",
        "join-room": "Object: extra {sessionid, userid or playerId, optional room_password}.",
        "leave-room": "No payload. Leaves the room joined by this socket session.",
        "webrtc-signal": "Object: target; optional candidate, offer, answer, requestRenegotiate.",
        "webrtc-signal-error": "Two-argument event: JSON array [error_string, data].",
        "data-message": "Any JSON payload, broadcast to this session's room.",
        "snapshot": "Any JSON payload, broadcast to this session's room.",
        "input": "Any JSON payload, broadcast to this session's room.",
    },
}
LISTEN_EVENTS = {
    "main": {
        "scan:done",
        "scan:done_ko",
        "scan:scanning_platform",
        "scan:scanning_rom",
        "scan:update_stats",
        "activity:update",
        "activity:clear",
        "logs:entry",
        "sync:started",
        "sync:progress",
        "sync:completed",
        "sync:conflict",
        "sync:error",
        "streaming:launch-ready",
        "streaming:launch-failed",
        "streaming:launch-phase",
        "streaming:session-ended",
        "permissions:changed",
    },
    "netplay": {"users-updated", "webrtc-signal", "data-message", "snapshot", "input"},
}


@dataclass
class Session:
    service: str
    client: Any
    http: aiohttp.ClientSession
    events: deque = field(default_factory=lambda: deque(maxlen=20))
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    truncated: bool = False
    expiry: asyncio.TimerHandle | None = None
    pending_acks: int = 0
    closing: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class SocketAPI:
    def __init__(self, settings):
        self.settings = settings
        self.sessions: dict[str, Session] = {}
        self.lock = asyncio.Lock()

    def _gate(self, name):
        if not getattr(self.settings, "allow_" + name, False):
            raise ToolError(f"Set ROMM_ALLOW_{name.upper()}=true to enable this operation.")

    def catalog(self) -> dict:
        return {
            "services": {
                service: {
                    "path": path,
                    "call_events": CALL_EVENTS[service],
                    "listen_events": sorted(LISTEN_EVENTS[service]),
                }
                for service, path in PATHS.items()
            },
            "requirements": {
                "open": ["ROMM_ALLOW_REALTIME"],
                "send": ["ROMM_ALLOW_REALTIME", "ROMM_ALLOW_WRITES", "ROMM_ALLOW_TASKS"],
                "scan": [
                    "ROMM_ALLOW_REALTIME",
                    "ROMM_ALLOW_WRITES",
                    "ROMM_ALLOW_TASKS",
                    "ROMM_ALLOW_FILES",
                    "ROMM_ALLOW_DESTRUCTIVE",
                ],
                "main_auth": "ROMM_SESSION_COOKIE: value of RomM's romm_session browser cookie; "
                "RomM 5.3.1 does not authenticate these sockets with bearer tokens.",
                "logs": "ROMM_ALLOW_ADMIN required to capture logs:entry.",
            },
            "limits": {
                "sessions": MAX_SESSIONS,
                "ttl_seconds": SESSION_TTL,
                "events": 20,
                "json_bytes": MAX_BYTES,
                "listen_seconds": 10,
                "pending_acknowledgements": MAX_PENDING_ACKS,
            },
            "lifecycle": "Open, send/listen on the same session, close. Sessions expire after "
            "300 seconds. Disconnect clears activity and leaves netplay rooms; scans continue.",
            "binary": 'Represent binary event values as {"$binary_base64":"..."}; '
            "the same wrapper appears in captured binary events, within the JSON size limit.",
        }

    def _encode(self, value):
        try:
            return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (ValueError, TypeError, RecursionError):
            raise ToolError("Socket payload must be finite JSON.") from None

    def _safe(self, value):
        if isinstance(value, bytes):
            if len(value) > MAX_BYTES // 3:
                return {"binary_bytes": len(value), "omitted": True}
            for name in ("romm_token", "romm_session_cookie", "auth_token", "romm_password"):
                secret = getattr(self.settings, name, "")
                if secret:
                    value = value.replace(str(secret).encode(), b"[redacted]")
            return {"$binary_base64": base64.b64encode(value).decode("ascii")}
        if isinstance(value, dict):
            return {self._safe(str(k)): self._safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._safe(v) for v in value]
        if isinstance(value, str):
            for name in ("romm_token", "romm_session_cookie", "auth_token", "romm_password"):
                secret = getattr(self.settings, name, "")
                if secret:
                    value = value.replace(str(secret), "[redacted]")
            return value
        return value

    def _wire(self, value):
        if isinstance(value, dict):
            if set(value) == {"$binary_base64"}:
                try:
                    return base64.b64decode(value["$binary_base64"], validate=True)
                except (binascii.Error, ValueError, TypeError):
                    raise ToolError("Invalid $binary_base64 payload.") from None
            return {key: self._wire(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._wire(item) for item in value]
        return value

    def _capture(self, session, event, payload):
        if event == "logs:entry" and not getattr(self.settings, "allow_admin", False):
            return
        try:
            safe = self._safe(payload)
            encoded = self._encode(safe)
        except (ToolError, RecursionError):
            safe = {"omitted": True, "reason": "Unsupported event payload"}
            encoded = ""
        if len(encoded.encode()) > MAX_BYTES // 2:
            safe = {"omitted": True, "bytes": len(encoded.encode())}
            session.truncated = True
        if len(session.events) == session.events.maxlen:
            session.truncated = True
        session.events.append({"event": event, "data": safe})
        session.changed.set()

    async def open(self, service: str) -> dict:
        self._gate("realtime")
        if service not in PATHS:
            raise ToolError("Socket service must be main or netplay.")
        parsed = urlsplit(self.settings.romm_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ToolError("ROMM_URL must be an HTTP(S) base URL without credentials or query.")
        headers = {"Authorization": "Bearer " + self.settings.romm_token}
        if service == "main":
            cookie = getattr(self.settings, "romm_session_cookie", "")
            if not cookie:
                raise ToolError(
                    "Main Socket.IO requires ROMM_SESSION_COOKIE; bearer is unsupported."
                )
            if any(char in cookie for char in "\r\n;"):
                raise ToolError("ROMM_SESSION_COOKIE must contain only the cookie value.")
            headers["Cookie"] = "romm_session=" + cookie
        async with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                raise ToolError("Socket session limit reached; close an existing session.")
            # aiohttp ws_connect follows redirects and Engine.IO puts session cookies into
            # a domainless cookie jar. Stop every redirect before credentials can leave RomM.
            trace = aiohttp.TraceConfig()

            async def reject_redirect(http_session, trace_context, params):
                params.response.close()
                raise ToolError("Socket redirects are disabled; configure the direct RomM URL.")

            trace.on_request_redirect.append(reject_redirect)
            http = aiohttp.ClientSession(trace_configs=[trace], trust_env=False)
            client = socketio.AsyncClient(
                reconnection=False,
                logger=False,
                engineio_logger=False,
                request_timeout=5,
                http_session=http,
            )
            session = Session(service, client, http)

            async def receive(event, *args):
                if event in LISTEN_EVENTS[service]:
                    self._capture(session, event, args[0] if len(args) == 1 else list(args))

            client.on("*", receive)
            try:
                async with asyncio.timeout(10):
                    prefix = parsed.path.strip("/")
                    await client.connect(
                        f"{parsed.scheme}://{parsed.netloc}",
                        headers=headers,
                        socketio_path="/".join(filter(None, [prefix, PATHS[service]])),
                        wait_timeout=5,
                        transports=["websocket"],
                        retry=False,
                    )
            except asyncio.CancelledError:
                await self._disconnect(client, http)
                raise
            except Exception:
                await self._disconnect(client, http)
                raise ToolError(
                    "RomM socket connection failed; check session/auth and endpoint."
                ) from None
            identifier = secrets.token_urlsafe(24)
            self.sessions[identifier] = session
            session.expiry = asyncio.get_running_loop().call_later(
                SESSION_TTL, lambda: asyncio.create_task(self.close(identifier))
            )
        return {"session_id": identifier, "service": service, "expires_in_seconds": SESSION_TTL}

    def _session(self, identifier):
        self._gate("realtime")
        if identifier not in self.sessions or self.sessions[identifier].closing:
            raise ToolError("Socket session is unknown or expired; open a new session.")
        return self.sessions[identifier]

    async def send(self, session_id: str, event: str, payload=None) -> dict:
        session = self._session(session_id)
        self._gate("writes")
        self._gate("tasks")
        if event not in CALL_EVENTS[session.service]:
            raise ToolError("Unknown event for this service; inspect the socket catalog.")
        if event == "scan":
            # Complete scans can remove stale game assets; all scan modes share one safe gate.
            self._gate("files")
            self._gate("destructive")
        if len(self._encode(payload).encode()) > MAX_BYTES:
            raise ToolError("Socket payload exceeds 16384 bytes.")
        payload = self._wire(payload)
        if event in {"scan:stop", "leave-room"} and payload is not None:
            raise ToolError("This event accepts no payload.")
        if event == "webrtc-signal-error":
            if not isinstance(payload, list) or len(payload) != 2:
                raise ToolError("webrtc-signal-error requires [error, data].")
            payload = tuple(payload)
        if payload is None and event in {"data-message", "snapshot", "input"}:
            payload = (None,)

        async def acknowledgement(*args):
            session.pending_acks -= 1
            self._capture(session, "ack:" + event, args[0] if len(args) == 1 else list(args))

        try:
            async with asyncio.timeout(5):
                async with session.send_lock:
                    if session.pending_acks >= MAX_PENDING_ACKS:
                        raise ToolError(
                            "Pending socket acknowledgement limit reached; wait for a reply"
                            " or close the session. Previous sends are not retried."
                        )
                    session.pending_acks += 1
                    await session.client.emit(event, data=payload, callback=acknowledgement)
        except ToolError:
            raise
        except asyncio.CancelledError:
            await self.close(session_id)
            raise
        except Exception:
            await self.close(session_id)
            raise ToolError(
                "Socket send failed; outcome uncertain. Check state before retrying."
            ) from None
        return {
            "session_id": session_id,
            "event": event,
            "sent": True,
            "note": "Sent is transport acceptance; use listen and REST state to verify effects.",
        }

    async def listen(self, session_id: str, seconds: float = 2, max_events: int = 20) -> dict:
        session = self._session(session_id)
        if not 0 < seconds <= 10 or not 1 <= max_events <= 20:
            raise ToolError("seconds must be >0 and <=10; max_events must be 1..20.")
        if not session.events:
            session.changed.clear()
            try:
                await asyncio.wait_for(session.changed.wait(), timeout=seconds)
            except TimeoutError:
                pass
        events = []
        size = 0
        while session.events and len(events) < max_events:
            event = session.events[0]
            if event["event"] == "logs:entry" and not getattr(self.settings, "allow_admin", False):
                session.events.popleft()
                continue
            length = len(self._encode(event).encode())
            if size + length > MAX_BYTES - 512:
                break
            events.append(session.events.popleft())
            size += length
        result = {
            "session_id": session_id,
            "events": events,
            "remaining": len(session.events),
            "truncated": session.truncated,
        }
        session.truncated = False
        return result

    async def _disconnect(self, client, http):
        try:
            async with asyncio.timeout(5):
                await client.disconnect()
        except Exception:
            pass
        finally:
            await http.close()

    async def close(self, session_id: str) -> dict:
        # Cleanup must remain available after an operator disables feature flags.
        session = self.sessions.get(session_id)
        if session:
            if session.closing:
                await session.closed.wait()
            else:
                # Retain the quota slot until transport cleanup actually finishes.
                session.closing = True
                try:
                    if session.expiry:
                        session.expiry.cancel()
                    await self._disconnect(session.client, session.http)
                finally:
                    self.sessions.pop(session_id, None)
                    session.closed.set()
                    session.changed.set()
        return {"session_id": session_id, "closed": True}

    async def aclose(self):
        for identifier in list(self.sessions):
            await self.close(identifier)
