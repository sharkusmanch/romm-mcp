"""Wire-level regression tests for RomM's hidden DAV routes and asynchronous downloads."""

from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from romm_mcp.api import APIRequest, FullAPI
from romm_mcp.catalog import Catalog, Operation

DAV = "/api/sync/retroarch/{file_path}"


def make_api(handler, method="get", spec=None, **settings):
    defaults = {
        **{
            f"allow_{gate}": True
            for gate in ("files", "writes", "destructive", "admin", "auth", "tasks")
        },
        "romm_url": "https://romm.example",
        "romm_token": "test-secret",
        "romm_username": "u",
        "romm_password": "p",
        "transfer_directory": None,
        "max_transfer_bytes": 1024 * 1024,
    }
    a = FullAPI(SimpleNamespace(**(defaults | settings)), transport=httpx.MockTransport(handler))
    a._catalog = Catalog({"paths": {}}, a.settings)
    a._catalog.operations["dav"] = Operation(
        "dav",
        method,
        DAV,
        spec
        or {
            "x-romm-webdav": True,
            "parameters": [
                {"in": "path", "name": "file_path", "required": True, "schema": {"type": "string"}},
                {"in": "header", "name": "Destination", "schema": {"type": "string"}},
                {"in": "header", "name": "Depth", "schema": {"type": "string", "enum": ["0", "1"]}},
            ],
        },
    )
    return a


async def test_conversion_202_does_not_create_download_artifact():
    a = make_api(lambda r: httpx.Response(202, headers={"Retry-After": "5"}))
    a._catalog.operations["dav"] = Operation("dav", "get", "/api/test", {})
    try:
        result = await a.call("dav", APIRequest(download=True))
        assert result["status_code"] == 202
        assert result["pending"] is True
        assert result["headers"]["retry-after"] == "5"
        assert "artifact" not in result
    finally:
        a.close()


@pytest.mark.parametrize(
    "file_path, wire",
    [
        ("", b"/api/sync/retroarch/"),
        ("saves/core/Game name.srm", b"/api/sync/retroarch/saves/core/Game%20name.srm"),
    ],
)
async def test_dav_root_and_nested_path_encoding(file_path, wire):
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(204), method="options")
    try:
        await a.call("dav", APIRequest(path={"file_path": file_path}))
        assert seen[0].url.raw_path == wire
    finally:
        a.close()


@pytest.mark.parametrize(
    "path",
    [
        "../users",
        "/api/users",
        "//evil/x",
        "saves//x",
        "saves/./x",
        "saves/%252e%252e/x",
        "saves/%2f..%2fusers",
        "saves/\\x",
        "saves/a\x7f",
        "https://evil/x",
        "x" * 2049,
    ],
)
async def test_dav_rejects_ambiguous_paths_before_dispatch(path):
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(204), method="options")
    try:
        with pytest.raises(ToolError):
            await a.call("dav", APIRequest(path={"file_path": path}))
        assert not seen
    finally:
        a.close()


async def test_propfind_xml_body_and_multistatus_are_bounded_text():
    seen = []
    a = make_api(
        lambda r: (
            seen.append(r)
            or httpx.Response(
                207,
                text="<D:multistatus>" + "x" * 6000,
                headers={"content-type": "application/xml"},
            )
        ),
        method="propfind",
    )
    try:
        result = await a.call(
            "dav",
            APIRequest(
                path={"file_path": ""},
                headers={"Depth": "1"},
                xml_body='<D:propfind xmlns:D="DAV:"/>',
            ),
        )
        assert seen[0].method == "PROPFIND"
        assert seen[0].content == b'<D:propfind xmlns:D="DAV:"/>'
        assert seen[0].headers["content-type"] == "application/xml; charset=utf-8"
        assert result["status_code"] == 207 and len(result["data"]) == 4096
        assert result["next_offset"] == 4096
    finally:
        a.close()


async def test_dav_put_streams_existing_artifact():
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(201), method="put")
    try:
        artifact = a.artifacts.create()["artifact_id"]
        a.artifacts.append_bytes(artifact, b"save\x00bytes")
        result = await a.call(
            "dav",
            APIRequest(path={"file_path": "saves/core/Game.srm"}, raw_artifact_id=artifact),
            write=True,
        )
        assert seen[0].content == b"save\x00bytes"
        assert seen[0].headers["content-type"] == "application/octet-stream"
        assert result["status_code"] == 201
        assert result["verification"] == "not_performed"
    finally:
        a.close()


@pytest.mark.parametrize(
    "destination",
    [
        "https://evil.example/x",
        "https://romm.example/api/users",
        "/api/users",
        "//evil.example/x",
        "deleted/%2e%2e/x",
        "deleted/../x",
        "deleted/\r\nx",
    ],
)
async def test_move_rejects_unsafe_destination_before_dispatch(destination):
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(204), method="move")
    try:
        with pytest.raises(ToolError):
            await a.call(
                "dav",
                APIRequest(
                    path={"file_path": "saves/core/x"}, headers={"Destination": destination}
                ),
                write=True,
            )
        assert not seen
    finally:
        a.close()


async def test_move_destination_is_fixed_origin_dav_path():
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(204), method="move")
    try:
        await a.call(
            "dav",
            APIRequest(
                path={"file_path": "saves/core/x"},
                headers={"Destination": "deleted/saves/core/x y"},
            ),
            write=True,
        )
        assert (
            seen[0].headers["destination"]
            == "https://romm.example/api/sync/retroarch/deleted/saves/core/x%20y"
        )
    finally:
        a.close()


