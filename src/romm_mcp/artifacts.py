"""Bounded, short-lived file transfers addressed only by opaque artifact IDs.

Methods are synchronous and must run serially on the server's event-loop thread.
Upload handles belong to callers and must be closed promptly. Failed streamed
downloads must be deleted by the executor, including cancellation failures.
Open uploads pin storage quota and prevent mutation; expiry hides their artifacts
until the final lease closes. Failed writes retain reserved quota until cleanup.
"""

import base64
import binascii
import io
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from mcp.server.mcpserver.exceptions import ToolError


class _ReadLease(io.BufferedReader):
    def __init__(self, handle: BinaryIO, release: Callable[["_ReadLease"], None]):
        self._release = release
        self._released = False
        super().__init__(handle)

    def close(self) -> None:
        if self._released:
            return
        super().close()
        self._released = True
        self._release(self)

    def detach(self):
        raise io.UnsupportedOperation("Artifact upload handles cannot be detached.")


@dataclass
class _Artifact:
    path: Path
    created: float
    size: int = 0
    failed: bool = False
    leases: set[_ReadLease] = field(default_factory=set)


class ArtifactStore:
    MAX_ARTIFACTS = 16
    MAX_CHUNK_BYTES = 32768

    def __init__(
        self, directory: str | None = None, max_bytes: int = 67108864, ttl_seconds: int = 3600
    ):
        if max_bytes <= 0 or ttl_seconds <= 0:
            raise ToolError("Artifact capacity and lifetime must be positive.")
        try:
            self._directory = tempfile.TemporaryDirectory(prefix="romm-artifacts-", dir=directory)
            os.chmod(self._directory.name, 0o700)
        except OSError:
            raise ToolError("Artifact storage could not be initialized.") from None
        self._artifacts: dict[str, _Artifact] = {}
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._total_bytes = 0
        self._closed = False

    def _discard(self, artifact_id: str) -> None:
        artifact = self._artifacts[artifact_id]
        if artifact.leases:
            raise ToolError("Artifact is in use by an active upload.")
        try:
            artifact.path.unlink(missing_ok=True)
        except OSError:
            raise ToolError("Artifact cleanup failed.") from None
        self._total_bytes -= artifact.size
        del self._artifacts[artifact_id]

    def _purge(self) -> None:
        if self._closed:
            raise ToolError("Artifact store is closed.")
        now = time.monotonic()
        for key, artifact in list(self._artifacts.items()):
            if now - artifact.created >= self._ttl_seconds and not artifact.leases:
                self._discard(key)

    def _get(self, artifact_id: str, *, allow_failed: bool = False) -> _Artifact:
        self._purge()
        if not isinstance(artifact_id, str) or artifact_id not in self._artifacts:
            raise ToolError("Unknown or expired artifact.")
        artifact = self._artifacts[artifact_id]
        if time.monotonic() - artifact.created >= self._ttl_seconds:
            raise ToolError("Unknown or expired artifact.")
        if artifact.failed and not allow_failed:
            raise ToolError("Artifact is unavailable after a failed write; delete it to retry.")
        return artifact

    @staticmethod
    def _open(artifact: _Artifact, write: bool = False) -> BinaryIO:
        try:
            fd = os.open(artifact.path, (os.O_WRONLY if write else os.O_RDONLY) | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("Not a regular file")
                return os.fdopen(fd, "wb" if write else "rb")
            except BaseException:
                os.close(fd)
                raise
        except OSError:
            raise ToolError("Artifact storage could not be accessed.") from None

    def create(self) -> dict:
        self._purge()
        if len(self._artifacts) >= self.MAX_ARTIFACTS:
            raise ToolError("Artifact limit of 16 reached; delete an artifact or wait for expiry.")
        artifact_id = secrets.token_hex(16)
        path = Path(self._directory.name) / artifact_id
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except OSError:
            raise ToolError("Artifact could not be created.") from None
        self._artifacts[artifact_id] = _Artifact(path, time.monotonic())
        return {"artifact_id": artifact_id, "size_bytes": 0}

    def info(self, artifact_id: str) -> dict:
        artifact = self._get(artifact_id)
        return {"artifact_id": artifact_id, "size_bytes": artifact.size}

    def append(self, artifact_id: str, data_base64: str, offset: int) -> dict:
        artifact = self._get(artifact_id)
        if type(offset) is not int or offset != artifact.size:
            raise ToolError("Append offset must equal the current artifact size; refresh info.")
        if not isinstance(data_base64, str):
            raise ToolError("Chunk must be strict base64 text.")
        if len(data_base64) > ((self.MAX_CHUNK_BYTES + 2) // 3) * 4:
            raise ToolError("Artifact chunks must contain at most 32768 decoded bytes.")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("Chunk must be strict base64 text.") from None
        if len(data) > self.MAX_CHUNK_BYTES:
            raise ToolError("Artifact chunks must contain at most 32768 decoded bytes.")
        return self.append_bytes(artifact_id, data)

    def append_bytes(self, artifact_id: str, data: bytes) -> dict:
        """Append a streamed download chunk, enforcing the total storage limit."""
        artifact = self._get(artifact_id)
        if artifact.leases:
            raise ToolError("Artifact is in use by an active upload.")
        if self._total_bytes + len(data) > self._max_bytes:
            raise ToolError("Artifact storage capacity exceeded; delete artifacts or reduce size.")
        try:
            with self._open(artifact, write=True) as handle:
                offset = artifact.size
                # Reserve before touching disk: failed cleanup must retain this quota.
                artifact.size += len(data)
                self._total_bytes += len(data)
                handle.seek(offset)
                if handle.write(data) != len(data):
                    raise OSError("Incomplete artifact write")
        except OSError:
            artifact.failed = True
            self._discard(artifact_id)
            raise ToolError("Artifact write failed; the partial artifact was removed.") from None
        return {"artifact_id": artifact_id, "size_bytes": artifact.size}

    def read(self, artifact_id: str, offset: int = 0, limit: int = 4096) -> dict:
        artifact = self._get(artifact_id)
        if type(offset) is not int or not 0 <= offset <= artifact.size:
            raise ToolError("Read offset must be within the artifact.")
        if type(limit) is not int or not 1 <= limit <= self.MAX_CHUNK_BYTES:
            raise ToolError("Read limit must be between 1 and 32768 bytes.")
        try:
            with self._open(artifact) as handle:
                handle.seek(offset)
                data = handle.read(limit)
        except OSError:
            raise ToolError("Artifact read failed.") from None
        end = offset + len(data)
        return {
            "artifact_id": artifact_id,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "offset": offset,
            "total_bytes": artifact.size,
            "next_offset": end if end < artifact.size else None,
        }

    def delete(self, artifact_id: str) -> dict:
        self._get(artifact_id, allow_failed=True)
        self._discard(artifact_id)
        return {"artifact_id": artifact_id, "deleted": True}

    def open_file(self, artifact_id: str) -> BinaryIO:
        """Pin immutable bytes and their quota for an upload until the caller closes."""
        artifact = self._get(artifact_id)

        def release(lease: _ReadLease) -> None:
            artifact.leases.remove(lease)
            if not artifact.leases and time.monotonic() - artifact.created >= self._ttl_seconds:
                self._discard(artifact_id)

        lease = _ReadLease(self._open(artifact), release)
        artifact.leases.add(lease)
        return lease

    def close(self) -> None:
        if self._closed:
            return
        for artifact in list(self._artifacts.values()):
            for lease in list(artifact.leases):
                lease.close()
        try:
            self._directory.cleanup()
        except OSError:
            raise ToolError("Artifact storage cleanup failed.") from None
        self._artifacts.clear()
        self._total_bytes = 0
        self._closed = True
