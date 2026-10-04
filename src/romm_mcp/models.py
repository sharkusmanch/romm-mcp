"""Small, bounded representations of the parts of RomM agents use."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[int, Field(gt=0)]
Limit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0)]
Status = Literal["incomplete", "finished", "completed_100", "retired", "never_playing"]


class Progress(BaseModel):
    status: Status | None = None
    backlogged: bool = False
    now_playing: bool = False
    hidden: bool = False
    rating: int = 0
    difficulty: int = 0
    completion: int = 0
    last_played: str | None = None


class ProgressChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Status | None = None
    backlogged: bool | None = None
    now_playing: bool | None = None
    hidden: bool | None = None
    rating: Annotated[int, Field(ge=0, le=10)] | None = None
    difficulty: Annotated[int, Field(ge=0, le=10)] | None = None
    completion: Annotated[int, Field(ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def meaningful(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one progress field.")
        if any(getattr(self, k) is None for k in self.model_fields_set if k != "status"):
            raise ValueError("Only status may be null (clears status).")
        return self


class RomSummary(BaseModel):
    id: int
    name: str
    platform_id: int | None = None
    platform: str = ""
    regions: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    ra_id: int | None = None


class Platform(BaseModel):
    id: int
    name: str
    slug: str = ""
    rom_count: int = 0


class Collection(BaseModel):
    id: int
    name: str
    description: str = ""
    rom_count: int = 0
    is_public: bool = False
    is_favorite: bool = False


class Page[T](BaseModel):
    items: list[T]
    total: int
    offset: int
    limit: int
    next_offset: int | None


class Metadata(BaseModel):
    genres: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    first_release_date: int | None = None
    average_rating: float | None = None
    igdb_id: int | None = None
    hltb_id: int | None = None


class RomFile(BaseModel):
    id: int
    name: str
    size_bytes: int = 0


class RomDetail(RomSummary):
    summary: str = ""
    metadata: Metadata | None = None
    progress: Progress | None = None
    files: list[RomFile] | None = None
    truncated_fields: list[str] = Field(default_factory=list)


class CollectionDetail(BaseModel):
    collection: Collection
    roms: Page[RomSummary]


class CollectionWrite(BaseModel):
    collection: Collection
    verified: bool
    created: bool | None = None


class ProgressWrite(BaseModel):
    rom_id: int
    progress: Progress
    verified: bool


def short(value, limit=200):
    return str(value or "")[:limit]


def summary(raw):
    return RomSummary(
        id=raw["id"],
        name=short(raw.get("name") or raw.get("fs_name")),
        platform_id=raw.get("platform_id"),
        platform=short(raw.get("platform_display_name")),
        regions=[short(x, 50) for x in (raw.get("regions") or [])[:10]],
        languages=[short(x, 50) for x in (raw.get("languages") or [])[:10]],
        ra_id=raw.get("ra_id"),
    )


def collection(raw):
    return Collection(
        id=raw["id"],
        name=short(raw.get("name")),
        description=short(raw.get("description"), 500),
        rom_count=raw.get("rom_count", len(raw.get("rom_ids", []))),
        is_public=raw.get("is_public", False),
        is_favorite=raw.get("is_favorite", False),
    )


def page(items, total, offset, limit):
    next_offset = offset + len(items)
    return Page(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        next_offset=next_offset if items and next_offset < total else None,
    )
