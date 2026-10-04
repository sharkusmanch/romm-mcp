from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from romm_mcp.sockets import SocketAPI


def settings(**changes):
    values = dict(
        romm_url="https://romm.example",
        romm_token="token-secret",
        romm_session_cookie="session-secret",
        allow_realtime=True,
        allow_writes=True,
        allow_tasks=True,
        allow_admin=False,
        allow_files=True,
        allow_destructive=True,
    )
    values.update(changes)
    return SimpleNamespace(**values)


class FakeSocket:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.handlers = {}
        self.sent = []
        self.disconnected = False

    def on(self, name, handler):
        self.handlers[name] = handler

    async def connect(self, url, **kwargs):
        self.connection = (url, kwargs)

    async def emit(self, event, data=None, callback=None):
        self.sent.append((event, data))
        await self.handlers["*"]("activity:update", {"token": "token-secret"})
        if callback:
            await callback({"ok": True})

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake(monkeypatch):
    instance = FakeSocket()
    monkeypatch.setattr("romm_mcp.sockets.socketio.AsyncClient", lambda **kw: instance)
    return instance


@pytest.mark.parametrize("flag", ["allow_realtime", "allow_writes", "allow_tasks"])
async def test_calls_require_every_gate(flag, fake):
    api = SocketAPI(settings())
    opened = await api.open("main")
    setattr(api.settings, flag, False)
    with pytest.raises(ToolError, match="ROMM_ALLOW"):
        await api.send(opened["session_id"], "scan", {})
    assert not fake.sent
    await api.aclose()


@pytest.mark.parametrize("flag", ["allow_files", "allow_destructive"])
@pytest.mark.parametrize("scan_type", ["quick", "complete"])
async def test_all_scans_require_file_and_destructive_gates(flag, scan_type, fake):
    api = SocketAPI(settings(**{flag: False}))
    identifier = (await api.open("main"))["session_id"]
    with pytest.raises(ToolError, match="ROMM_ALLOW"):
        await api.send(identifier, "scan", {"type": scan_type})
    assert not fake.sent
    await api.aclose()


async def test_pending_acknowledgements_bound_real_client_registry(monkeypatch):
    import socketio

    real_client = socketio.AsyncClient()
    real_client.namespaces["/"] = "sid"

    async def noop(*args, **kwargs):
        return None

    real_client.connect = noop
    real_client.disconnect = noop
    real_client._send_packet = noop
    monkeypatch.setattr("romm_mcp.sockets.socketio.AsyncClient", lambda **kw: real_client)
    api = SocketAPI(settings())
    identifier = (await api.open("netplay"))["session_id"]
    try:
        for _ in range(20):
            await api.send(identifier, "input", {})
        with pytest.raises(ToolError, match="acknowledgement limit"):
            await api.send(identifier, "input", {})
        assert len(real_client.callbacks["/"]) == 21  # Counter plus twenty callbacks.
        await real_client._handle_ack("/", 1, [{"ok": True}])
        await api.send(identifier, "input", {})
        assert len(real_client.callbacks["/"]) == 21
    finally:
        await api.aclose()


async def test_websocket_redirect_never_contacts_target_with_cookie():
    from aiohttp import web

    hits = []

    async def target(request):
        hits.append(dict(request.headers))
        return web.Response(status=400)

    target_app = web.Application()
    target_app.router.add_get("/{tail:.*}", target)
    target_runner = web.AppRunner(target_app)
    await target_runner.setup()
    target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
    await target_site.start()
    target_port = target_site._server.sockets[0].getsockname()[1]

    async def redirect(request):
        raise web.HTTPFound(f"http://localhost:{target_port}/leak")

    source_app = web.Application()
    source_app.router.add_get("/{tail:.*}", redirect)
    source_runner = web.AppRunner(source_app)
    await source_runner.setup()
    source_site = web.TCPSite(source_runner, "127.0.0.1", 0)
    await source_site.start()
    source_port = source_site._server.sockets[0].getsockname()[1]
    api = SocketAPI(settings(romm_url=f"http://127.0.0.1:{source_port}"))
    try:
        with pytest.raises(ToolError, match="connection failed"):
            await api.open("main")
        assert hits == []
        assert not api.sessions
    finally:
        await api.aclose()
        await source_runner.cleanup()
        await target_runner.cleanup()


