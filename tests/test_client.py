import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from romm_mcp.client import RomMClient
from romm_mcp.models import ProgressChanges


def make_client(handler):
    return RomMClient(
        "https://romm.example", "upstream-secret", transport=httpx.MockTransport(handler)
    )


async def test_search_filters_indexes_and_projects_page():
    def handler(request):
        assert request.headers["authorization"] == "Bearer upstream-secret"
        for key in ("with_char_index", "with_filter_values", "with_rom_id_index"):
            assert request.url.params[key] == "false"
        assert request.url.params.get_list("platform_ids") == ["2", "3"]
        assert request.url.params["search_term"] == "Zelda"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 1,
                        "name": "Zelda",
                        "platform_id": 2,
                        "platform_display_name": "NES",
                        "igdb_metadata": {"huge": "x" * 10000},
                        "summary": "x" * 10000,
                    }
                ],
                "total": 30,
            },
        )

    async with make_client(handler) as c:
        result = await c.search_roms(query="Zelda", platform_ids=[2, 3], limit=1)
    assert result.next_offset == 1
    assert result.total == 30
    assert len(result.model_dump_json()) < 1000
    assert "huge" not in result.model_dump_json()


async def test_empty_page_does_not_offer_infinite_pagination():
    async with make_client(lambda r: httpx.Response(200, json={"items": [], "total": 30})) as c:
        assert (await c.search_roms(offset=30)).next_offset is None


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_upstream_errors_are_sanitized(status):
    async with make_client(
        lambda r: httpx.Response(status, text="upstream-secret private body")
    ) as c:
        with pytest.raises(ToolError) as e:
            await c.search_roms()
    assert "upstream-secret" not in str(e.value)
    assert str(status) in str(e.value)


async def test_html_response_is_not_empty_success():
    async with make_client(lambda r: httpx.Response(200, text="<html>Login</html>")) as c:
        with pytest.raises(ToolError, match="JSON"):
            await c.search_roms()


async def test_membership_is_atomic_and_read_back():
    calls = []

    def handler(r):
        calls.append(r.method)
        if r.method == "POST":
            assert json.loads(r.content) == {"rom_ids": [1, 2]}
            return httpx.Response(200, json={})
        return httpx.Response(
            200, json={"id": 7, "name": "Test", "rom_ids": [1, 2, 99], "rom_count": 3}
        )

    async with make_client(handler) as c:
        result = await c.update_collection_roms(7, [1, 2], "add")
    assert calls == ["GET", "POST", "GET"]
    assert result.verified is True
    assert result.collection.rom_count == 3


async def test_failed_readback_is_not_reported_as_success():
    def handler(r):
        return httpx.Response(200, json={"id": 7, "name": "Test", "rom_ids": [], "rom_count": 0})

    async with make_client(handler) as c:
        with pytest.raises(ToolError, match="verification"):
            await c.update_collection_roms(7, [1], "add")


async def test_progress_sends_only_changed_fields_and_reads_back():
    bodies = []

    def handler(r):
        if r.method == "PUT":
            bodies.append(json.loads(r.content))
        return httpx.Response(
            200,
            json={
                "id": 1,
                "name": "Test",
                "rom_user": {"rating": 0, "backlogged": False, "completion": 80},
            },
        )

    async with make_client(handler) as c:
        result = await c.update_rom_progress(1, ProgressChanges(rating=0, backlogged=False))
    assert bodies == [{"backlogged": False, "rating": 0}]
    assert result.verified
    assert result.progress.completion == 80


async def test_write_timeout_never_retries():
    calls = []

    def handler(r):
        calls.append(r.method)
        if r.method == "POST":
            raise httpx.ReadTimeout("upstream-secret")
        return httpx.Response(200, json={"id": 7, "name": "Test", "rom_ids": [], "rom_count": 0})

    async with make_client(handler) as c:
        with pytest.raises(ToolError, match="uncertain"):
            await c.update_collection_roms(7, [1], "add")
    assert calls.count("POST") == 1


def test_progress_rejects_unknown_fields_bad_ratings_and_empty():
    for value in ({"admin": True}, {"rating": 11}, {"status": "playing"}, {}):
        with pytest.raises(ValueError):
            ProgressChanges(**value)


async def test_successful_write_then_failed_readback_is_uncertain():
    def handler(r):
        return httpx.Response(200, json={}) if r.method == "PUT" else httpx.Response(503)

    async with make_client(handler) as c:
        with pytest.raises(ToolError, match="may already have applied"):
            await c.update_rom_progress(1, ProgressChanges(rating=2))


async def test_successful_write_body_is_not_needed_for_verification():
    def handler(r):
        if r.method == "PUT":
            return httpx.Response(200, text="")
        return httpx.Response(200, json={"rom_user": {"rating": 2}})

    async with make_client(handler) as c:
        assert (await c.update_rom_progress(1, ProgressChanges(rating=2))).verified


async def test_create_allows_other_owner_same_name_and_verifies():
    calls = []

    def handler(r):
        calls.append((r.method, r.url.path))
        if r.url.path == "/api/users/me":
            return httpx.Response(200, json={"id": 1})
        if r.method == "POST":
            return httpx.Response(200, json={"id": 8})
        if r.url.path == "/api/collections":
            return httpx.Response(200, json=[{"id": 7, "user_id": 2, "name": "Picks"}])
        return httpx.Response(
            200,
            json={"id": 8, "user_id": 1, "name": "Picks", "description": "", "is_public": False},
        )

    async with make_client(handler) as c:
        assert (await c.create_collection("Picks")).verified
    assert ("GET", "/api/collections/8") in calls
