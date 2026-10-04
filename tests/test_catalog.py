import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from romm_mcp.catalog import Catalog

FLAGS = ["writes", "destructive", "admin", "auth", "files", "tasks", "realtime"]


def settings(enabled=False):
    return SimpleNamespace(**{"allow_" + k: enabled for k in FLAGS})


def inventory():
    rows = json.loads((Path(__file__).parent / "fixtures/romm-5.3.1-operations.json").read_text())
    paths = {}
    for row in rows:
        row = dict(row)
        p = row.pop("path")
        m = row.pop("method")
        paths.setdefault(p, {})[m] = row
    return {"info": {"version": "5.3.1"}, "paths": paths, "components": {"schemas": {}}}


def test_all_246_operations_are_describable_and_enabled_by_switches():
    c = Catalog(inventory(), settings(True))
    assert len(c.operations) == 246
    for key, op in c.operations.items():
        assert c.describe(key)["operation"]["operation_id"] == key
        assert c.check(op) == c.requirements(op)


@pytest.mark.parametrize(
    "method,path,gate",
    [
        ("post", "/api/roms/delete", "destructive"),
        ("delete", "/api/collections/{id}", "destructive"),
        ("put", "/api/users/{id}", "admin"),
        ("get", "/api/login/openid", "writes"),
        ("get", "/api/oauth/openid", "auth"),
        ("get", "/api/saves/{id}/content", "files"),
        ("post", "/api/tasks/run/{task_name}", "tasks"),
        ("get", "/api/logs", "admin"),
        ("post", "/api/roms/{id}/manuals", "destructive"),
    ],
)
def test_sensitive_categories_fail_closed(method, path, gate):
    c = Catalog(inventory(), settings())
    op = next(x for x in c.operations.values() if x.method == method and x.path == path)
    assert gate in c.requirements(op)
    with pytest.raises(ToolError):
        c.check(op)


def test_readtool_rejects_side_effect_get_even_when_write_enabled():
    c = Catalog(inventory(), settings(True))
    op = next(x for x in c.operations.values() if x.path == "/api/saves/{id}/content")
    assert c.is_write(op, {"session_id": "test"})
    assert c.is_write(op, {"device_id": "test", "optimistic": True})
    assert not c.is_write(op, {})


def test_catalog_rejects_external_ref_and_unsafe_path():
    for schema in (
        {"paths": {"https://evil.example": {"get": {"operationId": "bad"}}}},
        {"paths": {}, "components": {"schemas": {"bad": {"$ref": "https://evil.example"}}}},
    ):
        with pytest.raises(ToolError):
            Catalog(schema, settings())


@pytest.mark.parametrize(
    "path",
    [
        "/api/%2e%2e/private",
        "/api/%252e%252e/private",
        "/api/%2E%2E%2Fprivate",
        "/api/test%5c..%5cprivate",
        "/api/test%3fadmin=true",
        "/api/test%00",
        "/api/test\n",
    ],
)
def test_catalog_rejects_encoded_and_control_literal_paths(path):
    with pytest.raises(ToolError, match="unsafe API path"):
        Catalog({"paths": {path: {"get": {"operationId": "unsafe"}}}}, settings())


@pytest.mark.parametrize(
    "method,path,gates",
    [
        ("post", "/api/tasks/run/{task_name}", {"destructive"}),
        ("put", "/api/roms/{id}", {"destructive", "files"}),
        ("post", "/api/roms/upload/{upload_id}/complete", {"destructive"}),
        ("post", "/api/saves", {"destructive", "files"}),
        ("post", "/api/states", {"destructive", "files"}),
        ("post", "/api/screenshots", {"destructive", "files"}),
        ("post", "/api/firmware", {"destructive", "files"}),
        ("put", "/api/saves/{id}", {"destructive", "files"}),
        ("put", "/api/states/{id}", {"destructive", "files"}),
        ("post", "/api/roms/{id}/manuals/redownload", {"destructive", "files"}),
        ("post", "/api/roms/{id}/walkthroughs/gamefaqs", {"files"}),
        ("get", "/api/permissions/catalog", {"admin"}),
        ("get", "/api/streaming/sessions", {"admin"}),
        ("delete", "/api/streaming/sessions", {"admin"}),
        ("get", "/api/memory-cards/{id}/versions", {"writes"}),
        ("get", "/api/tasks/status", {"writes", "tasks"}),
        ("get", "/api/tasks", {"tasks"}),
        ("post", "/api/memory-cards/{id}/versions", {"writes", "files"}),
    ],
)
def test_adversarial_source_semantics_require_all_gates(method, path, gates):
    c = Catalog(inventory(), settings())
    op = next(x for x in c.operations.values() if x.method == method and x.path == path)
    assert gates <= set(c.requirements(op))