async def test_dav_options_and_lock_headers_survive():
    a = make_api(
        lambda r: httpx.Response(
            200,
            headers={
                "DAV": "1, 2",
                "Allow": "GET, PROPFIND",
                "Lock-Token": "<opaquelocktoken:1>",
                "Set-Cookie": "secret",
            },
        ),
        method="options",
    )
    try:
        result = await a.call("dav", APIRequest(path={"file_path": ""}))
        assert result["headers"]["dav"] == "1, 2"
        assert result["headers"]["allow"] == "GET, PROPFIND"
        assert result["headers"]["lock-token"] == "<opaquelocktoken:1>"
        assert "set-cookie" not in result["headers"]
    finally:
        a.close()


async def test_pending_download_keeps_json_job_metadata_without_artifact():
    a = make_api(lambda r: httpx.Response(202, json={"job_id": "abc"}))
    a._catalog.operations["dav"] = Operation("dav", "get", "/api/test", {})
    try:
        result = await a.call("dav", APIRequest(download=True))
        assert result["pending"] is True
        assert result["data"] == {"job_id": "abc"}
        assert "artifact" not in result
    finally:
        a.close()


async def test_xml_rejects_excessive_utf8_before_dispatch():
    body = "é" * 32769
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(207), method="propfind")
    try:
        with pytest.raises(ToolError):
            await a.call("dav", APIRequest(path={"file_path": ""}, xml_body=body))
        assert not seen
    finally:
        a.close()


@pytest.mark.parametrize(
    "api_request",
    [
        APIRequest(path={"file_path": ""}, xml_body="<propfind/>", body={}),
        APIRequest(path={"file_path": ""}, xml_body="<propfind/>", form={}),
        APIRequest(path={"file_path": ""}, xml_body="<propfind/>", raw_artifact_id="missing"),
    ],
)
async def test_xml_cannot_mix_with_other_body_encodings(api_request):
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(207), method="propfind")
    try:
        with pytest.raises(ToolError):
            await a.call("dav", api_request, write=True)
        assert not seen
    finally:
        a.close()


async def test_notification_other_recipients_require_admin_before_dispatch():
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(201), allow_admin=False)
    a._catalog.operations["dav"] = Operation(
        "dav",
        "post",
        "/api/notifications",
        {"requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}},
    )
    try:
        with pytest.raises(ToolError, match="ADMIN"):
            await a.call("dav", APIRequest(body={"recipients": [2], "message": "hi"}), write=True)
        assert not seen
        await a.call("dav", APIRequest(body={"recipients": None, "message": "hi"}), write=True)
        assert len(seen) == 1
    finally:
        a.close()


async def test_device_http_auth_requires_dedicated_token_without_dispatch():
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(200), method="options")
    try:
        with pytest.raises(ToolError, match="ROMM_DEVICE_TOKEN"):
            await a.call("dav", APIRequest(path={"file_path": ""}, auth_mode="device"))
        assert not seen
    finally:
        a.close()


async def test_device_http_auth_is_isolated_from_main_token_and_cookies():
    seen = []
    a = make_api(
        lambda r: seen.append(r) or httpx.Response(200, headers={"Set-Cookie": "device=secret"}),
        method="options",
        romm_device_token="device-only-secret",
    )
    try:
        await a.call("dav", APIRequest(path={"file_path": ""}, auth_mode="device"))
        await a.call("dav", APIRequest(path={"file_path": ""}))
        assert seen[0].headers["authorization"] == "Bearer device-only-secret"
        assert seen[1].headers["authorization"] == "Bearer test-secret"
        assert all("cookie" not in request.headers for request in seen)
        with pytest.raises(ToolError):
            await a.call(
                "dav", APIRequest(path={"file_path": ""}, auth_mode="device", auth_session="new")
            )
        assert len(seen) == 2
    finally:
        a.close()


@pytest.mark.parametrize(
    "method",
    ["OPTIONS", "LOCK", "UNLOCK", "PROPFIND", "GET", "HEAD", "PUT", "DELETE", "MOVE", "MKCOL"],
)
async def test_supplemented_catalog_dispatches_each_hidden_dav_method(method):
    seen = []
    a = make_api(lambda r: seen.append(r) or httpx.Response(204))
    a._catalog = Catalog({"info": {"version": "5.4.0"}, "paths": {}}, a.settings)
    try:
        fields = {"path": {"file_path": "saves/core/Game.srm"}}
        if method == "PUT":
            artifact = a.artifacts.create()["artifact_id"]
            a.artifacts.append_bytes(artifact, b"save-bytes")
            fields["raw_artifact_id"] = artifact
        if method == "MOVE":
            fields["headers"] = {"Destination": "deleted/saves/core/Game.srm"}
        if method == "PROPFIND":
            fields["xml_body"] = '<D:propfind xmlns:D="DAV:"/>'
            fields["headers"] = {"Depth": "0"}
        result = await a.call(
            "romm_webdav_" + method.lower(),
            APIRequest(**fields),
            write=method not in ("OPTIONS", "PROPFIND"),
        )
        assert result["status_code"] == 204
        assert seen[0].method == method
        assert seen[0].url.raw_path == b"/api/sync/retroarch/saves/core/Game.srm"
        assert seen[0].headers["authorization"] == "Bearer test-secret"
    finally:
        a.close()


async def test_dav_redirect_is_reported_without_sending_credentials_to_target():
    seen = []
    a = make_api(
        lambda r: (
            seen.append(r)
            or httpx.Response(307, headers={"Location": "https://other.example/game"})
        )
    )
    a._catalog = Catalog({"info": {"version": "5.4.0"}, "paths": {}}, a.settings)
    try:
        result = await a.call(
            "romm_webdav_get",
            APIRequest(path={"file_path": "roms/psx/Game.cue"}, download=True),
            write=True,
        )
        assert result["redirect_followed"] is False
        assert "artifact" not in result
        assert len(seen) == 1
    finally:
        a.close()
