import json
import urllib.request
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from romm_mcp.api import APIRequest, FullAPI, Upload, bounded
from romm_mcp.catalog import Catalog


def config(**kwargs):
    d = {
        f"allow_{x}": True
        for x in ["writes", "admin", "auth", "files", "tasks", "destructive", "realtime"]
    }
    d.update(
        romm_url="https://romm.example",
        romm_token="UPSTREAM_SECRET",
        romm_username="u",
        romm_password="p",
        transfer_directory=None,
        max_transfer_bytes=1024 * 1024,
    )
    d.update(kwargs)
    return SimpleNamespace(**d)


def api(handler, method="get", path="/api/test/{id}", **spec):
    a = FullAPI(config(), transport=httpx.MockTransport(handler))
    op = {
        "operationId": "test",
        "parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        **spec,
    }
    a._catalog = Catalog({"paths": {path: {method: op}}}, a.settings)
    return a


async def test_query_encoding_and_parameter_validation_before_send():
    calls = []

    def h(r):
        calls.append(r)
        assert r.headers["authorization"] == "Bearer UPSTREAM_SECRET"
        assert r.url.params.get_list("ids") == ["1", "2"]
        return httpx.Response(200, json={"ok": True})

    a = api(
        h,
        parameters=[
            {
                "name": "ids",
                "in": "query",
                "schema": {"type": "array", "items": {"type": "integer"}},
            }
        ],
    )
    try:
        result = await a.call("test", APIRequest(path={"id": "a b"}, query={"ids": [1, 2]}))
        assert result["data"] == {"ok": True}
        assert "%20" in str(calls[0].url)
        with pytest.raises(ToolError):
            await a.call("test", APIRequest(path={"id": "../users"}))
        with pytest.raises(ToolError):
            await a.call("test", APIRequest(path={"id": "a"}, headers={"Authorization": "x"}))
        assert len(calls) == 1
    finally:
        a.close()


async def test_json_delete_body_and_empty_success_unverified():
    def h(r):
        assert r.method == "DELETE"
        assert json.loads(r.content) == {"ids": [1]}
        return httpx.Response(204)

    a = api(
        h,
        method="delete",
        requestBody={"content": {"application/json": {"schema": {"type": "object"}}}},
    )
    try:
        r = await a.call("test", APIRequest(path={"id": "1"}, body={"ids": [1]}), write=True)
        assert r["data"] is None and r["verification"] == "not_performed"
    finally:
        a.close()


async def test_multipart_files_and_form():
    def h(r):
        assert "multipart/form-data" in r.headers["content-type"]
        assert b"abc" in r.content and b'name="description"' in r.content
        return httpx.Response(200, json={})

    a = api(h, method="post", requestBody={"content": {"multipart/form-data": {"schema": {}}}})
    try:
        handle = a.artifacts.create()["artifact_id"]
        a.artifacts.append_bytes(handle, b"abc")
        await a.call(
            "test",
            APIRequest(
                path={"id": "1"},
                form={"description": "test"},
                files=[Upload(field="file", artifact_id=handle, filename="test.dat")],
            ),
            write=True,
        )
    finally:
        a.close()


async def test_binary_stream_artifact_and_head():
    a = api(
        lambda r: httpx.Response(
            200, content=b"\0\1binary", headers={"Content-Type": "application/octet-stream"}
        )
    )
    try:
        r = await a.call("test", APIRequest(path={"id": "1"}, download=True))
        assert a.artifacts.info(r["artifact"]["artifact_id"])["size_bytes"] == 8
    finally:
        a.close()
    a = api(lambda r: httpx.Response(200, headers={"content-length": "123"}), method="head")
    try:
        assert (await a.call("test", APIRequest(path={"id": "1"})))["data"] is None
    finally:
        a.close()


async def test_redirect_does_not_forward_credentials_or_pollute_other_calls():
    calls = []

    def h(r):
        calls.append(r)
        assert "cookie" not in r.headers
        return httpx.Response(
            302, headers={"location": "https://evil.example", "set-cookie": "session=secret"}
        )

    a = api(h)
    try:
        for _ in range(2):
            assert (await a.call("test", APIRequest(path={"id": "1"})))[
                "redirect_followed"
            ] is False
        assert len(calls) == 2 and all(r.url.host == "romm.example" for r in calls)
    finally:
        a.close()


