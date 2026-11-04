import csv
import io
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .artists import ArtistSongs, merge_songs
from .config import Config
from .covers import CoverManager
from .db import Database
from .downloader import Downloader, PreviewCache
from .errors import SongSeekError, log_failure, public_error
from .events import Broadcaster
from .importers import parse_file, parse_text
from .matching import evaluate, evaluate_candidates, same_recording, sort_candidates
from .lyrics import LyricsManager, has_lyrics_file
from .models import Candidate, Health, Job, ParsedRow, Playlist, RawCandidate, STATUSES, Track, now
from .normalize import title_key, version_flags
from .providers import create_providers
from .query import TRUSTED, detect_artist, parse_query
from .providers.netease import NeteaseProvider

BUSY = {"searching", "queued", "downloading"}
SEARCH_TIMEOUT = 9.5


class Service:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.db = Database(self.config.home / "songseek.db")
        self.events = Broadcaster()
        self.providers = create_providers(self.config)
        self.provider_pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="songseek-provider")
        self.downloader = Downloader(self.config)
        self.previews = PreviewCache(self.config)
        self.lyrics = LyricsManager(self)
        self.covers = CoverManager(self)
        self.artist_songs = ArtistSongs(self)
        self._locks_guard = threading.Lock()
        self._locks: dict[int, threading.RLock] = {}
        self._recover()
        from .jobs import JobManager
        self.jobs = JobManager(self)

    @contextmanager
    def lock(self, track_id: int):
        with self._locks_guard:
            lock = self._locks.setdefault(track_id, threading.RLock())
        if not lock.acquire(blocking=False):
            raise SongSeekError("曲目正在执行任务，请稍后重试", "track_busy", 409)
        try:
            yield
        finally:
            lock.release()

    def _recover(self):
        for data in self.db.list("tracks"):
            track = Track(**data)
            if track.status in BUSY:
                track.status = "matched" if track.chosen_candidate_id else "needs_review" if track.candidate_count else "pending"
                track.error = "上次运行中断，可重新执行"
                track.updated_at = now()
                self.db.save("tracks", track.model_dump())
        for data in self.db.list("jobs"):
            if data["status"] == "running":
                data.update(status="error", finished_at=now(), current=None)
                self.db.save("jobs", data)

    def parse(self, *, text: str | None = None, url: str | None = None, data: bytes | None = None, filename: str = "", mapping: dict | None = None):
        if sum(value is not None for value in (text, url, data)) != 1:
            raise SongSeekError("请选择一种导入方式", "invalid_import")
        if data is not None:
            return parse_file(data, filename, mapping)
        if url is not None:
            return NeteaseProvider(self.config).playlist(url)
        return parse_text(text, mapping=mapping)

    def playlist(self, identifier: int) -> Playlist:
        data = self.db.get("playlists", identifier)
        counts = dict.fromkeys(STATUSES, 0)
        tracks = self.db.list("tracks", "playlist_id", identifier)
        for track in tracks:
            counts[track["status"]] += 1
        counts["total"] = len(tracks)
        return Playlist(**(data | {"counts": counts}))

    def playlists(self) -> list[Playlist]:
        return [self.playlist(data["id"]) for data in reversed(self.db.list("playlists"))]

    def tracks(self, playlist_id: int) -> list[Track]:
        self.db.get("playlists", playlist_id)
        return sorted([Track(**data) for data in self.db.list("tracks", "playlist_id", playlist_id)], key=lambda t: t.position)

    def track(self, identifier: int) -> Track:
        return Track(**self.db.get("tracks", identifier))

    def _playlist_event(self, identifier: int):
        self.events.emit("playlist", self.playlist(identifier).model_dump())

    def save_track(self, track: Track) -> Track:
        track.updated_at = now()
        self.db.save("tracks", track.model_dump())
        self.events.emit("track", track.model_dump())
        self._playlist_event(track.playlist_id)
        return track

    def create_playlist(self, name: str, source: str, rows: list[ParsedRow]) -> Playlist:
        if not name.strip():
            raise SongSeekError("清单名称不能为空", "invalid_name")
        with self.db.transaction():
            identifier = self.db.insert("playlists", {"id": 0, "name": name.strip(), "source": source, "created_at": now(), "counts": {}})
            for position, row in enumerate(rows, 1):
                self._add_track(identifier, row, position)
        self._playlist_event(identifier)
        return self.playlist(identifier)

    def _add_track(self, playlist_id: int, row: ParsedRow, position: int) -> Track:
        track = Track(**row.model_dump(), id=0, playlist_id=playlist_id, position=position, flags=version_flags(row.title), status="pending", updated_at=now())
        track.id = self.db.insert("tracks", track.model_dump(), playlist_id=playlist_id)
        self.events.emit("track", track.model_dump())
        return track

    def rename_playlist(self, identifier: int, name: str) -> Playlist:
        playlist = self.playlist(identifier)
        if not name.strip():
            raise SongSeekError("清单名称不能为空", "invalid_name")
        playlist.name = name.strip()
        self.db.save("playlists", playlist.model_dump())
        self._playlist_event(identifier)
        return playlist

    def delete_playlist(self, identifier: int):
        if any(t.status in BUSY for t in self.tracks(identifier)) or self.jobs.active_playlist(identifier):
            raise SongSeekError("清单仍在执行任务，请取消并等待当前曲目完成", "track_busy", 409)
        self.db.delete("playlists", identifier)

    def delete_track(self, identifier: int):
        with self.lock(identifier):
            track = self.track(identifier)
            self.ensure_idle(track)
            self.db.delete("tracks", identifier)
            self._playlist_event(track.playlist_id)

    def ensure_idle(self, track: Track):
        if track.status in BUSY or self.jobs.track_active(track.id):
            raise SongSeekError("曲目正在执行任务，请稍后重试", "track_busy", 409)

    def update_track(self, identifier: int, values: dict) -> Track:
        with self.lock(identifier):
            track = self.track(identifier)
            self.ensure_idle(track)
            changed = any(k not in ("note", "lyrics_wanted") and getattr(track, k) != v for k, v in values.items())
            data = track.model_dump() | values
            if changed:
                with self.db.transaction() as connection:
                    connection.execute("DELETE FROM candidates WHERE track_id=?", (identifier,))
                data.update(flags=version_flags(data["title"]), status="pending", chosen_candidate_id=None, chosen_by=None,
                            candidate_count=0, best_score=None, error=None, file=None)
            return self.save_track(Track(**data))

    def candidates(self, track_id: int) -> list[Candidate]:
        self.track(track_id)
        return sorted([Candidate(**data) for data in self.db.list("candidates", "track_id", track_id)], key=lambda c: c.rank)

    def candidate(self, identifier: int) -> Candidate:
        return Candidate(**self.db.get("candidates", identifier))

    def raw_candidate(self, identifier: int) -> RawCandidate:
        return RawCandidate(**self.db.raw_candidate(identifier))

    def choose(self, track_id: int, candidate_id: int) -> Track:
        with self.lock(track_id):
            track = self.track(track_id)
            self.ensure_idle(track)
            candidate = self.candidate(candidate_id)
            if candidate.track_id != track_id:
                raise SongSeekError("该候选不属于此曲目", "invalid_candidate")
            track.status, track.chosen_candidate_id, track.chosen_by, track.error = "matched", candidate_id, "user", None
            return self.save_track(track)

    def _search_provider(self, provider_id: str, query: str, deadline: float):
        provider = self.providers[provider_id]
        try:
            with provider.search_budget(deadline):
                provider.remaining(8)
                return provider.search(query, 15), []
        except Exception as error:
            return [], [f"{provider_id}：{log_failure(provider_id, error)}"]

    def _rank_search(self, track: Track, found: dict) -> list[Candidate]:
        raws = dict(found)
        for candidate in self.candidates(track.id):
            raw = self.raw_candidate(candidate.id)
            raws.setdefault((raw.provider, raw.source_id), raw)
        ranked = sort_candidates(evaluate_candidates(track, list(raws.values()), {p.id: p.label for p in self.config.providers}, netease_cookie_set=self.config.settings.netease_cookie_set), {p.id: p.order for p in self.config.providers})
        with self.db.transaction() as connection:
            for candidate in ranked:
                raw = raws[(candidate.provider, candidate.source_id)]
                existing = connection.execute("SELECT id FROM candidates WHERE track_id=? AND provider=? AND source_id=?", (track.id, candidate.provider, candidate.source_id)).fetchone()
                if existing:
                    candidate.id = existing["id"]
                    connection.execute("UPDATE candidates SET raw=? WHERE id=?", (json.dumps(asdict(raw), ensure_ascii=False), candidate.id))
                else:
                    candidate.id = self.db.insert("candidates", candidate.model_dump(), track_id=track.id, provider=candidate.provider, source_id=candidate.source_id, raw=json.dumps(asdict(raw), ensure_ascii=False))
                candidate.preview_url = f"/api/candidates/{candidate.id}/preview"
                self.db.save("candidates", candidate.model_dump())
        return ranked

    def search_track(self, track_id: int, query: str | None = None, providers: list[str] | None = None, *, in_job: bool = False, infer_query: bool = False, on_event: Callable[[dict], None] | None = None, prepared=None) -> dict:
        with self.lock(track_id):
            track = self.track(track_id)
            if not in_job:
                self.ensure_idle(track)
            enabled = [p.id for p in self.config.providers if p.enabled]
            if providers is not None:
                if any(p not in enabled for p in providers):
                    raise SongSeekError("所选音源不存在或未启用", "unknown_provider")
                enabled = list(dict.fromkeys(providers))
            track.status, track.error = "searching", None
            self.save_track(track)
            futures = {}
            try:
                infos = {p.id: p for p in self.config.providers}
                if on_event and prepared is None:
                    on_event({"type": "start", "track_id": track_id,
                              "providers": [{"id": p, "label": infos[p].label} for p in enabled]})
                first_query = query or f"{track.title} {track.artist}".strip()
                if prepared is None:
                    deadline = time.monotonic() + SEARCH_TIMEOUT
                    futures = {self.provider_pool.submit(self._search_provider, p, first_query, deadline): p for p in enabled}
                else:
                    futures, deadline = prepared
                fallback = set()
                found, errors = {}, []

                def rank():
                    if infer_query:
                        ordered = sorted(found.values(), key=lambda raw: infos[raw.provider].order)
                        track.title, track.artist = parse_query(query, ordered)
                        track.flags = version_flags(track.title)
                    return self._rank_search(track, found)

                def progress(provider_id, error):
                    if not on_event:
                        return  # batch jobs only need the final ranking
                    ranked = rank()
                    if on_event:
                        on_event({"type": "progress", "provider": provider_id, "label": infos[provider_id].label,
                                  "ok": error is None, "error": error,
                                  "candidates": [c.model_dump() for c in ranked]})

                while futures and (time.monotonic() < deadline or any(f.done() for f in futures)):
                    completed, _ = wait(futures, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
                    for future in completed:
                        provider_id = futures.pop(future)
                        raw_candidates, failures = future.result()
                        errors.extend(failures)
                        for raw in raw_candidates:
                            found[(raw.provider, raw.source_id)] = raw
                        progress(provider_id, failures[0].removeprefix(f"{provider_id}：") if failures else None)
                        usable = [evaluate(track, raw, infos[provider_id].label, netease_cookie_set=self.config.settings.netease_cookie_set) for raw in raw_candidates]
                        if (not infer_query and not failures and provider_id not in fallback and first_query != track.title
                                and not any(c.verdict != "reject" and c.checks[1].ok is not False for c in usable)):
                            fallback.add(provider_id)
                            futures[self.provider_pool.submit(self._search_provider, provider_id, track.title, deadline)] = provider_id
                for future in futures:
                    future.cancel()
                    errors.append(f"{futures[future]}：搜索超时")
                    progress(futures[future], "搜索超时")
                ranked = rank()
                with self.db.transaction():
                    track.candidate_count = len(ranked)
                    track.best_score = ranked[0].score if ranked else None
                    track.error = "；".join(dict.fromkeys(errors))[:300] or None
                    available = [c for c in ranked if c.verdict != "reject"]
                    if track.chosen_by == "user" and track.chosen_candidate_id in {c.id for c in ranked}:
                        track.status = "matched"
                    elif available and available[0].verdict == "match" and self.config.settings.auto_approve:
                        track.status, track.chosen_candidate_id, track.chosen_by = "matched", available[0].id, "auto"
                    else:
                        track.status = "needs_review" if available else "not_found"
                        track.chosen_candidate_id = track.chosen_by = None
                    self.save_track(track)
                if on_event:
                    on_event({"type": "done", "track": track.model_dump(), "candidates": [c.model_dump() for c in ranked]})
                return {"track": track, "candidates": ranked}
            except Exception as error:
                for future in futures:
                    future.cancel()
                track.status, track.error = "failed", public_error(error)
                self.save_track(track)
                raise

    def selected_providers(self, providers: list[str] | None) -> list[str]:
        return [p.id for p in sorted(self.config.providers, key=lambda p: p.order)
                if p.enabled and (not providers or p.id in providers)]

    def _artist_lookup(self, provider_id: str, query: str, deadline: float):
        try:
            provider = self.providers[provider_id]
            with provider.search_budget(deadline):
                return provider.artist_lookup(query)
        except Exception:
            return None

    def _artist_hot(self, provider_id: str, artist: dict, deadline: float):
        try:
            provider = self.providers[provider_id]
            with provider.search_budget(deadline):
                return provider.artist_songs(artist), None
        except Exception as error:
            return [], log_failure(provider_id, error)

    def _search_artist(self, artist, lookups, initial, on_event):
        infos = {p.id: p for p in self.config.providers}
        cover_url = None
        if artist.get("cover_url"):
            cover = RawCandidate(artist["provider"], artist["id"], artist["name"],
                                 extra={"cover_url": artist["cover_url"]})
            cover_url = f"/api/artist-songs/{self.artist_songs.put([cover])}/cover"
        if on_event:
            on_event({"type": "artist", "name": artist["name"], "aliases": artist["aliases"], "cover_url": cover_url})
        deadline = time.monotonic() + SEARCH_TIMEOUT
        futures = {self.provider_pool.submit(self._artist_hot, p, lookup, deadline): p for p, lookup in lookups.items()}
        results = {} if lookups else {p: raws for p, raws in initial.items() if p in TRUSTED}
        tokens = {}

        def songs():
            output = []
            for group in merge_songs(dict(sorted(results.items(), key=lambda item: infos[item[0]].order)), artist):
                identities = [(r.provider, r.source_id) for r in group]
                token = next((tokens[i] for i in identities if i in tokens), None)
                token = self.artist_songs.put(group, token)
                tokens.update(dict.fromkeys(identities, token))
                output.append(self.artist_songs.describe(group, token))
            return output

        merged = songs()
        if on_event and merged:
            on_event({"type": "artist_songs", "songs": merged})
        while futures and time.monotonic() < deadline:
            completed, _ = wait(futures, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
            for future in completed:
                provider_id = futures.pop(future)
                raws, error = future.result()
                if not error:
                    results[provider_id] = raws
                merged = songs()
                if on_event:
                    on_event({"type": "source", "provider": provider_id, "label": infos[provider_id].label,
                              "ok": error is None, "error": error})
                    on_event({"type": "artist_songs", "songs": merged})
        for future, provider_id in futures.items():
            future.cancel()
            if on_event:
                on_event({"type": "source", "provider": provider_id, "label": infos[provider_id].label,
                          "ok": False, "error": "搜索超时"})
        if on_event:
            on_event({"type": "artist_done", "songs": merged})
        return {"mode": "artist", "artist": {"name": artist["name"], "aliases": artist["aliases"]}, "songs": merged}

    def search_one(self, query: str, duration: float | None = None, providers: list[str] | None = None, *,
                   on_event: Callable[[dict], None] | None = None, song_only: bool = False,
                   initial_raws: list[RawCandidate] | None = None) -> dict:
        query = query.strip()
        enabled = self.selected_providers(providers)
        trusted = [p for p in enabled if p in TRUSTED]
        prepared = None
        if trusted and not song_only:
            infos = {p.id: p for p in self.config.providers}
            if on_event:
                on_event({"type": "start", "track_id": None,
                          "providers": [{"id": p, "label": infos[p].label} for p in enabled]})
            deadline = time.monotonic() + SEARCH_TIMEOUT
            futures = {self.provider_pool.submit(self._search_provider, p, query, deadline): p for p in enabled}
            lookup_deadline = time.monotonic() + .8
            lookups = {self.provider_pool.submit(self._artist_lookup, p, query, lookup_deadline): p for p in trusted}
            catalog = [f for f, p in futures.items() if p in TRUSTED]
            wait(catalog, timeout=max(0, lookup_deadline - time.monotonic()))
            wait(lookups, timeout=max(0, lookup_deadline - time.monotonic()))
            artists = {p: f.result() for f, p in lookups.items() if f.done() and f.result() is not None}
            for future in lookups:
                future.cancel()
            initial = {p: f.result()[0] for f, p in futures.items() if f.done()}
            raws = [r for rows in initial.values() for r in rows]
            artist = detect_artist(query, raws, list(artists.values()))
            pending = {f for f in catalog if not f.done()}
            while (artist is None and pending and not parse_query(query, raws)[1]
                   and not any(title_key(r.title) == title_key(query) for r in raws if r.provider in TRUSTED)
                   and time.monotonic() < deadline):
                completed, pending = wait(pending, timeout=max(0, deadline - time.monotonic()), return_when=FIRST_COMPLETED)
                initial = {p: f.result()[0] for f, p in futures.items() if f.done()}
                raws = [r for rows in initial.values() for r in rows]
                artist = detect_artist(query, raws, list(artists.values()))
            if artist:
                for future, provider_id in futures.items():
                    future.cancel()
                    if on_event and provider_id not in artists:
                        # Sources that don't feed the artist list are simply not needed, not failed.
                        failures = future.result()[1] if future.done() and not future.cancelled() else []
                        on_event({"type": "source", "provider": provider_id, "label": infos[provider_id].label,
                                  "ok": not failures, "error": failures[0] if failures else None,
                                  "skipped": not (future.done() and not future.cancelled())})
                return self._search_artist(artist, artists, initial, on_event)
            prepared = (futures, deadline)
        title, artist = parse_query(query)
        existing = next((p for p in self.playlists() if p.source == "search" and p.name == "单曲搜索"), None)
        playlist = existing or self.create_playlist("单曲搜索", "search", [])
        with self.db.transaction():
            tracks = self.tracks(playlist.id)
            track = self._add_track(playlist.id, ParsedRow(title=title, artist=artist, duration=duration), max((t.position for t in tracks), default=0) + 1)
        if initial_raws:
            self._rank_search(track, {(r.provider, r.source_id): r for r in initial_raws if r.provider in enabled})
        result = self.search_track(track.id, query=query, providers=enabled, infer_query=True,
                                   on_event=on_event, prepared=prepared)
        return {"mode": "song", **result}

    def download_artist_song(self, token: str, lyrics: bool | None = None, providers: list[str] | None = None) -> dict:
        raws = self.artist_songs.get(token)
        raw = raws[0]
        result = self.search_one(f"{raw.title} - {raw.artist}", raw.duration, providers, song_only=True, initial_raws=raws)
        track = result["track"]
        track.lyrics_wanted = lyrics
        self.save_track(track)
        job = self.jobs.start("download", track.playlist_id, [track.id]) if track.status == "matched" and track.chosen_by == "auto" else None
        return {"track": self.track(track.id), "job": job}

    def wants_lyrics(self, track: Track) -> bool:
        return self.config.settings.lyrics if track.lyrics_wanted is None else track.lyrics_wanted

    def download_track(self, track_id: int, *, in_job: bool = False) -> Track:
        with self.lock(track_id):
            track = self.track(track_id)
            if not in_job:
                self.ensure_idle(track)
            if track.status not in ("matched", "queued") or track.chosen_candidate_id is None:
                raise SongSeekError("请先选定候选再下载", "not_matched")
            track.status, track.error = "downloading", None
            self.save_track(track)
            candidates = self.candidates(track_id)
            chosen = self.candidate(track.chosen_candidate_id)
            choices = [chosen] + sorted((c for c in candidates if c.id != chosen.id and (c.verdict == "match" or same_recording(track, chosen, c))),
                                        key=lambda c: c.needs_member)
            errors = []
            for candidate in choices:
                try:
                    track.file = self.downloader.download(track, self.raw_candidate(candidate.id), self.providers[candidate.provider], self.playlist(track.playlist_id).name)
                    if track.chosen_candidate_id != candidate.id:
                        track.chosen_by = "auto"
                    track.chosen_candidate_id, track.status, track.error = candidate.id, "done", None
                    if self.wants_lyrics(track):
                        track.file.lyrics = self.lyrics.save(track)
                    return self.save_track(track)
                except Exception as error:
                    errors.append(f"{candidate.provider_label}：{log_failure(candidate.provider, error)}")
            summary = (f"试了 {len(errors)} 个候选都失败 — " if len(errors) > 1 else "") + "；".join(dict.fromkeys(errors))
            track.status, track.error = "failed", summary[:300]
            return self.save_track(track)

    def lyrics_track(self, track_id: int, *, force: bool = False, in_job: bool = False) -> Track:
        with self.lock(track_id):
            track = self.track(track_id)
            if not in_job:
                self.ensure_idle(track)
            if track.status != "done" or track.file is None:
                raise SongSeekError("请先下载曲目再获取歌词", "not_downloaded")
            if (in_job and not self.wants_lyrics(track)) or (not force and has_lyrics_file(track)):
                return track
            track.file.lyrics = self.lyrics.save(track)
            return self.save_track(track)

    def batch(self, ids: list[int], action: str) -> list[Track]:
        ids = list(dict.fromkeys(ids))
        for identifier in ids:
            self.ensure_idle(self.track(identifier))
        retry: dict[int, list[int]] = {}
        for identifier in ids:
            with self.lock(identifier):
                track = self.track(identifier)
                if action == "accept_best":
                    candidates = [c for c in self.candidates(identifier) if c.verdict != "reject"]
                    if candidates:
                        self.choose(identifier, candidates[0].id)
                elif action == "skip":
                    track.status = "skipped"
                    self.save_track(track)
                elif action == "unskip" and track.status == "skipped":
                    candidates = self.candidates(identifier)
                    track.status = "done" if track.file else "matched" if track.chosen_candidate_id else "needs_review" if any(c.verdict != "reject" for c in candidates) else "not_found" if candidates else "pending"
                    self.save_track(track)
                elif action == "retry" and track.status == "failed" and track.chosen_candidate_id:
                    track.status, track.error = "matched", None
                    self.save_track(track)
                    retry.setdefault(track.playlist_id, []).append(identifier)
        for playlist_id, track_ids in retry.items():
            self.jobs.start("download", playlist_id, track_ids)
        return [self.track(identifier) for identifier in ids]

    def health(self):
        enabled = [p for p in self.config.providers if p.enabled]
        futures = {self.provider_pool.submit(self.providers[p.id].health): p for p in enabled}
        completed, pending = wait(futures, timeout=10)
        for future in completed:
            futures[future].health = future.result()
        for future in pending:
            future.cancel()
            futures[future].health = Health(ok=False, latency_ms=None, message="体检超时（10 秒）", checked_at=now())
        return sorted(self.config.providers, key=lambda p: p.order)

    def export_csv(self, playlist_id: int) -> str:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["序号", "歌名", "歌手", "状态", "来源", "音质", "时长", "文件路径", "备注", "歌词"])
        def cell(value):
            text = str(value) if value is not None else ""
            return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
        for track in self.tracks(playlist_id):
            candidate = self.candidate(track.chosen_candidate_id) if track.chosen_candidate_id else None
            writer.writerow([cell(v) for v in [track.position, track.title, track.artist, track.status, candidate.provider_label if candidate else "",
                            f"{track.file.bitrate}k" if track.file and track.file.bitrate else candidate.quality.label if candidate else "",
                            track.file.duration if track.file else track.duration, track.file.path if track.file else "", track.note or track.error or "",
                            track.file.lyrics.path if track.file and track.file.lyrics else ""]])
        return "\ufeff" + stream.getvalue()

    def close(self):
        self.jobs.close()
        self.provider_pool.shutdown(wait=True, cancel_futures=True)
        self.events.close()
        self.db.close()
