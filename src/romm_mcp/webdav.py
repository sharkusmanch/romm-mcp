"""Explicit RomM 5.4 DAV definitions (upstream excludes these from OpenAPI)."""

from urllib.parse import quote

from mcp.server.mcpserver.exceptions import ToolError

DAV_ROOT = "/api/sync/retroarch/"
DAV_PATH = DAV_ROOT + "{file_path}"
DAV_METHODS = (
    "options",
    "lock",
    "unlock",
    "propfind",
    "get",
    "head",
    "put",
    "delete",
    "move",
    "mkcol",
)


def is_webdav(op):
    return op.path == DAV_PATH and op.spec.get("x-romm-webdav") is True


def encode_path(path):
    """Accept literal relative DAV paths, then encode each segment once."""
    if (
        not isinstance(path, str)
        or len(path) > 2048
        or path.startswith("/")
        or any(c in path for c in ("%", "\\", ":", "?", "#"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or "//" in path
        or any(part in (".", "..") for part in path.split("/"))
    ):
        raise ToolError("Use a literal relative WebDAV path without traversal or percent encoding.")
    return "/".join(quote(part, safe="") for part in path.split("/"))


def webdav_operations():
    """Return path items ready to merge into a verified RomM >=5.4 catalog."""
    descriptions = {
        "options": "Advertise supported DAV methods.",
        "lock": "Return an always-granted compatibility lock; RomM does not enforce it.",
        "unlock": "Acknowledge a compatibility unlock; no persistent lock exists.",
        "propfind": "Browse ROM/save/state paths; Depth 0 or 1. XML is returned as bounded text.",
        "get": (
            "Read manifest or file. Requires writes because RomM can touch devices "
            "and flag missing assets. ROM paths return a 307 location; redirects are "
            "never followed."
        ),
        "head": (
            "Read file metadata. Requires writes because RomM shares the GET handler "
            "and its side effects."
        ),
        "put": (
            "Upload save/state bytes using raw_artifact_id. Existing files may be "
            "overwritten. Manifest uploads are accepted and discarded."
        ),
        "delete": "Permanently delete the matching save/state/blob (potentially all aliases).",
        "move": (
            "DESTRUCTIVE: RomM implements MOVE as permanent DELETE, ignoring "
            "Destination; it does not retain or move a copy."
        ),
        "mkcol": "Acknowledge directory creation; RomM derives the directory layout.",
    }
    methods = {}
    for method in DAV_METHODS:
        parameters = [
            {
                "name": "file_path",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "maxLength": 2048},
                "description": (
                    "Literal relative path such as saves/core/Game.srm; empty string "
                    "for root. Do not percent-encode."
                ),
            }
        ]
        if method == "propfind":
            parameters.append(
                {"name": "Depth", "in": "header", "schema": {"type": "string", "enum": ["0", "1"]}}
            )
        if method == "move":
            parameters.append(
                {
                    "name": "Destination",
                    "in": "header",
                    "schema": {"type": "string", "maxLength": 2048},
                    "description": (
                        "Literal relative DAV path, expanded to fixed RomM origin. "
                        "RomM ignores it and permanently deletes the source."
                    ),
                }
            )
        if method in ("lock", "unlock"):
            parameters.extend(
                {"name": name, "in": "header", "schema": {"type": "string", "maxLength": 2048}}
                for name in ("Lock-Token", "Timeout", "If")
            )
        spec = {
            "operationId": "romm_webdav_" + method,
            "summary": method.upper() + " RetroArch WebDAV",
            "description": descriptions[method],
            "tags": ["retroarch-webdav"],
            "x-romm-webdav": True,
            "parameters": parameters,
            "security": [
                {
                    "OAuth2PasswordBearer": [
                        "assets.write"
                        if method in ("put", "delete", "move", "mkcol")
                        else "assets.read"
                    ]
                }
            ],
            "responses": {
                "2XX": {"description": "DAV status, XML multistatus, metadata or file bytes."}
            },
        }
        if method == "put":
            spec["requestBody"] = {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        elif method in ("propfind", "lock"):
            spec["requestBody"] = {
                "content": {"application/xml": {"schema": {"type": "string", "maxLength": 65536}}},
                "description": (
                    "Optional xml_body, bounded to 64 KiB; RomM currently ignores "
                    "these request bodies."
                ),
            }
        methods[method] = spec
    return {DAV_PATH: methods}