def test_long_text_can_be_reassembled_without_missing_characters():
    original = "abcd" * 15000
    offset = 0
    parts = []
    while True:
        r = bounded(original, offset=offset)
        parts.append(r["data"])
        if r["next_offset"] is None:
            break
        offset = r["next_offset"]
    assert "".join(parts) == original


async def test_gates_and_wrong_tool_prevent_transport():
    calls = []
    a = api(lambda r: calls.append(r), method="delete")
    try:
        a.settings.allow_destructive = False
        with pytest.raises(ToolError, match="DESTRUCTIVE"):
            await a.call("test", APIRequest(path={"id": "1"}), write=True)
        a.settings.allow_destructive = True
        with pytest.raises(ToolError, match="romm_api_write"):
            await a.call("test", APIRequest(path={"id": "1"}))
        assert calls == []
    finally:
        a.close()


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_catalog_rejects_every_external_reference_type(keyword):
    document = {
        "paths": {},
        "components": {"schemas": {"unsafe": {keyword: "https://remote.example/schema"}}},
    }
    with pytest.raises(ToolError, match="External OpenAPI references"):
        Catalog(document, config())


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_validator_never_fetches_remote_schemas_even_without_catalog_guard(monkeypatch, keyword):
    calls = []

    def forbidden_fetch(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Validator attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_fetch)
    a = api(lambda r: httpx.Response(200, json={}))
    try:
        with pytest.raises(ToolError, match="Unsupported upstream schema"):
            a.validate(
                a._catalog,
                {keyword: "http://169.254.169.254/latest/meta-data/schema"},
                1,
                "query",
            )
        assert calls == []
    finally:
        a.close()


async def test_logout_cookie_deletion_persists_for_later_session_requests():
    seen = []

    def h(r):
        seen.append(r.headers.get("cookie"))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={},
                headers={"set-cookie": "session=DUMMY; Path=/; HttpOnly; Secure"},
            )
        if len(seen) == 2:
            return httpx.Response(
                200,
                json={},
                headers={"set-cookie": "session=; Path=/; Max-Age=0; HttpOnly; Secure"},
            )
        return httpx.Response(200, json={})

    a = api(h, method="post")
    try:
        login = await a.call(
            "test",
            APIRequest(path={"id": "login"}, auth_mode="basic", auth_session="new"),
            write=True,
        )
        session_id = login["auth_session"]
        for action in ("logout", "after-logout"):
            await a.call(
                "test",
                APIRequest(path={"id": action}, auth_mode="session", auth_session=session_id),
                write=True,
            )
        assert seen == [None, "session=DUMMY", None]
    finally:
        a.close()


async def test_user_avatar_multipart_uses_urlencoded_userform_schema():
    calls = []

    def h(r):
        calls.append(r)
        assert r.url.path == "/api/users/1"
        assert r.headers["content-type"].startswith("multipart/form-data;")
        assert b'name="avatar"; filename="avatar.png"' in r.content
        assert b"IMAGE" in r.content
        return httpx.Response(200, json={"id": 1})

    a = api(
        h,
        method="put",
        path="/api/users/{id}",
        requestBody={
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {"$ref": "#/components/schemas/UserForm"}
                }
            }
        },
    )
    a._catalog.document["components"] = {
        "schemas": {
            "UserForm": {
                "type": "object",
                "properties": {
                    "avatar": {"type": "string", "contentMediaType": "application/octet-stream"},
                    "enabled": {"type": "boolean"},
                },
            }
        }
    }
    try:
        handle = a.artifacts.create()["artifact_id"]
        a.artifacts.append_bytes(handle, b"IMAGE")
        upload = Upload(field="avatar", artifact_id=handle, filename="avatar.png")
        result = await a.call("test", APIRequest(path={"id": "1"}, files=[upload]), write=True)
        assert result["verification"] == "not_performed"
        with pytest.raises(ToolError, match="Invalid form body"):
            await a.call(
                "test",
                APIRequest(path={"id": "1"}, files=[upload], form={"enabled": "invalid"}),
                write=True,
            )
        assert len(calls) == 1
    finally:
        a.close()


