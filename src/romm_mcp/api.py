"""Schema-guided HTTP invocation with bounded output and no arbitrary destinations."""

import asyncio
import json
import re
import secrets
import time
from contextlib import ExitStack
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

import httpx
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from referencing import Registry
from referencing.exceptions import NoSuchResource

from .artifacts import ArtifactStore
from .catalog import DYNAMIC_UPLOAD, Catalog


class Upload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    artifact_id: str
    filename: str
    content_type: str = "application/octet-stream"


class APIRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: dict[str, str | int] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    form: dict[str, Any] | None = None
    files: Annotated[list[Upload], Field(max_length=16)] = Field(default_factory=list)
    raw_artifact_id: str | None = None
    download: bool = False
    response_pointer: Annotated[str, Field(max_length=1000)] = ""
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    auth_mode: Literal["bearer", "basic", "session", "none"] = "bearer"
    auth_session: str | None = None


def select(value, pointer):
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ToolError("response_pointer must be a JSON Pointer.")
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise ValueError()
                value = value[int(part)]
            else:
                value = value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise ToolError("response_pointer does not exist in this response.") from None


def bounded(value, pointer="", offset=0, limit=20):
    value = select(value, pointer)
    if isinstance(value, str):
        data = value[offset : offset + 4096]
        while len(json.dumps(data, ensure_ascii=True)) > 12000:
            data = data[: max(1, len(data) // 2)]
        return {
            "data": data,
            "response_pointer": pointer,
            "offset": offset,
            "total": len(value),
            "next_offset": offset + len(data) if data and offset + len(data) < len(value) else None,
            "truncated_paths": [],
        }
    total = len(value) if isinstance(value, (list, dict, str)) else 1
    if isinstance(value, list):
        data = value[offset : offset + limit]
    elif isinstance(value, dict):
        data = dict(list(value.items())[offset : offset + limit])
    elif isinstance(value, str):
        data = value[offset : offset + 16000]
    else:
        data = value
    count = len(data) if isinstance(data, (list, dict, str)) else 1
    truncated = []

    def trim(item, path, depth=0):
        if depth > 8:
            truncated.append(path)
            return None
        if isinstance(item, str) and len(item) > 2000:
            truncated.append(path)
            return item[:2000]
        if isinstance(item, list):
            if len(item) > limit:
                truncated.append(path)
            return [trim(v, path + "/" + str(i), depth + 1) for i, v in enumerate(item[:limit])]
        if isinstance(item, dict):
            if len(item) > 100:
                truncated.append(path)
            return {
                k: trim(v, path + "/" + k.replace("~", "~0").replace("/", "~1"), depth + 1)
                for k, v in list(item.items())[:100]
            }
        return item

    data = trim(data, pointer)
    encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > 16000:
        data = {
            "preview_json": encoded[:12000],
            "hint": "Select a narrower response_pointer or smaller limit.",
        }
        truncated.append(pointer or "/")
    return {
        "data": data,
        "response_pointer": pointer,
        "offset": offset,
        "total": total,
        "next_offset": offset + count if count and offset + count < total else None,
        "truncated_paths": truncated,
    }


class FullAPI:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self._catalog = None
        self._schema_lock = asyncio.Lock()
        self.artifacts = ArtifactStore(settings.transfer_directory, settings.max_transfer_bytes)
        self.sessions = {}

    def http(self):
        return httpx.AsyncClient(
            base_url=self.settings.romm_url.rstrip("/") + "/",
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )

    async def catalog(self):
        if self._catalog is None:
            async with self._schema_lock:
                if self._catalog is None:
                    try:
                        async with self.http() as client:
                            async with client.stream(
                                "GET",
                                "openapi.json",
                                headers={"Authorization": "Bearer " + self.settings.romm_token},
                            ) as r:
                                if r.status_code != 200:
                                    raise ToolError("Cannot fetch RomM OpenAPI; check base URL.")
                                chunks = []
                                size = 0
                                async for chunk in r.aiter_bytes():
                                    size += len(chunk)
                                    if size > 16 * 1024 * 1024:
                                        raise ToolError("OpenAPI exceeds 16 MiB.")
                                    chunks.append(chunk)
                                self._catalog = Catalog(json.loads(b"".join(chunks)), self.settings)
                    except (httpx.HTTPError, ValueError):
                        raise ToolError(
                            "Cannot load valid RomM OpenAPI; check URL and connectivity."
                        ) from None
        return self._catalog

    def validate(self, catalog, schema, value, label):
        # Catalog has already rejected all non-local refs: validation never fetches a URL.
        document = {**schema, "components": catalog.document.get("components", {})}
        try:

            def refuse_remote(uri):
                raise NoSuchResource(ref=uri)

            error = next(
                Draft202012Validator(
                    document, registry=Registry(retrieve=refuse_remote)
                ).iter_errors(value),
                None,
            )
        except Exception:
            raise ToolError("Unsupported upstream schema; inspect romm_api_describe.") from None
        if error:
            # ValidationError.message can contain credential bodies; never echo it.
            raise ToolError(f"Invalid {label}; expected the schema from romm_api_describe.")

    def prepare(self, catalog, op, request):
        parameters = op.spec.get("parameters", [])
        expected = {p["name"] for p in parameters if p.get("in") == "path"}
        expected |= set(re.findall(r"\{([^}]+)\}", op.path))
        if set(request.path) != expected:
            raise ToolError("Supply exactly the declared path parameters.")
        path = op.path
        for key, value in request.path.items():
            text = str(value)
            decoded = text
            for _ in range(3):
                decoded = unquote(decoded)
            if (
                not text
                or any(ord(c) < 32 for c in text)
                or "\\" in decoded
                or any(x in (".", "..") for x in decoded.split("/"))
            ):
                raise ToolError("Unsafe path parameter.")
            path = path.replace("{" + key + "}", quote(text, safe=""))
        if re.search(r"\{[^}]+\}", path):
            raise ToolError("Missing path parameter.")
        headers = {k.lower(): v for k, v in request.headers.items()}
        allowed = {p["name"].lower() for p in parameters if p.get("in") == "header"} | {
            "accept",
            "range",
            "if-none-match",
            "if-modified-since",
        }
        if DYNAMIC_UPLOAD.fullmatch(op.path):
            allowed |= {"x-doc-author", "x-doc-title"}
        prohibited = {
            "authorization",
            "proxy-authorization",
            "host",
            "cookie",
            "content-length",
            "transfer-encoding",
            "connection",
        }
        if (
            set(headers) - allowed
            or set(headers) & prohibited
            or any("\r" in v or "\n" in v for v in headers.values())
        ):
            raise ToolError(
                "Only declared safe request headers are supported; auth uses operator settings."
            )
        query = dict(request.query)
        expected_query = {p["name"] for p in parameters if p.get("in") == "query"}
        if set(query) - expected_query:
            raise ToolError("Unknown query parameter; inspect romm_api_describe.")
        for p in parameters:
            where = p.get("in")
            name = p["name"]
            values = {"path": request.path, "query": query, "header": headers}.get(where, {})
            key = name.lower() if where == "header" else name
            if p.get("required") and key not in values:
                raise ToolError(f"Missing {where} parameter: {name}.")
            if key in values:
                v = values[key]
                # Header values are wire strings; coerce numeric/bool headers for schema validation.
                if where == "header":
                    kind = p.get("schema", {}).get("type")
                    try:
                        if kind == "integer":
                            v = int(v)
                        elif kind == "number":
                            v = float(v)
                    except ValueError:
                        raise ToolError("Invalid header value type.") from None
                self.validate(catalog, p.get("schema", {}), v, where + " parameter")
        if op.path == "/api/roms" and op.method == "get":
            for key in ("with_char_index", "with_filter_values", "with_rom_id_index"):
                if key in expected_query:
                    query.setdefault(key, False)
            if "limit" in expected_query:
                query.setdefault("limit", 20)
        params = []
        for key, value in query.items():
            if value is None:
                continue
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, (dict, list)):
                    raise ToolError("Query values must be scalar or scalar arrays.")
                params.append((key, str(v).lower() if isinstance(v, bool) else str(v)))
        return path.lstrip("/"), headers, params

    async def call(self, operation_id, request, *, write=False):
        catalog = await self.catalog()
        op = catalog.get(operation_id)
        extra = set()
        if request.files or request.raw_artifact_id:
            extra |= {"files", "writes"}
        if request.download:
            extra.add("files")
        if request.auth_mode != "bearer" or request.auth_session:
            extra.add("auth")
        catalog.check(op, request.query, extra)
        if catalog.is_write(op, request.query) and not write:
            raise ToolError("This operation changes state; use romm_api_write.")
        if not write and (
            request.files
            or request.raw_artifact_id
            or request.form is not None
            or "body" in request.model_fields_set
        ):
            raise ToolError("Request bodies require romm_api_write.")
        path, headers, params = self.prepare(catalog, op, request)
        body_kinds = (
            int("body" in request.model_fields_set)
            + int(request.form is not None)
            + int(request.raw_artifact_id is not None)
        )
        if body_kinds > 1 or request.files and request.raw_artifact_id:
            raise ToolError("Choose JSON body, form/multipart, or raw_artifact_id.")
        if request.files and "body" in request.model_fields_set:
            raise ToolError("Files cannot accompany JSON body.")
        for f in request.files:
            if (
                any(c in f.filename + f.field + f.content_type for c in "\r\n")
                or "/" in f.filename
                or "\\" in f.filename
            ):
                raise ToolError("Upload filename must be a basename without control characters.")
        if DYNAMIC_UPLOAD.fullmatch(op.path) and op.method == "post":
            name = headers.get("x-upload-filename")
            if len(request.files) != 1 or request.files[0].field != name:
                raise ToolError(
                    "This upload requires one multipart file whose field equals x-upload-filename."
                )
        content = op.spec.get("requestBody", {}).get("content", {})
        if "body" in request.model_fields_set:
            if "application/json" not in content:
                raise ToolError("This operation does not accept JSON body.")
            self.validate(
                catalog, content["application/json"].get("schema", {}), request.body, "JSON body"
            )
        if op.spec.get("requestBody", {}).get("required") and not body_kinds and not request.files:
            raise ToolError("This operation requires a request body.")
        if request.form is not None or request.files:
            mime = (
                "multipart/form-data"
                if request.files or "multipart/form-data" in content
                else "application/x-www-form-urlencoded"
            )
            if (
                mime not in content
                and not DYNAMIC_UPLOAD.fullmatch(op.path)
                and not (
                    op.path == "/api/users/{id}"
                    and op.method == "put"
                    and mime == "multipart/form-data"
                )
            ):
                raise ToolError("This operation does not accept this form encoding.")
            form_schema = content.get(
                mime, content.get("application/x-www-form-urlencoded", {})
            ).get("schema", {})
            if any(v is None for v in (request.form or {}).values()):
                raise ToolError(
                    "Form null has no wire representation; omit the field or use an empty string."
                )
            fields = dict(request.form or {})
            for f in request.files:
                if f.field in fields:
                    old = fields[f.field]
                    fields[f.field] = (
                        old + [f.filename] if isinstance(old, list) else [old, f.filename]
                    )
                else:
                    fields[f.field] = f.filename
            # File arrays use arrays even for a single attachment.
            root = form_schema
            if "$ref" in root:
                root = select(catalog.document, root["$ref"][1:])
            for k, v in list(fields.items()):
                if root.get("properties", {}).get(k, {}).get("type") == "array" and not isinstance(
                    v, list
                ):
                    fields[k] = [v]
            self.validate(catalog, form_schema, fields, "form body")
        if request.raw_artifact_id and op.path != "/api/roms/upload/{upload_id}":
            raise ToolError("Raw upload is supported only for the declared ROM chunk endpoint.")
        if (
            op.path == "/api/roms/upload/{upload_id}"
            and op.method == "put"
            and not request.raw_artifact_id
        ):
            raise ToolError("Upload chunk requires raw_artifact_id.")
        session_id = None
        cookies = None
        self.sessions = {k: v for k, v in self.sessions.items() if time.monotonic() - v[0] < 600}
        if request.auth_session:
            if request.auth_session == "new":
                if len(self.sessions) >= 8:
                    raise ToolError(
                        "Auth session limit reached; sessions expire after ten minutes."
                    )
                session_id = secrets.token_hex(16)
                cookies = httpx.Cookies()
            else:
                session_id = request.auth_session
                if session_id not in self.sessions:
                    raise ToolError("Unknown or expired auth_session.")
                cookies = httpx.Cookies(self.sessions[session_id][1])
        elif request.auth_mode == "session":
            raise ToolError("Session mode requires auth_session=new or a returned handle.")
        if request.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + self.settings.romm_token
        auth = None
        if request.auth_mode == "basic":
            if not self.settings.romm_username or not self.settings.romm_password:
                raise ToolError("Basic authentication requires ROMM_USERNAME and ROMM_PASSWORD.")
            auth = httpx.BasicAuth(self.settings.romm_username, self.settings.romm_password)
        download_id = None
        dispatched = False
        try:
            with ExitStack() as stack:
                kwargs = {}
                if "body" in request.model_fields_set:
                    kwargs["json"] = request.body
                if request.raw_artifact_id:
                    f = stack.enter_context(self.artifacts.open_file(request.raw_artifact_id))
                    headers["Content-Type"] = "application/octet-stream"
                    headers["Content-Length"] = str(
                        self.artifacts.info(request.raw_artifact_id)["size_bytes"]
                    )

                    async def chunks():
                        while chunk := f.read(65536):
                            yield chunk

                    kwargs["content"] = chunks()
                elif request.files or request.form is not None:
                    data = {
                        k: json.dumps(v, separators=(",", ":"))
                        if isinstance(v, (dict, list))
                        else str(v).lower()
                        if isinstance(v, bool)
                        else str(v)
                        for k, v in (request.form or {}).items()
                    }
                    files = [
                        (
                            f.field,
                            (
                                f.filename,
                                stack.enter_context(self.artifacts.open_file(f.artifact_id)),
                                f.content_type,
                            ),
                        )
                        for f in request.files
                    ]
                    # httpx needs a files part to select multipart, even for a file-less form.
                    if "multipart/form-data" in content or files:
                        kwargs["files"] = [(k, (None, v)) for k, v in data.items()] + files
                    else:
                        kwargs["data"] = data
                async with self.http() as client:
                    if cookies is not None:
                        client.cookies = cookies
                    dispatched = True
                    async with client.stream(
                        op.method.upper(),
                        path,
                        params=params,
                        headers=headers,
                        auth=auth,
                        **kwargs,
                    ) as response:
                        if session_id:
                            self.sessions[session_id] = (
                                time.monotonic(),
                                httpx.Cookies(client.cookies),
                            )
                        safe_headers = {
                            k: v
                            for k, v in response.headers.items()
                            if k
                            in (
                                "content-type",
                                "content-length",
                                "content-range",
                                "etag",
                                "last-modified",
                                "retry-after",
                                "location",
                                "content-disposition",
                            )
                        }
                        result = {
                            "operation_id": op.id,
                            "status_code": response.status_code,
                            "headers": safe_headers,
                            "verification": "not_performed"
                            if catalog.is_write(op, request.query)
                            else "not_applicable",
                        }
                        if session_id:
                            result["auth_session"] = session_id
                        if response.is_redirect:
                            return {**result, "redirect_followed": False}
                        if not response.is_success:
                            # Server errors may contain paths or credentials; do not echo bodies.
                            raise ToolError(
                                f"RomM HTTP {response.status_code}; "
                                "check arguments, scopes and upstream state."
                            )
                        if op.method == "head" or response.status_code == 204:
                            return {**result, "data": None}
                        if (
                            "x-accel-redirect" in response.headers
                            or "x-archive-files" in response.headers
                        ):
                            raise ToolError(
                                "Use the RomM nginx front door for downloads, not the internal "
                                "API backend."
                            )
                        if request.download:
                            download_id = self.artifacts.create()["artifact_id"]
                            async for chunk in response.aiter_bytes():
                                self.artifacts.append_bytes(download_id, chunk)
                            return {**result, "artifact": self.artifacts.info(download_id)}
                        chunks_out = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 16 * 1024 * 1024:
                                raise ToolError(
                                    "Response exceeds 16 MiB; use download=true or upstream "
                                    "pagination."
                                )
                            chunks_out.append(chunk)
                        raw = b"".join(chunks_out)
                        if not raw:
                            return {**result, "data": None}
                        ctype = response.headers.get("content-type", "").split(";")[0]
                        if "json" in ctype:
                            try:
                                value = json.loads(raw)
                            except ValueError:
                                raise ToolError("Invalid upstream JSON.") from None
                        elif ctype.startswith("text/"):
                            value = raw.decode("utf-8", errors="replace")
                        else:
                            raise ToolError(
                                "Binary response: repeat with download=true and ROMM_ALLOW_FILES."
                            )
                        return {
                            **result,
                            **bounded(
                                value, request.response_pointer, request.offset, request.limit
                            ),
                        }
        except (httpx.HTTPError, ToolError, ValueError, KeyError, TypeError) as error:
            if download_id:
                self.artifacts.delete(download_id)
            if dispatched and catalog.is_write(op, request.query):
                raise ToolError(
                    "Write dispatched; outcome or response processing is uncertain. Read current "
                    "state before retrying; no automatic retry."
                ) from None
            if isinstance(error, ToolError):
                raise
            raise ToolError("RomM request failed; check connectivity and schema.") from None
        except BaseException:
            if download_id:
                self.artifacts.delete(download_id)
            raise

    def close(self):
        self.artifacts.close()
        self.sessions.clear()
