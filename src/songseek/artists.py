import secrets
import threading
import time
from collections import OrderedDict

from .errors import SongSeekError, public_error
import re

from .normalize import artists_match, key, normalized, title_key, tolerance, version_flags
from .query import TRUSTED

TOKEN_TTL = 2 * 60 * 60
TOKEN_CAPACITY = 4096
PREVIEW_SEARCH_TIMEOUT = 5


SPLIT = re.compile(r"\s*(?:&|,|，|、|/|;|；|×|\bfeat(?:uring)?\.?(?![a-z])|\bft\.?(?![a-z]))\s*")
FEATURED_WEIGHT = 0.3  # a duet where the artist is billed second still counts, just lower
MAX_CREDITS = 4  # charity singles with dozens of voices are not "their songs"


def credits(artist: str) -> list[str]:
    return [key(part) for part in SPLIT.split(normalized(artist)) if key(part)]


def merge_songs(results: dict, artist: dict) -> list[list]:
    groups = []
    names = [artist["name"], *artist["aliases"]]
    own = {key(n) for n in names}
    for provider, songs in results.items():
        for rank, raw in enumerate(songs):
            if version_flags(raw.title) or version_flags(raw.album) or not any(artists_match(n, raw.artist) for n in names):
                continue
            if len(credits(raw.artist)) > MAX_CREDITS:
                continue
            group = next((g for g in groups if title_key(g[0][0].title) == title_key(raw.title)
                          and artists_match(g[0][0].artist, raw.artist)
                          and (g[0][0].duration is None or raw.duration is None
                               or abs(g[0][0].duration - raw.duration) <= tolerance(raw.duration))), None)
            if group is None:
                group = []
                groups.append(group)
            if not any((r.provider, r.source_id) == (raw.provider, raw.source_id) for r, _ in group):
                group.append((raw, rank))
    def lead(group) -> bool:
        return any((credits(r.artist) or [""])[0] in own for r, _ in group)

    groups.sort(key=lambda g: (-(1 if lead(g) else FEATURED_WEIGHT)
                               * sum(1 / (10 + min(rank for r, rank in g if r.provider == p))
                                     for p in {r.provider for r, _ in g}),
                               -max((r.popularity or 0 for r, _ in g))))
    return [[raw for raw, _ in group] for group in groups[:50]]


class ArtistSongs:
    def __init__(self, service):
        self.service = service
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def put(self, raws: list, token: str | None = None) -> str:
        with self.lock:
            token = token or secrets.token_urlsafe(24)
            if token in self.cache:
                identities = {(r.provider, r.source_id) for r in raws}
                raws = raws + [r for r in self.cache[token][1] if (r.provider, r.source_id) not in identities]
            self.cache[token] = (time.monotonic() + TOKEN_TTL, raws)
            self.cache.move_to_end(token)
            while len(self.cache) > TOKEN_CAPACITY:
                self.cache.popitem(last=False)
            return token

    def get(self, token: str) -> list:
        with self.lock:
            value = self.cache.get(token)
            if value is None or value[0] <= time.monotonic():
                self.cache.pop(token, None)
                raise SongSeekError("歌曲链接已过期，请重新搜索歌手", "not_found", 404)
            self.cache.move_to_end(token)
            return list(value[1])

    def append(self, token: str, raw):
        with self.lock:
            value = self.cache.get(token)
            if value is not None and value[0] > time.monotonic():
                if not any((r.provider, r.source_id) == (raw.provider, raw.source_id) for r in value[1]):
                    self.cache[token] = (value[0], [*value[1], raw])

    def preview(self, token: str, open_preview):
        raws = self.get(token)
        labels = {p.id: p.label for p in self.service.config.providers}
        errors = []
        for index, raw in enumerate(raws):
            try:
                return open_preview(raw, labels[raw.provider], index != 0)
            except Exception as error:
                errors.append(f"{labels[raw.provider]}：{public_error(error)}")

        chosen = raws[0]
        tried = {r.provider for r in raws}
        providers = sorted((p for p in self.service.selected_providers(None) if p not in tried),
                           key=lambda p: p not in TRUSTED)
        deadline = time.monotonic() + PREVIEW_SEARCH_TIMEOUT
        for identifier in providers:
            if time.monotonic() >= deadline:
                break
            found, failures = self.service._search_provider(identifier, f"{chosen.title} {chosen.artist}", deadline)
            errors.extend(failures)
            provider = self.service.providers[identifier]
            with provider.search_budget(deadline):
                for raw in found:
                    if (title_key(raw.title) != title_key(chosen.title)
                            or not artists_match(chosen.artist, raw.artist)
                            or version_flags(raw.title + " " + raw.album + " " + raw.extra.get("version_evidence", ""))
                            or (chosen.duration is not None and raw.duration is not None
                                and abs(raw.duration - chosen.duration) > tolerance(chosen.duration))):
                        continue
                    try:
                        provider.remaining(8)
                        response = open_preview(raw, labels[identifier], True)
                    except Exception as error:
                        errors.append(f"{labels[identifier]}：{public_error(error)}")
                    else:
                        self.append(token, raw)
                        return response
                    if time.monotonic() >= deadline:
                        break
        raise SongSeekError("；".join(dict.fromkeys(errors)), "preview_failed", 502)

    def describe(self, raws: list, token: str) -> dict:
        raw = raws[0]
        infos = {p.id: p for p in self.service.config.providers}
        providers = list(dict.fromkeys(r.provider for r in raws))
        popularity = [r.popularity for r in raws if r.popularity is not None]
        bitrate = max((r.bitrate or 0 for r in raws))
        base = f"/api/artist-songs/{token}"
        return {"token": token, "title": raw.title, "artist": raw.artist, "album": raw.album,
                "duration": raw.duration, "popularity": max(popularity) if popularity else None, "flags": [],
                "providers": [{"id": p, "label": infos[p].label} for p in providers],
                "quality_label": f"{bitrate}k" if bitrate else "未知音质",
                "preview_url": base + "/preview", "cover_url": base + "/cover", "lyrics_url": base + "/lyrics"}