@pytest.mark.parametrize("mime", ["multipart/form-data", "application/x-www-form-urlencoded"])
async def test_nullable_form_fields_never_become_literal_none(mime):
    calls = []
    a = api(
        lambda r: calls.append(r),
        method="put",
        requestBody={
            "content": {
                mime: {
                    "schema": {
                        "type": "object",
                        "properties": {"name": {"type": ["string", "null"]}},
                    }
                }
            }
        },
    )
    try:
        with pytest.raises(ToolError, match="null"):
            await a.call("test", APIRequest(path={"id": "1"}, form={"name": None}), write=True)
        assert calls == []
    finally:
        a.close()


async def test_concurrent_auth_sessions_enforce_cap_and_serialize_cookie_updates():
    import asyncio

    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={})

    a = api(handler)
    try:
        results = await asyncio.gather(
            *(
                a.call("test", APIRequest(path={"id": "login"}, auth_session="new"))
                for _ in range(12)
            ),
            return_exceptions=True,
        )
        assert len(a.sessions) == 8
        assert sum(isinstance(r, ToolError) for r in results) == 4
        assert peak == 1
    finally:
        a.close()


def test_paginated_truncation_paths_select_the_original_item():
    rows = [{"text": str(i) + "x" * 2100} for i in range(30)]
    page = bounded(rows, offset=20, limit=1)
    assert page["truncated_paths"] == ["/20/text"]
    detail = bounded(rows, pointer=page["truncated_paths"][0])
    assert detail["data"] == rows[20]["text"]


@pytest.mark.parametrize("method", ["get", "head"])
async def test_hidden_folder_download_query_is_supported(method):
    def handler(request):
        assert request.url.params["hidden_folder"] == "true"
        return httpx.Response(204)

    a = FullAPI(config(), transport=httpx.MockTransport(handler))
    a._catalog = Catalog(
        {"paths": {"/api/roms/{id}/content/{file_name}": {method: {"operationId": "content"}}}},
        a.settings,
    )
    try:
        result = await a.call(
            "content",
            APIRequest(path={"id": 1, "file_name": "game.zip"}, query={"hidden_folder": True}),
        )
        assert result["status_code"] == 204
    finally:
        a.close()


async def test_concurrent_logout_does_not_restore_stale_cookie():
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def handler(request):
        action = request.url.path.rsplit("/", 1)[-1]
        observed.append((action, request.headers.get("cookie", "")))
        if action == "login":
            return httpx.Response(200, json={}, headers={"set-cookie": "session=test; Path=/"})
        if action == "slow":
            entered.set()
            await release.wait()
        if action == "logout":
            return httpx.Response(
                200, json={}, headers={"set-cookie": "session=; Path=/; Max-Age=0"}
            )
        return httpx.Response(200, json={})

    a = api(handler)
    try:
        login = await a.call("test", APIRequest(path={"id": "login"}, auth_session="new"))
        handle = login["auth_session"]

        def request(action):
            return APIRequest(path={"id": action}, auth_mode="session", auth_session=handle)

        slow = asyncio.create_task(a.call("test", request("slow")))
        await entered.wait()
        logout = asyncio.create_task(a.call("test", request("logout")))
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(slow, logout)
        await a.call("test", request("after"))
        assert observed[-1] == ("after", "")
    finally:
        release.set()
        a.close()


async def test_easyrpg_nested_assets_encode_each_segment():
    seen = []
    a = api(
        lambda request: seen.append(request) or httpx.Response(200, json={}),
        path="/api/roms/{id}/easyrpg/{path}",
        parameters=[{"name": "path", "in": "path", "schema": {"type": "string"}}],
    )
    try:
        await a.call("test", APIRequest(path={"id": 1, "path": "CharSet/Hero name.png"}))
        assert seen[0].url.raw_path == b"/api/roms/1/easyrpg/CharSet/Hero%20name.png"
        with pytest.raises(ToolError):
            await a.call("test", APIRequest(path={"id": 1, "path": "CharSet/%252e%252e/secrets"}))
        assert len(seen) == 1
    finally:
        a.close()
