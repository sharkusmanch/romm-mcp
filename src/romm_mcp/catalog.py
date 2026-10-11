"""Discover the complete upstream HTTP surface without inflating MCP tool discovery."""

import re
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

from .webdav import DAV_METHODS, webdav_operations

METHODS = {"get", "head", "post", "put", "patch", "delete", "options"} | set(DAV_METHODS)
DYNAMIC_UPLOAD = re.compile(
    r"^/api/roms/\{id\}/(manuals(?:/files)?|walkthroughs/files|soundtracks|screenshots)$"
)


@dataclass
class Operation:
    id: str
    method: str
    path: str
    spec: dict


class Catalog:
    def __init__(self, document, settings):
        self.document = document
        self.settings = settings
        self.operations = {}

        def check_refs(value):
            if isinstance(value, dict):
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if key in value and not value[key].startswith("#"):
                        raise ToolError("External OpenAPI references are not supported.")
                for v in value.values():
                    check_refs(v)
            elif isinstance(value, list):
                for v in value:
                    check_refs(v)

        check_refs(document)
        paths = dict(document.get("paths", {}))
        version = re.match(
            r"^v?(\d+)\.(\d+)(?:\.|$)", str(document.get("info", {}).get("version", ""))
        )
        if version and tuple(map(int, version.groups())) >= (5, 4):
            for path, definitions in webdav_operations().items():
                if path in paths:
                    raise ToolError(
                        "Upstream WebDAV definitions conflict with the verified supplement."
                    )
                paths[path] = definitions
        for path, item in paths.items():
            # Literal route templates never need percent encoding; reject it so
            # proxy decoding cannot turn a declared route into another endpoint.
            if (
                not path.startswith("/api/")
                or any(x in path for x in ("..", "//", "?", "#", "\\", "%"))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
            ):
                raise ToolError("OpenAPI contains an unsafe API path.")
            for method, spec in item.items():
                if method not in METHODS:
                    continue
                key = spec.get("operationId", f"{method.upper()} {path}")
                if key in self.operations:
                    raise ToolError("Duplicate OpenAPI operation ID.")
                merged = dict(spec)
                merged["parameters"] = item.get("parameters", []) + spec.get("parameters", [])
                if path == "/api/roms/{id}/content/{file_name}" and method in ("get", "head"):
                    if not any(p.get("name") == "hidden_folder" for p in merged["parameters"]):
                        merged["parameters"].append(
                            {
                                "name": "hidden_folder",
                                "in": "query",
                                "schema": {"type": "boolean"},
                                "description": "Use the muOS hidden multi-disc folder layout.",
                            }
                        )
                self.operations[key] = Operation(key, method, path, merged)

    def get(self, operation_id):
        try:
            return self.operations[operation_id]
        except KeyError:
            raise ToolError("Unknown operation_id; use romm_api_list.") from None

    def is_write(self, op, query=None):
        q = query or {}
        return (
            op.method not in ("get", "head", "options", "propfind")
            or (op.spec.get("x-romm-webdav") and op.method in ("get", "head"))
            or (
                op.method == "get"
                and op.path == "/api/roms/{id}/content/{file_name}"
                and q.get("format") is not None
            )
            or op.path
            in (
                "/api/login/openid",
                "/api/oauth/openid",
                "/api/memory-cards/{id}/versions",
                "/api/tasks/status",
            )
            or (
                op.path == "/api/saves/{id}/content"
                and (
                    q.get("session_id") is not None
                    or (
                        q.get("device_id") is not None
                        and q.get("optimistic", True) not in (False, "false", "0", 0)
                    )
                )
            )
        )

    def requirements(self, op, query=None):
        p, m = op.path, op.method
        tags = set(op.spec.get("tags", []))
        gates = set()
        if self.is_write(op, query):
            gates.add("writes")
        if (
            (m == "delete" and p != "/api/collections/{id}/roms")
            or p.endswith("/delete")
            or "/regenerate" in p
        ):
            gates |= {"writes", "destructive"}
        if tags & {"auth", "device-auth", "client-tokens"} or p.startswith("/api/auth/"):
            gates.add("auth")
        if (
            p.startswith(("/api/config", "/api/setup", "/api/logs"))
            or (p.startswith("/api/users") and p != "/api/users/me")
            or (p.startswith("/api/permissions") and p not in ("/api/permissions/me",))
            or p
            in (
                "/api/client-tokens/all",
                "/api/streaming/containers",
                "/api/streaming/sessions",
                "/api/streaming/desktop",
            )
            or p.endswith("/admin")
        ):
            gates.add("admin")
        scopes = [
            s for sec in op.spec.get("security", []) for s in sec.get("OAuth2PasswordBearer", [])
        ]
        if any(s.startswith(("users.", "config.", "permissions.")) for s in scopes) and p not in (
            "/api/users/me",
            "/api/permissions/me",
        ):
            gates.add("admin")
        if (
            p.startswith(("/api/tasks", "/api/export/"))
            or (p.startswith("/api/streaming") and self.is_write(op))
            or p.endswith(("/ra/refresh", "/convert-to-folder", "/redownload", "/push-pull"))
        ):
            gates.add("tasks")
        if (
            "multipart/form-data" in op.spec.get("requestBody", {}).get("content", {})
            or "/content" in p
            or p == "/api/roms/{id}/easyrpg/{path}"
            or p.endswith("/avatar")
            or p == "/api/roms/download"
            or "/upload" in p
            or DYNAMIC_UPLOAD.fullmatch(p)
            and m == "post"
            or p.endswith("/patch")
        ):
            gates.add("files")
        file_family = p.startswith(
            (
                "/api/roms/",
                "/api/saves",
                "/api/states",
                "/api/screenshots",
                "/api/firmware",
                "/api/memory-cards",
            )
        )
        if (
            file_family
            and p != "/api/roms/{id}/notes/{note_id}"
            and (m == "delete" or p.endswith("/delete"))
            or p.endswith("/convert-to-folder")
            or (p == "/api/platforms" and m == "post")
            or p.startswith("/api/tasks/run/")
        ):
            gates.add("files")
        if DYNAMIC_UPLOAD.fullmatch(p) and m == "post":
            gates.add("destructive")
        # Starting an upload supports replacing existing files through several formats.
        if p == "/api/roms/upload/start":
            gates.add("destructive")
        overwrite = (
            (m == "put" and p in ("/api/roms/{id}", "/api/saves/{id}", "/api/states/{id}"))
            or (
                m == "post"
                and p in ("/api/saves", "/api/states", "/api/screenshots", "/api/firmware")
            )
            or p.endswith("/manuals/redownload")
        )
        if overwrite:
            gates |= {"writes", "destructive", "files"}
        if p.endswith("/walkthroughs/gamefaqs"):
            gates |= {"writes", "files"}
        if p.startswith("/api/tasks/run/") or p == "/api/roms/upload/{upload_id}/complete":
            gates.add("destructive")
        # RomM 5.4 routes contain side effects/admin checks not represented by scopes.
        if (
            p.startswith("/api/notification-channels/apprise-services")
            or (p.startswith("/api/notification-channels") and self.is_write(op))
            or p == "/api/audit-events"
        ):
            gates.add("admin")
        if p.startswith("/api/devices/") and "/installs" in p and self.is_write(op):
            gates |= {"tasks", "files"}
        if p == "/api/tasks/scan":
            gates |= {"writes", "tasks", "files", "destructive"}
        if p in ("/api/saves/{id}/file-name", "/api/states/{id}/file-name"):
            gates.add("files")
        if (
            m == "get"
            and p == "/api/roms/{id}/content/{file_name}"
            and (query or {}).get("format") is not None
        ):
            gates |= {"writes", "tasks", "files"}
        if op.spec.get("x-romm-webdav"):
            gates.add("files")
            if m in ("put", "move", "delete"):
                gates |= {"writes", "destructive"}
        return sorted(gates)

    def check(self, op, query=None, extra=()):
        needed = sorted(set(self.requirements(op, query)) | set(extra))
        disabled = [g for g in needed if not getattr(self.settings, "allow_" + g, False)]
        if disabled:
            raise ToolError(
                "Disabled operation; enable "
                + ", ".join("ROMM_ALLOW_" + g.upper() for g in disabled)
                + "."
            )
        return needed

    def summary(self, op):
        gates = self.requirements(op)
        return {
            "operation_id": op.id,
            "method": op.method.upper(),
            "path": op.path,
            "summary": op.spec.get("summary", "")[:200],
            "tags": op.spec.get("tags", []),
            "required_switches": ["ROMM_ALLOW_" + g.upper() for g in gates],
            "enabled": all(getattr(self.settings, "allow_" + g, False) for g in gates),
            "scopes": sorted(
                {
                    s
                    for sec in op.spec.get("security", [])
                    for s in sec.get("OAuth2PasswordBearer", [])
                }
            ),
        }

    def list(self, query=None, tag=None, method=None, offset=0, limit=20):
        ops = [
            op
            for op in self.operations.values()
            if (
                not query
                or query.casefold()
                in (op.id + " " + op.path + " " + op.spec.get("summary", "")).casefold()
            )
            and (not tag or tag in op.spec.get("tags", []))
            and (not method or method.lower() == op.method)
        ]
        ops.sort(key=lambda op: (op.path, op.method))
        return {
            "version": self.document.get("info", {}).get("version"),
            "items": [self.summary(op) for op in ops[offset : offset + limit]],
            "total": len(ops),
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < len(ops) else None,
        }

    def describe(self, operation_id, schema_ref=None):
        op = self.get(operation_id)
        if schema_ref:
            if not schema_ref.startswith("#/components/schemas/"):
                raise ToolError("schema_ref must name #/components/schemas/...")
            value = self.document
            try:
                for part in schema_ref[2:].split("/"):
                    value = value[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                raise ToolError("Unknown schema reference.") from None
            return {"operation": self.summary(op), "schema_ref": schema_ref, "schema": value}
        corrections = []
        if op.path == "/api/users/{id}" and op.method == "put":
            corrections.append(
                "Avatar UploadFile also accepts multipart/form-data using the UserForm schema, "
                "despite OpenAPI declaring urlencoded only."
            )
        if DYNAMIC_UPLOAD.fullmatch(op.path) and op.method == "post":
            corrections.append(
                "Multipart upload omitted from OpenAPI: file field must equal x-upload-filename; "
                "walkthroughs also support x-doc-author and x-doc-title."
            )
        if op.path == "/api/roms/upload/{upload_id}" and op.method == "put":
            corrections.append(
                "Raw byte body omitted from OpenAPI: supply raw_artifact_id and x-chunk-index."
            )
        if op.path == "/api/saves/{id}/content":
            corrections.append(
                "session_id, or device_id with optimistic not false, changes sync state; use "
                "romm_api_write and ROMM_ALLOW_WRITES."
            )
        return {
            "operation": self.summary(op),
            "parameters": op.spec.get("parameters", []),
            "request_body": op.spec.get("requestBody"),
            "responses": op.spec.get("responses", {}),
            "description": op.spec.get("description", "")[:3000],
            "corrections": corrections,
        }