async def test_main_requires_session(fake):
    with pytest.raises(ToolError, match="ROMM_SESSION_COOKIE"):
        await SocketAPI(settings(romm_session_cookie="")).open("main")


async def test_main_transport_redacts_keeps_session_and_disconnects(fake):
    api = SocketAPI(settings())
    opened = await api.open("main")
    identifier = opened["session_id"]
    await api.send(identifier, "activity:start", {"rom_id": 1, "device_id": "dev"})
    result = await api.listen(identifier, seconds=0.01)
    assert fake.connection[1]["socketio_path"] == "ws/socket.io"
    assert fake.connection[1]["headers"]["Cookie"] == "romm_session=session-secret"
    assert not fake.disconnected
    assert "token-secret" not in str(result)
    assert result["events"][-1] == {"event": "ack:activity:start", "data": {"ok": True}}
    await api.close(identifier)
    assert fake.disconnected


async def test_invalid_event_limits_and_payload_fail_before_send(fake):
    api = SocketAPI(settings())
    identifier = (await api.open("main"))["session_id"]
    with pytest.raises(ToolError):
        await api.send(identifier, "arbitrary", {})
    with pytest.raises(ToolError):
        await api.send(identifier, "scan", "x" * 17000)
    for kwargs in [dict(seconds=11), dict(seconds=float("nan")), dict(max_events=21)]:
        with pytest.raises(ToolError):
            await api.listen(identifier, **kwargs)
    assert not fake.sent
    await api.aclose()


async def test_failed_emit_disconnects_without_retry(fake):
    async def fail(*args, **kwargs):
        fake.sent.append(args)
        raise RuntimeError("session-secret")

    fake.emit = fail
    api = SocketAPI(settings())
    identifier = (await api.open("main"))["session_id"]
    with pytest.raises(ToolError, match="uncertain") as error:
        await api.send(identifier, "scan", {})
    assert "session-secret" not in str(error.value)
    assert len(fake.sent) == 1
    assert fake.disconnected
    assert not api.sessions


async def test_netplay_multiargument_and_noargument_events(fake):
    api = SocketAPI(settings(romm_session_cookie=""))
    identifier = (await api.open("netplay"))["session_id"]
    await api.send(identifier, "webrtc-signal-error", ["error", {}])
    assert fake.sent[0] == ("webrtc-signal-error", ("error", {}))
    assert fake.connection[1]["socketio_path"] == "netplay/socket.io"
    await api.send(identifier, "leave-room", None)
    assert fake.sent[-1] == ("leave-room", None)
    await api.aclose()


async def test_binary_event_roundtrip_with_explicit_json_wrapper(fake):
    api = SocketAPI(settings())
    identifier = (await api.open("netplay"))["session_id"]
    await api.send(identifier, "snapshot", {"$binary_base64": "AQID"})
    assert fake.sent[-1] == ("snapshot", b"\x01\x02\x03")
    await fake.handlers["*"]("snapshot", b"\x01\x02\x03")
    result = await api.listen(identifier, seconds=0.01)
    assert result["events"][-1]["data"] == {"$binary_base64": "AQID"}
    with pytest.raises(ToolError, match="Invalid"):
        await api.send(identifier, "snapshot", {"$binary_base64": "!"})
    await api.aclose()


async def test_capture_bounds_and_read_only_listener(fake):
    async def connect(*args, **kwargs):
        for _ in range(30):
            await fake.handlers["*"]("activity:update", {"large": "x" * 17000})

    fake.connect = connect
    api = SocketAPI(settings(allow_writes=False))
    identifier = (await api.open("main"))["session_id"]
    result = await api.listen(identifier, seconds=0.01, max_events=2)
    assert len(result["events"]) == 2
    assert len(str(result)) < 2000
    assert result["truncated"]
    await api.aclose()
    assert fake.disconnected


