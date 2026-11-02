from dataclasses import field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.dataclasses import dataclass

TrackStatus = Literal["pending", "searching", "matched", "needs_review", "not_found", "queued", "downloading", "done", "failed", "skipped"]
VersionFlag = Literal["live", "dj", "remix", "instrumental", "cover", "speed", "clip"]
Source = Literal["xlsx", "csv", "md", "text", "netease", "search"]
STATUSES = ("pending", "searching", "matched", "needs_review", "not_found", "queued", "downloading", "done", "failed", "skipped")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class ParsedRow(Model):
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field(default="", max_length=500)
    album: str = Field(default="", max_length=500)
    duration: float | None = Field(default=None, gt=0)


class ColumnMapping(Model):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration: str | None = None


class ParseResult(Model):
    name: str
    source: Source
    columns: list[str] = Field(default_factory=list)
    mapping: ColumnMapping = Field(default_factory=ColumnMapping)
    rows: list[ParsedRow]
    warnings: list[str] = Field(default_factory=list)


class Settings(Model):
    output_dir: str = "~/Music/SongSeek"
    filename_template: str = "{artist} - {title}"
    group_by_playlist: bool = True
    proxy: str = ""
    search_concurrency: int = Field(default=4, ge=1, le=32)
    download_concurrency: int = Field(default=2, ge=1, le=16)
    auto_approve: bool = True
    netease_cookie_set: bool = False


class DownloadedFile(Model):
    path: str
    size: int
    format: str
    bitrate: int | None
    duration: float
    sha256: str
    provider: str | None = None
    provider_label: str | None = None


class Track(ParsedRow):
    id: int
    playlist_id: int
    position: int
    flags: list[VersionFlag]
    status: TrackStatus
    chosen_candidate_id: int | None = None
    chosen_by: Literal["auto", "user"] | None = None
    best_score: float | None = None
    candidate_count: int = 0
    file: DownloadedFile | None = None
    error: str | None = None
    note: str = ""
    updated_at: str


class Playlist(Model):
    id: int
    name: str
    source: Source
    created_at: str
    counts: dict[str, int]


class CheckItem(Model):
    key: Literal["title", "artist", "duration", "version", "quality", "popularity"]
    ok: bool | None
    label: str


class Quality(Model):
    format: str = "mp3"
    bitrate: int | None = None
    label: str = "未知音质"


class Candidate(Model):
    id: int
    track_id: int
    provider: str
    provider_label: str
    source_id: str
    title: str
    artist: str
    album: str
    duration: float | None
    quality: Quality
    popularity: float | None
    score: float
    verdict: Literal["match", "review", "reject"]
    checks: list[CheckItem]
    flags: list[VersionFlag]
    preview_url: str
    needs_member: bool = False
    web_url: str | None
    rank: int


class Job(Model):
    id: int
    kind: Literal["search", "download"]
    playlist_id: int | None
    status: Literal["running", "done", "cancelled", "error"]
    total: int
    completed: int = 0
    failed: int = 0
    current: str | None = None
    started_at: str
    finished_at: str | None = None


class Health(Model):
    ok: bool
    latency_ms: float | None
    message: str
    checked_at: str


class ProviderInfo(Model):
    id: str
    label: str
    max_quality: str
    needs: Literal["cookie", "proxy"] | None = None
    enabled: bool = True
    order: int
    health: Health | None = None


@dataclass(config=ConfigDict(allow_inf_nan=False))
class RawCandidate:
    provider: str
    source_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float | None = Field(default=None, gt=0)
    format: str = "mp3"
    bitrate: int | None = Field(default=None, gt=0)
    popularity: float | None = Field(default=None, ge=0, le=1)
    web_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AudioSource:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    ext: str = "mp3"
    bitrate: int | None = None
    local_path: str | None = None
