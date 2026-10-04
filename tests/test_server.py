import pytest
from starlette.testclient import TestClient

from romm_mcp.server import Settings, create_http_app, create_server


def settings(**kwargs):
    return Settings(
        romm_url="https://romm.example",
        romm_token="upstream-secret",
        auth_token="a" * 48,
        allowed_hosts=["testserver"],
        **kwargs,
    )


def test_http_fails_closed_without_auth():
    with pytest.raises(ValueError, match="MCP_AUTH_TOKEN"):
        create_http_app(Settings(romm_url="https://romm.example", romm_token="x"))


def test_http_health_auth_and_origin():
    app = create_http_app(settings())
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.post("/mcp", json={}).status_code == 401
        auth = {"Authorization": "Bearer " + "a" * 48}
        assert c.post("/mcp", json={}, headers={**auth, "Host": "evil.example"}).status_code == 421
        assert (
            c.post("/mcp", json={}, headers={**auth, "Origin": "https://evil.example"}).status_code
            == 403
        )
        assert (
            c.post("/mcp", json={}, headers={"Authorization": "Bearer upstream-secret"}).status_code
            == 401
        )


async def test_registration_readonly_and_typed_bounded_schema():
    mcp = create_server(settings())
    tools = await mcp.list_tools()
    assert len(tools) == 10
    search = next(t for t in tools if t.name == "search_roms")
    assert search.input_schema["properties"]["limit"]["maximum"] == 100
    assert search.output_schema
    assert search.annotations.read_only_hint
    rw = await create_server(settings(allow_writes=True)).list_tools()
    assert len(rw) == 14
    assert not next(t for t in rw if t.name == "update_collection_roms").annotations.read_only_hint
