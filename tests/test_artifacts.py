import base64
import stat

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from romm_mcp.artifacts import ArtifactStore


@pytest.fixture
def store(tmp_path):
    instance = ArtifactStore(str(tmp_path))
    yield instance
    instance.close()


def test_roundtrip_chunks_offsets_and_upload_file(store):
    artifact = store.create()
    key = artifact["artifact_id"]
    assert artifact["size_bytes"] == 0
    assert store.append(key, "YWJj", offset=0)["size_bytes"] == 3
    assert store.append(key, "ZGVm", offset=3)["size_bytes"] == 6
    assert store.read(key, limit=4) == {
        "artifact_id": key,
        "data_base64": "YWJjZA==",
        "offset": 0,
        "total_bytes": 6,
        "next_offset": 4,
    }
    assert store.read(key, offset=4)["data_base64"] == "ZWY="
    assert store.read(key, offset=4)["next_offset"] is None
    with store.open_file(key) as upload:
        assert upload.read() == b"abcdef"
    assert store.info(key) == {"artifact_id": key, "size_bytes": 6}


@pytest.mark.parametrize("key", ["../secret", "/etc/passwd", "", "a" * 32])
def test_unrecognized_handles_never_resolve_paths(store, key):
    for operation in (store.info, store.read, store.open_file, store.delete):
        with pytest.raises(ToolError, match="Unknown or expired artifact"):
            operation(key)


@pytest.mark.parametrize("data", ["invalid!!!", "YQ", "Y Q==", "é", "YQ==extra"])
def test_malformed_base64_preserves_contents(store, data):
    key = store.create()["artifact_id"]
    with pytest.raises(ToolError, match="base64"):
        store.append(key, data, offset=0)
    assert store.info(key)["size_bytes"] == 0


def test_stale_offset_rejects_retry_without_duplicating_data(store):
    key = store.create()["artifact_id"]
    store.append(key, "YQ==", offset=0)
    with pytest.raises(ToolError, match="offset"):
        store.append(key, "YQ==", offset=0)
    assert store.read(key)["data_base64"] == "YQ=="


def test_chunk_and_total_capacity_are_enforced(tmp_path):
    store = ArtifactStore(str(tmp_path), max_bytes=40000)
    try:
        first = store.create()["artifact_id"]
        second = store.create()["artifact_id"]
        with pytest.raises(ToolError, match="32768"):
            store.append(first, base64.b64encode(b"a" * 32769).decode(), offset=0)
        store.append_bytes(first, b"a" * 39000)
        with pytest.raises(ToolError, match="capacity"):
            store.append_bytes(second, b"b" * 1001)
        assert store.info(second)["size_bytes"] == 0
        store.append_bytes(second, b"b" * 1000)
        store.delete(first)
        store.append_bytes(second, b"b" * 1000)
        assert store.info(second)["size_bytes"] == 2000
    finally:
        store.close()


def test_artifact_count_bounded_and_delete_reclaims_slot(store):
    keys = [store.create()["artifact_id"] for _ in range(16)]
    with pytest.raises(ToolError, match="16"):
        store.create()
    assert store.delete(keys[0]) == {"artifact_id": keys[0], "deleted": True}
    store.create()


def test_expired_files_removed_and_capacity_reclaimed(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("romm_mcp.artifacts.time.monotonic", lambda: now[0])
    store = ArtifactStore(str(tmp_path), max_bytes=3, ttl_seconds=2)
    try:
        key = store.create()["artifact_id"]
        store.append_bytes(key, b"abc")
        now[0] = 102.0
        with pytest.raises(ToolError, match="Unknown or expired artifact"):
            store.info(key)
        assert not list(tmp_path.glob("*/*"))
        store.append_bytes(store.create()["artifact_id"], b"def")
    finally:
        store.close()


def test_partial_download_can_be_deleted_and_store_closes_cleanly(tmp_path):
    store = ArtifactStore(str(tmp_path), max_bytes=3)
    key = store.create()["artifact_id"]
    store.append_bytes(key, b"abc")
    with pytest.raises(ToolError):
        store.append_bytes(key, b"d")
    store.delete(key)
    store.append_bytes(store.create()["artifact_id"], b"xyz")
    store.close()
    store.close()
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ToolError, match="closed"):
        store.create()