async def test_admin_log_capture_gate_and_recheck(fake):
    api = SocketAPI(settings())
    identifier = (await api.open("main"))["session_id"]
    await fake.handlers["*"]("logs:entry", {"message": "private"})
    assert not api.sessions[identifier].events
    api.settings.allow_admin = True
    await fake.handlers["*"]("logs:entry", {"message": "private"})
    assert api.sessions[identifier].events
    api.settings.allow_admin = False
    assert not (await api.listen(identifier, seconds=0.01))["events"]
    await api.aclose()


async def test_max_sessions_and_expiry(fake, monkeypatch):
    monkeypatch.setattr("romm_mcp.sockets.SESSION_TTL", 0.01)
    api = SocketAPI(settings())
    for _ in range(4):
        await api.open("netplay")
    with pytest.raises(ToolError, match="limit"):
        await api.open("netplay")
    import asyncio

    await asyncio.sleep(0.02)
    assert not api.sessions
    assert fake.disconnected


async def test_closing_sessions_keep_quota_until_disconnected(fake):
    import asyncio

    release = asyncio.Event()

    async def slow_disconnect():
        await release.wait()

    fake.disconnect = slow_disconnect
    api = SocketAPI(settings())
    identifiers = [(await api.open("netplay"))["session_id"] for _ in range(4)]
    closing = [asyncio.create_task(api.close(identifier)) for identifier in identifiers]
    await asyncio.sleep(0)
    try:
        with pytest.raises(ToolError, match="limit"):
            await api.open("netplay")
        with pytest.raises(ToolError, match="expired"):
            await api.send(identifiers[0], "input", {})
        assert len(api.sessions) == 4
    finally:
        release.set()
        await asyncio.gather(*closing)
    assert not api.sessions


async def test_base_path_and_unknown_services(fake):
    api = SocketAPI(settings(romm_url="https://romm.example/romm"))
    identifier = (await api.open("netplay"))["session_id"]
    assert fake.connection[1]["socketio_path"] == "romm/netplay/socket.io"
    with pytest.raises(ToolError):
        await api.open("https://evil.example")
    await api.close(identifier)
    with pytest.raises(ToolError, match="expired"):
        await api.listen(identifier)


def test_catalog_has_all_verified_client_events_without_credentials():
    result = SocketAPI(settings()).catalog()
    assert len(result["services"]["main"]["call_events"]) == 5
    assert len(result["services"]["netplay"]["call_events"]) == 8
    assert "session-secret" not in str(result)


async def test_real_socketio_persistent_room_and_multiple_arguments():
    import socketio
    from aiohttp import web

    server = socketio.AsyncServer(async_mode="aiohttp")
    app = web.Application()
    server.attach(app, socketio_path="netplay/socket.io")
    received_errors = []

    @server.on("open-room")
    @server.on("join-room")
    async def join(sid, payload):
        await server.enter_room(sid, payload["extra"]["sessionid"])

    @server.on("input")
    async def input_event(sid, payload):
        await server.emit("input", payload, room="test-room", skip_sid=sid)

    @server.on("webrtc-signal-error")
    async def signal_error(sid, error, data):
        received_errors.append([error, data])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    api = SocketAPI(settings(romm_url=f"http://127.0.0.1:{port}"))
    try:
        first = (await api.open("netplay"))["session_id"]
        second = (await api.open("netplay"))["session_id"]
        await api.send(first, "open-room", {"extra": {"sessionid": "test-room"}})
        await api.listen(first, seconds=1)
        await api.send(second, "join-room", {"extra": {"sessionid": "test-room"}})
        await api.listen(second, seconds=1)
        await api.send(first, "input", {"button": "A"})
        result = await api.listen(second, seconds=1)
        assert result["events"] == [{"event": "input", "data": {"button": "A"}}]
        await api.send(first, "input", None)
        result = await api.listen(second, seconds=1)
        assert result["events"] == [{"event": "input", "data": []}]
        await api.send(first, "webrtc-signal-error", ["test", {"detail": 1}])
        # Wait for this event's acknowledgement, which may follow input's acknowledgement.
        for _ in range(3):
            await api.listen(first, seconds=0.1)
            if received_errors:
                break
        assert received_errors == [["test", {"detail": 1}]]
    finally:
        await api.aclose()
        await runner.cleanup()
