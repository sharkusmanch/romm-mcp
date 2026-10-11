from types import SimpleNamespace

import pytest

from romm_mcp.catalog import Catalog


def operation(method, path):
    settings = SimpleNamespace()
    catalog = Catalog({"paths": {path: {method: {"operationId": "target"}}}}, settings)
    return catalog, catalog.get("target")


@pytest.mark.parametrize(
    "method,path,gates",
    [
        ("get", "/api/notification-channels/apprise-services", {"admin"}),
        ("post", "/api/notification-channels/apprise-services/parse", {"admin", "writes"}),
        ("post", "/api/devices/{device_id}/installs", {"tasks", "files", "writes"}),
        ("post", "/api/devices/{device_id}/installs/claim", {"tasks", "files", "writes"}),
        ("put", "/api/devices/{device_id}/installs/{request_id}", {"tasks", "writes"}),
        ("post", "/api/tasks/scan", {"tasks", "files", "writes", "destructive"}),
        ("put", "/api/saves/{id}/file-name", {"files", "writes"}),
        ("put", "/api/states/{id}/file-name", {"files", "writes"}),
    ],
)
def test_new_routes_require_side_effect_gates(method, path, gates):
    catalog, op = operation(method, path)
    assert gates <= set(catalog.requirements(op))


def test_converted_get_is_a_background_write_but_head_is_read_only():
    path = "/api/roms/{id}/content/{file_name}"
    catalog, op = operation("get", path)
    assert not catalog.is_write(op)
    assert catalog.is_write(op, {"format": "chd"})
    assert {"writes", "tasks", "files"} <= set(catalog.requirements(op, {"format": "chd"}))
    catalog, op = operation("head", path)
    assert not catalog.is_write(op, {"format": "chd"})


def test_device_socket_token_configuration(monkeypatch):
    from romm_mcp.server import Settings

    monkeypatch.setenv("ROMM_URL", "https://romm.example")
    monkeypatch.setenv("ROMM_TOKEN", "upstream")
    monkeypatch.setenv("ROMM_DEVICE_TOKEN", "device-secret")
    settings = Settings.from_env()
    assert settings.romm_device_token == "device-secret"
    assert "device-secret" not in repr(settings)


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
def test_notification_channel_mutations_require_admin(method):
    catalog, op = operation(method, "/api/notification-channels/{channel_id}")
    assert "admin" in catalog.requirements(op)


@pytest.mark.parametrize(
    "version,count", [("5.3.1", 0), ("5.4.0", 10), ("5.5.0", 10), ("6.0.0", 10), ("unknown", 0)]
)
def test_hidden_dav_routes_are_discovered_only_on_supported_versions(version, count):
    catalog = Catalog({"info": {"version": version}, "paths": {}}, SimpleNamespace())
    assert len(catalog.operations) == count
    if count:
        assert catalog.get("romm_webdav_propfind").method == "propfind"
        assert not catalog.is_write(catalog.get("romm_webdav_propfind"))
        assert catalog.is_write(catalog.get("romm_webdav_get"))
        assert {"files", "destructive", "writes"} <= set(
            catalog.requirements(catalog.get("romm_webdav_move"))
        )


def test_official_54_route_inventory_has_complete_discovery():
    import json
    from pathlib import Path

    rows = json.loads((Path(__file__).parent / "fixtures/romm-5.4.0-routes.json").read_text())
    paths = {}
    for row in rows:
        spec = dict(row)
        path, method = spec.pop("path"), spec.pop("method")
        paths.setdefault(path, {})[method] = spec
    enabled = SimpleNamespace(
        **{
            "allow_" + name: True
            for name in ("writes", "admin", "files", "tasks", "destructive", "auth", "realtime")
        }
    )
    catalog = Catalog({"info": {"version": "5.4.0"}, "paths": paths}, enabled)
    assert len(rows) == 280
    assert len(catalog.operations) == 290
    assert len({(op.method, op.path) for op in catalog.operations.values()}) == 290
    for operation_id, op in catalog.operations.items():
        assert catalog.describe(operation_id)["operation"]["enabled"]
        catalog.check(op)


def test_easyrpg_assets_require_file_access():
    catalog, op = operation("get", "/api/roms/{id}/easyrpg/{path}")
    assert "files" in catalog.requirements(op)
