"""Fixed-route RomM API client. Incoming MCP credentials never enter this client."""

import asyncio
from contextlib import contextmanager
from typing import Literal
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .models import (
    CollectionDetail,
    CollectionWrite,
    Metadata,
    Platform,
    Progress,
    ProgressChanges,
    ProgressWrite,
    RomDetail,
    RomFile,
    collection,
    page,
    short,
    summary,
)


class RomMClient:
    def __init__(self, url: str, token: str, *, transport=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "ROMM_URL must be an HTTP(S) base URL without credentials, query or fragment."
            )
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("ROMM_TOKEN is required and must be a single line.")
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(20, connect=5),
            limits=httpx.Limits(max_connections=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.write_lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def request(self, method, path, *, parse_json=True, **kwargs):
        try:
            # Bound upstream payloads as well as model-visible results.
            async with self.http.stream(method, "api/" + path, **kwargs) as response:
                if not response.is_success:
                    hints = {
                        401: "Check ROMM_TOKEN.",
                        403: "Check token scopes and collection ownership.",
                        404: "Refresh the ID using a list or search tool.",
                        429: "RomM is rate limiting requests; try later.",
                    }
                    raise ToolError(
                        f"RomM HTTP {response.status_code}. "
                        + hints.get(response.status_code, "Check upstream health.")
                    )
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise ToolError(
                            "RomM response exceeds 16 MiB; narrow the query or page size."
                        )
                    chunks.append(chunk)
                if not parse_json:
                    return None
                import json

                try:
                    return json.loads(b"".join(chunks))
                except (ValueError, UnicodeError):
                    raise ToolError(
                        "RomM returned invalid JSON; check the base URL and authentication proxy."
                    ) from None
        except httpx.HTTPError:
            message = "RomM request failed or timed out."
            if method != "GET":
                message += " Write outcome is uncertain; read current state before retrying."
            raise ToolError(message) from None

    async def search_roms(
        self, query=None, platform_ids=None, collection_id=None, has_ra=None, limit=20, offset=0
    ):
        params = {
            "limit": limit,
            "offset": offset,
            "with_char_index": False,
            "with_filter_values": False,
            "with_rom_id_index": False,
            "with_files": False,
            "with_total": True,
            "order_by": "id",
            "order_dir": "asc",
        }
        params.update(
            {
                k: v
                for k, v in {
                    "search_term": query,
                    "platform_ids": platform_ids,
                    "collection_id": collection_id,
                    "has_ra": has_ra,
                }.items()
                if v is not None
            }
        )
        raw = await self.request("GET", "roms", params=params)
        return page([summary(x) for x in raw["items"][:limit]], raw["total"], offset, limit)

    async def list_platforms(self, query=None, limit=20, offset=0):
        raw = await self.request("GET", "platforms")
        items = [
            Platform(
                id=x["id"],
                name=short(x.get("display_name") or x.get("name")),
                slug=short(x.get("slug")),
                rom_count=x.get("rom_count", 0),
            )
            for x in raw
        ]
        items = sorted(
            [x for x in items if not query or query.casefold() in x.name.casefold()],
            key=lambda x: x.id,
        )
        return page(items[offset : offset + limit], len(items), offset, limit)

    async def list_collections(self, query=None, limit=20, offset=0):
        raw = await self.request("GET", "collections")
        items = sorted(
            [collection(x) for x in raw if not query or query.casefold() in x["name"].casefold()],
            key=lambda x: x.id,
        )
        return page(items[offset : offset + limit], len(items), offset, limit)

    async def get_collection(self, collection_id, limit=20, offset=0):
        raw = await self.request("GET", f"collections/{collection_id}")
        return CollectionDetail(
            collection=collection(raw),
            roms=await self.search_roms(collection_id=collection_id, limit=limit, offset=offset),
        )

    async def get_rom(self, rom_id, sections=()):
        raw = await self.request(
            "GET", f"roms/{rom_id}" + ("" if "files" in sections else "/simple")
        )
        result = RomDetail(**summary(raw).model_dump(), summary=short(raw.get("summary"), 1500))
        if len(raw.get("summary") or "") > 1500:
            result.truncated_fields.append("summary")
        if "progress" in sections:
            result.progress = Progress.model_validate(raw.get("rom_user") or {})
        if "metadata" in sections:
            md = raw.get("metadatum") or {}
            result.metadata = Metadata(
                genres=[short(x) for x in (md.get("genres") or [])[:20]],
                companies=[short(x) for x in (md.get("companies") or [])[:20]],
                first_release_date=md.get("first_release_date"),
                average_rating=md.get("average_rating"),
                igdb_id=raw.get("igdb_id"),
                hltb_id=raw.get("hltb_id"),
            )
            for key in ("genres", "companies"):
                if len(md.get(key) or []) > 20:
                    result.truncated_fields.append("metadata." + key)
        if "files" in sections:
            files = raw.get("files") or []
            result.files = [
                RomFile(
                    id=x["id"],
                    name=short(x.get("file_name") or x.get("file_path"), 300),
                    size_bytes=x.get("file_size_bytes", 0),
                )
                for x in files[:50]
            ]
            if len(files) > 50:
                result.truncated_fields.append("files")
        return result

    @contextmanager
    def write_outcome(self, target):
        try:
            yield
        except (ToolError, ValueError, KeyError, TypeError):
            raise ToolError(
                f"Write to {target} is uncertain or verification failed; "
                "it may already have applied. "
                "Read current state before retrying."
            ) from None

    async def create_collection(self, name, description=""):
        async with self.write_lock:
            me = await self.request("GET", "users/me")
            raw = await self.request("GET", "collections")
            matches = [x for x in raw if x["name"] == name and x["user_id"] == me["id"]]
            if matches:
                raise ToolError(
                    "Collection name already exists; use list_collections and its ID. "
                    "No collection created."
                )
            with self.write_outcome("collection creation (search by the requested name)"):
                created = await self.request(
                    "POST",
                    "collections",
                    data={"name": name, "description": description},
                    params={"is_public": False},
                )
                verified = await self.request("GET", f"collections/{created['id']}")
                if (
                    verified["name"] != name
                    or verified.get("description", "") != description
                    or verified.get("is_public") is not False
                ):
                    raise ToolError("Collection creation verification failed.")
                return CollectionWrite(collection=collection(verified), verified=True, created=True)

    async def update_collection_roms(
        self, collection_id, rom_ids, operation: Literal["add", "remove"]
    ):
        # Atomic endpoints preserve unrelated members; never replace the collection's full list.
        await self.request("GET", f"collections/{collection_id}")
        with self.write_outcome(f"collection {collection_id}"):
            await self.request(
                "POST" if operation == "add" else "DELETE",
                f"collections/{collection_id}/roms",
                json={"rom_ids": list(dict.fromkeys(rom_ids))},
                parse_json=False,
            )
            raw = await self.request("GET", f"collections/{collection_id}")
            members = set(raw["rom_ids"])
            valid = (
                set(rom_ids).issubset(members)
                if operation == "add"
                else not set(rom_ids).intersection(members)
            )
            if not valid:
                raise ToolError("Collection membership verification failed.")
            return CollectionWrite(collection=collection(raw), verified=True)

    async def update_rom_progress(self, rom_id, changes: ProgressChanges):
        body = changes.model_dump(exclude_unset=True)
        with self.write_outcome(f"ROM {rom_id} progress"):
            await self.request("PUT", f"roms/{rom_id}/props", json=body, parse_json=False)
            raw = await self.request("GET", f"roms/{rom_id}/simple")
            progress = raw.get("rom_user") or {}
            if any(progress.get(key) != value for key, value in body.items()):
                raise ToolError("Progress verification failed.")
            return ProgressWrite(
                rom_id=rom_id, progress=Progress.model_validate(progress), verified=True
            )
