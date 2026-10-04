"""Actual stdio and TCP HTTP clients against a deterministic local upstream."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import httpx2
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture
def upstream():
    class Handler(BaseHTTPRequestHandler):
        members = {42, 43}

        def do_POST(self):
            assert self.path == "/api/test"
            self.send_response(204)
            self.end_headers()

        def do_DELETE(self):
            assert self.path == "/api/collections/501/roms"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.members.difference_update(body["rom_ids"])
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer upstream-secret"
            data = {"items": [{"id": 42, "name": "Test ROM", "platform_id": 2}], "total": 1}
            if self.path == "/openapi.json":
                data = {
                    "paths": {
                        "/api/roms": {"get": {"operationId": "roms"}},
                        "/api/test": {"post": {"operationId": "noop"}},
                    }
                }
            elif self.path == "/api/collections/501":
                data = {"id": 501, "name": "Test", "rom_ids": sorted(self.members)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join()
    server.server_close()


async def verify(session):
    await session.initialize()
    tools = await session.list_tools()
    assert len(tools.tools) == 14
    result = await session.call_tool("search_roms", {"query": "Test", "limit": 1})
    assert not result.is_error
    assert result.structured_content["items"][0]["id"] == 42
    assert result.structured_content["next_offset"] is None
    for name, args in [
        ("romm_api_list", {}),
        ("romm_api_describe", {"operation_id": "roms"}),
        ("romm_api_read", {"operation_id": "roms"}),
        ("romm_api_write", {"operation_id": "noop"}),
        ("romm_socket", {"action": "catalog"}),
    ]:
        generic = await session.call_tool(name, args)
        assert not generic.is_error
        assert generic.structured_content is not None, name
    removed = await session.call_tool(
        "update_collection_roms", {"collection_id": 501, "rom_ids": [42], "operation": "remove"}
    )
    assert not removed.is_error
    assert removed.structured_content["verified"]
    assert removed.structured_content["collection"]["rom_count"] == 1
    invalid = await session.call_tool("search_roms", {"limit": 101})
    assert invalid.is_error


async def test_stdio(upstream):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "romm_mcp.server"],
        env={
            **os.environ,
            "ROMM_URL": upstream,
            "ROMM_TOKEN": "upstream-secret",
            "ROMM_ALLOW_WRITES": "true",
            "MCP_TRANSPORT": "stdio",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await verify(session)


async def test_streamable_http(upstream):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "ROMM_URL": upstream,
        "ROMM_TOKEN": "upstream-secret",
        "MCP_AUTH_TOKEN": "a" * 48,
        "ROMM_ALLOW_WRITES": "true",
        "MCP_ALLOWED_HOSTS": f"127.0.0.1:{port}",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "romm_mcp.server", "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(process.stderr.read().decode())
                try:
                    if (await probe.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("HTTP server did not start")
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + "a" * 48}) as http:
            async with streamable_http_client(
                f"http://127.0.0.1:{port}/mcp", http_client=http
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await verify(session)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()