def test_private_permissions_and_symlink_rejection(tmp_path):
    store = ArtifactStore(str(tmp_path))
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    try:
        key = store.create()["artifact_id"]
        folder = next(item for item in tmp_path.iterdir() if item.is_dir())
        path = next(folder.iterdir())
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.unlink()
        path.symlink_to(secret)
        for operation in (store.read, store.open_file):
            with pytest.raises(ToolError) as error:
                operation(key)
            assert str(tmp_path) not in str(error.value)
        with pytest.raises(ToolError):
            store.append_bytes(key, b"overwrite")
        assert secret.read_bytes() == b"private"
    finally:
        store.close()


@pytest.mark.parametrize("offset,limit", [(-1, 1), (1, 1), (0, 0), (0, 32769)])
def test_read_rejects_invalid_ranges(store, offset, limit):
    key = store.create()["artifact_id"]
    with pytest.raises(ToolError):
        store.read(key, offset=offset, limit=limit)


def test_storage_initialization_errors_hide_paths(tmp_path):
    with pytest.raises(ToolError) as error:
        ArtifactStore(str(tmp_path / "missing"))
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("expiry", [False, True])
def test_open_upload_retains_capacity_until_last_lease_closes(tmp_path, monkeypatch, expiry):
    now = [100.0]
    monkeypatch.setattr("romm_mcp.artifacts.time.monotonic", lambda: now[0])
    store = ArtifactStore(str(tmp_path), max_bytes=4, ttl_seconds=2)
    handles = []
    try:
        key = store.create()["artifact_id"]
        store.append_bytes(key, b"data")
        handles.extend([store.open_file(key), store.open_file(key)])
        if expiry:
            now[0] = 102.0
            with pytest.raises(ToolError, match="expired"):
                store.info(key)
        else:
            with pytest.raises(ToolError, match="in use"):
                store.delete(key)
            with pytest.raises(ToolError, match="in use"):
                store.append_bytes(key, b"x")
        second = store.create()["artifact_id"]
        with pytest.raises(ToolError, match="capacity"):
            store.append_bytes(second, b"data")
        handles[0].close()
        with pytest.raises(ToolError, match="capacity"):
            store.append_bytes(second, b"data")
        assert handles[1].read() == b"data"
        handles[1].close()
        if not expiry:
            store.delete(key)
        store.append_bytes(second, b"data")
    finally:
        for handle in handles:
            handle.close()
        store.close()


def test_expired_uploads_still_count_towards_artifact_limit(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("romm_mcp.artifacts.time.monotonic", lambda: now[0])
    store = ArtifactStore(str(tmp_path), ttl_seconds=2)
    handles = []
    try:
        for _ in range(16):
            handles.append(store.open_file(store.create()["artifact_id"]))
        now[0] = 102.0
        with pytest.raises(ToolError, match="16"):
            store.create()
        handles.pop().close()
        store.create()
    finally:
        for handle in handles:
            handle.close()
        store.close()


def test_store_close_closes_outstanding_uploads_and_removes_storage(tmp_path):
    store = ArtifactStore(str(tmp_path))
    handle = store.open_file(store.create()["artifact_id"])
    store.close()
    assert handle.closed
    assert list(tmp_path.iterdir()) == []
    handle.close()


def test_partial_write_cleanup_failure_keeps_reserved_quota_and_unavailable_file(tmp_path):
    from pathlib import Path
    from unittest.mock import patch

    store = ArtifactStore(str(tmp_path), max_bytes=4096)
    try:
        first = store.create()["artifact_id"]
        actual_open = store._open

        class FailAfterPartialWrite:
            def __enter__(self):
                self.handle = actual_open(store._artifacts[first], write=True)
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def seek(self, offset):
                self.handle.seek(offset)

            def write(self, data):
                self.handle.write(data[:2048])
                self.handle.flush()
                raise OSError("simulated partial write")

        with (
            patch.object(store, "_open", return_value=FailAfterPartialWrite()),
            patch.object(Path, "unlink", side_effect=PermissionError("simulated cleanup failure")),
        ):
            with pytest.raises(ToolError, match="cleanup failed"):
                store.append_bytes(first, b"x" * 4096)
        for operation in (store.info, store.open_file, store.read):
            with pytest.raises(ToolError, match="unavailable"):
                operation(first)
        with pytest.raises(ToolError, match="unavailable"):
            store.append_bytes(first, b"x")
        second = store.create()["artifact_id"]
        with pytest.raises(ToolError, match="capacity"):
            store.append_bytes(second, b"y" * 4096)
        store.delete(first)
        store.append_bytes(second, b"y" * 4096)
    finally:
        store.close()
