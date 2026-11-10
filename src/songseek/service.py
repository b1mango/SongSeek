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
from .downloader import DownloadDestination, Downloader, PreviewCache, digest
from .errors import SongSeekError, log_failure, public_error
from .events import Broadcaster
from .importers import parse_file, parse_text
from .matching import evaluate, evaluate_candidates, same_recording, sort_candidates
from .lyrics import LyricsManager, has_lyrics_file
from .models import Candidate, ChosenCandidate, Health, LyricsInfo, ParsedRow, Playlist, RawCandidate, STATUSES, Track, now
from .normalize import normalized, title_key, tolerance, version_flags
from .providers import create_providers
from .query import TRUSTED, detect_artist, parse_query
from .providers.netease import NeteaseProvider

BUSY = {"searching", "queued", "downloading"}
DERIVED = {"file_missing"}  # computed from disk on read, never stored
SEARCH_TIMEOUT = 9.5


class Service:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.db = Database(self.config.home / "songseek.db")
        self.events = Broadcaster()
        self.providers = create_providers(self.config)
        self.provider_pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="songseek-provider")
        self.downloader = Downloader(self.config)
        self.downloader.cleanup_partials()
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

    def _recover_published(self, track: Track) -> bool:
        publication = self.db.publication(track.id)
        if not publication:
            return False
        try:
            chosen = self.candidate(track.chosen_candidate_id)
            candidate = self.candidate(publication["candidate_id"])
            if candidate.track_id != track.id or (candidate.id != chosen.id and not same_recording(chosen, candidate)):
                return False
            path = Path(publication["path"])
            if path.is_symlink() or not path.is_file() or digest(path) != publication["sha256"]:
                return False
            file = self.downloader.recovered_file(path, self.raw_candidate(candidate.id))
        except (OSError, SongSeekError):
            return False
        expected = track.duration if track.duration is not None else candidate.duration
        if expected is not None and abs(file.duration - expected) > tolerance(expected):
            return False
        if track.chosen_candidate_id != candidate.id:
            track.chosen_by = "auto"
        track.chosen_candidate_id, track.status, track.error, track.file = candidate.id, "done", None, file
        file.candidate_id, file.chosen_by = candidate.id, track.chosen_by
        return True

    def _recover(self):
        for data in self.db.list("tracks"):
            track = Track(**data)
            interrupted = track.status in BUSY
            recovered = False
            if track.status == "downloading" and track.chosen_candidate_id is not None:
                recovered = self._recover_published(track)
            if interrupted and not recovered:
                # A version change may have been interrupted before replacing the original file.
                # Keep the displayed choice consistent with the file that actually remains.
                if track.file and track.file.candidate_id is not None:
                    track.chosen_candidate_id, track.chosen_by = track.file.candidate_id, track.file.chosen_by
                track.status = "done" if track.file else "matched" if track.chosen_candidate_id else "needs_review" if track.candidate_count else "pending"
                track.error = "上次运行中断，可重新执行"
            if interrupted or recovered:
                track.updated_at = now()
            changed = self._sync_chosen(track)  # also backfills tracks saved before `chosen` existed
            if changed or interrupted:
                with self.db.transaction():
                    self.db.save("tracks", track.model_dump(exclude=DERIVED))
                    if interrupted:
                        self.db.clear_publication(track.id)
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
        counts.update(self.db.playlist_counts(identifier))
        counts["total"] = sum(counts.values())
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

    def _sync_chosen(self, track: Track) -> bool:
        """Refresh the snapshot of the chosen candidate shown in lists; True when it changed."""
        before = track.chosen
        track.chosen = None
        if track.chosen_candidate_id is not None:
            try:
                c = self.candidate(track.chosen_candidate_id)
                track.chosen = ChosenCandidate(id=c.id, provider_label=c.provider_label, title=c.title, artist=c.artist, album=c.album,
                                               duration=c.duration, quality=c.quality.label, score=c.score)
            except SongSeekError:
                pass
        return track.chosen != before

    def save_track(self, track: Track) -> Track:
        with self.db.transaction():
            previous_status = self.db.track_status(track.id)
            self._sync_chosen(track)
            track.updated_at = now()
            self.db.save("tracks", track.model_dump(exclude=DERIVED))
        self.events.emit("track", track.model_dump())
        if previous_status != track.status:
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

    def add_tracks(self, playlist_id: int, rows: list[ParsedRow]) -> dict:
        """Skip matching titles and artists when both durations are missing or within two seconds."""
        def same(a, b) -> bool:
            return (normalized(a.title) == normalized(b.title) and normalized(a.artist) == normalized(b.artist)
                    and ((a.duration is None and b.duration is None)
                         or (a.duration is not None and b.duration is not None and abs(a.duration - b.duration) <= 2)))
        with self.db.transaction():
            existing = self.tracks(playlist_id)
            position = max((t.position for t in existing), default=0)
            kept: list[ParsedRow] = []
            for row in rows:
                if not any(same(row, t) for t in existing) and not any(same(row, k) for k in kept):
                    kept.append(row)
            added = [self._add_track(playlist_id, row, position + index) for index, row in enumerate(kept, 1)]
        if added:
            self._playlist_event(playlist_id)
        return {"added": added, "duplicates": len(rows) - len(kept)}

    def _add_track(self, playlist_id: int, row: ParsedRow, position: int) -> Track:
        track = Track(**row.model_dump(), id=0, playlist_id=playlist_id, position=position, flags=version_flags(row.title), status="pending", updated_at=now())
        track.id = self.db.insert("tracks", track.model_dump(exclude=DERIVED), playlist_id=playlist_id)
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
        tracks = self.tracks(identifier)
        if any(t.status in BUSY for t in tracks) or self.jobs.active_playlist(identifier):
            raise SongSeekError("清单仍在执行任务，请取消并等待当前曲目完成", "track_busy", 409)
        self.db.delete("playlists", identifier)
        self.events.delete_playlist(identifier, [track.id for track in tracks])

    def delete_track(self, identifier: int):
        with self.lock(identifier):
            track = self.track(identifier)
            self.ensure_idle(track)
            self.db.delete("tracks", identifier)  # candidates go with it (ON DELETE CASCADE)
            self.events.delete_track(identifier, track.playlist_id)
            self._playlist_event(track.playlist_id)

    def ensure_idle(self, track: Track):
        if track.status in BUSY or self.jobs.track_active(track.id):
            raise SongSeekError("曲目正在执行任务，请稍后重试", "track_busy", 409)

    def update_track(self, identifier: int, values: dict) -> Track:
        with self.lock(identifier):
            track = self.track(identifier)
            self.ensure_idle(track)
            changed = any(k in ("title", "artist", "duration") and getattr(track, k) != v for k, v in values.items())
            data = track.model_dump() | values
            with self.db.transaction() as connection:
                if changed:
                    connection.execute("DELETE FROM candidates WHERE track_id=?", (identifier,))
                    data.update(flags=version_flags(data["title"]), status="done" if track.file else "pending", chosen_candidate_id=None, chosen_by=None,
                                candidate_count=0, best_score=None, error=None)
                    if data["file"]:
                        data["file"].update(candidate_id=None, chosen_by=None)
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
            if track.chosen_candidate_id == candidate_id and track.status != "failed":  # picking a failed one again means "try it again"
                return track
            if candidate.provider not in self.selected_providers(track.providers):
                raise SongSeekError("该候选音源未启用或不在本次范围内，请启用该音源并重新搜索后选择", "unknown_provider")
            if track.file and track.status == "done" and track.file.candidate_id is None:
                track.file.candidate_id, track.file.chosen_by = track.chosen_candidate_id, track.chosen_by
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
        enabled = self.selected_providers(track.providers)
        labels = {p.id: p.label for p in self.config.providers}
        ranked = evaluate_candidates(track, [raw for raw in raws.values() if raw.provider in enabled], labels,
                                     netease_cookie_set=self.config.settings.netease_cookie_set)
        ranked.extend(evaluate(track, raw, labels[raw.provider], netease_cookie_set=self.config.settings.netease_cookie_set)
                      for raw in raws.values() if raw.provider not in enabled)
        ranked = sort_candidates(ranked, {p.id: p.order for p in self.config.providers})
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
            track.providers = enabled if providers is not None else None
            track.status, track.error, track.search_failed = "searching", None, False
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
                fallback, answered = set(), set()
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
                        if not failures:
                            answered.add(provider_id)
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
                    current_providers = self.selected_providers(track.providers)
                    available = [c for c in ranked if c.verdict != "reject" and c.provider in current_providers]
                    if track.file:
                        track.status = "done"  # new candidates are only alternatives; the downloaded file stays current
                    elif track.chosen_by == "user" and track.chosen_candidate_id in {c.id for c in ranked if c.provider in current_providers}:
                        track.status = "matched"
                    elif available and available[0].verdict == "match" and self.config.settings.auto_approve:
                        track.status, track.chosen_candidate_id, track.chosen_by = "matched", available[0].id, "auto"
                    else:
                        track.status = "needs_review" if available else "not_found"
                        track.chosen_candidate_id = track.chosen_by = None
                    # Only signal a source failure when this search received no successful response and found no usable result.
                    track.search_failed = track.status == "not_found" and bool(errors) and not answered
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
                if p.enabled and (providers is None or p.id in providers)]

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
        providers = providers or None
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
            # Searching the same song again reuses its row instead of piling up duplicates in 单曲搜索.
            same = [t for t in tracks if t.status not in BUSY and normalized(t.title) == normalized(title) and normalized(t.artist) == normalized(artist)
                    and (duration is None or t.duration is None or abs(t.duration - duration) <= 2)]
            track = next((t for t in same if t.file), None) or (same[-1] if same else None)
            if track is None:
                track = self._add_track(playlist.id, ParsedRow(title=title, artist=artist, duration=duration), max((t.position for t in tracks), default=0) + 1)
                self._playlist_event(playlist.id)
            elif duration is not None and track.duration is None:
                track.duration = duration
                self.save_track(track)
        if initial_raws:
            track.providers = enabled if providers is not None else None
            self._rank_search(track, {(r.provider, r.source_id): r for r in initial_raws if r.provider in enabled})
        result = self.search_track(track.id, query=query, providers=enabled if providers is not None else None, infer_query=True,
                                   on_event=on_event, prepared=prepared)
        return {"mode": "song", **result}

    def download_artist_song(self, token: str, lyrics: bool | None = None, providers: list[str] | None = None) -> dict:
        providers = providers or None
        raws = self.artist_songs.get(token)
        raw = raws[0]
        result = self.search_one(f"{raw.title} - {raw.artist}", raw.duration, providers, song_only=True, initial_raws=raws)
        track = result["track"]
        track.lyrics_wanted = lyrics
        self.save_track(track)
        job = self.jobs.start("download", track.playlist_id, [track.id], providers=providers) if track.status == "matched" and track.chosen_by == "auto" else None
        return {"track": self.track(track.id), "job": job}

    def wants_lyrics(self, track: Track) -> bool:
        return self.config.settings.lyrics if track.lyrics_wanted is None else track.lyrics_wanted

    def download_track(self, track_id: int, *, in_job: bool = False, providers: list[str] | None = None,
                       destination: DownloadDestination | None = None) -> Track:
        with self.lock(track_id):
            track = self.track(track_id)
            if not in_job:
                self.ensure_idle(track)
            if track.status not in ("matched", "queued") or track.chosen_candidate_id is None:
                raise SongSeekError("请先选定候选再下载", "not_matched")
            # A file that vanished from disk is nothing to fall back to if the new download fails.
            original_file = track.file if track.file and Path(track.file.path).is_file() else None
            original_choice = (original_file.candidate_id, original_file.chosen_by) if original_file else (track.chosen_candidate_id, track.chosen_by)
            track.status, track.error = "downloading", None
            with self.db.transaction():
                self.db.clear_publication(track.id)
                self.save_track(track)
            candidates = self.candidates(track_id)
            chosen = self.candidate(track.chosen_candidate_id)
            enabled = self.selected_providers(track.providers)
            if providers is not None:
                enabled = [p for p in enabled if p in providers]
            choices = ([chosen] if chosen.provider in enabled else []) + sorted(
                (c for c in candidates if c.provider in enabled and c.id != chosen.id and same_recording(chosen, c)), key=lambda c: c.needs_member)
            errors = []
            playlist_name = self.playlist(track.playlist_id).name
            last_progress = None
            def progress(phase: str, downloaded: int, total: int | None):
                nonlocal last_progress
                current = time.monotonic()
                complete = total is not None and downloaded >= total
                if phase == "download":
                    if not complete and last_progress is not None and current - last_progress < .2:
                        return
                    last_progress = current
                fraction = None if total is None else round(min(1, max(0, downloaded / total)), 3) if total else 1
                self.events.emit("track_progress", {"track_id": track.id, "playlist_id": track.playlist_id,
                                                    "phase": phase, "downloaded": downloaded, "total": total,
                                                    "fraction": fraction})
            for candidate in choices:
                if candidate.provider not in self.selected_providers(track.providers):
                    continue
                try:
                    options = {"destination": destination} if destination is not None else {}
                    options["on_publish"] = lambda path, sha: self.db.prepare_publication(track.id, candidate.id, str(path.resolve()), sha)
                    file = self.downloader.download(track, self.raw_candidate(candidate.id), self.providers[candidate.provider], playlist_name,
                                                    on_progress=progress, **options)
                except Exception as error:
                    errors.append(f"{candidate.provider_label}：{log_failure(candidate.provider, error)}")
                    if isinstance(error, SongSeekError) and error.code == "storage_error":
                        break
                    continue
                track.file = file
                if track.chosen_candidate_id != candidate.id:
                    track.chosen_by = "auto"
                track.chosen_candidate_id, track.status, track.error = candidate.id, "done", None
                track.file.candidate_id, track.file.chosen_by = candidate.id, track.chosen_by
                with self.db.transaction():
                    self.save_track(track)
                    self.db.clear_publication(track.id)
                if self.wants_lyrics(track):
                    self._save_lyrics(track)
                    self.save_track(track)
                return track
            summary = (f"试了 {len(errors)} 个候选都失败 — " if len(errors) > 1 else "") + "；".join(dict.fromkeys(errors))
            if not errors:
                summary = "所选音源未启用或不在本次范围内，请启用音源或重新搜索后选择版本再下载"
            track.status, track.error, track.file = "failed", summary[:300], None
            if original_file:
                track.status, track.file = "done", original_file
                track.chosen_candidate_id, track.chosen_by = original_choice
                track.error = f"换版本下载失败，已保留原文件：{summary}"[:300]
            with self.db.transaction():
                self.db.clear_publication(track.id)
                return self.save_track(track)

    def _save_lyrics(self, track: Track):
        previous = track.file.lyrics if has_lyrics_file(track) else None
        try:
            info = self.lyrics.save(track)
        except Exception:
            info = LyricsInfo(source=track.file.provider or "", source_label=track.file.provider_label or "",
                              error="歌词获取或保存失败，请稍后重试")
        if previous and (info.error or not info.path):
            previous.refresh_error = info.error or "歌词更新失败，请稍后重试"
            track.file.lyrics = previous
        else:
            track.file.lyrics = info

    def lyrics_track(self, track_id: int, *, force: bool = False, in_job: bool = False) -> Track:
        with self.lock(track_id):
            track = self.track(track_id)
            if not in_job:
                self.ensure_idle(track)
            if track.status != "done" or track.file is None:
                raise SongSeekError("请先下载曲目再获取歌词", "not_downloaded")
            if (in_job and not self.wants_lyrics(track)) or (not force and has_lyrics_file(track)):
                return track
            self._save_lyrics(track)
            return self.save_track(track)

    def batch(self, ids: list[int], action: str) -> dict:
        ids = list(dict.fromkeys(ids))
        retry: dict[int, list[int]] = {}  # tracks to queue for download again, per playlist
        redownload: dict[int, list[int]] = {}
        updated, skipped = [], []
        for identifier in ids:
            try:
                with self.lock(identifier):
                    track = self.track(identifier)
                    self.ensure_idle(track)
                    before = track.model_dump(exclude={"updated_at"})
                    reason = None
                    if action == "accept_best":
                        enabled = self.selected_providers(track.providers)
                        candidates = [c for c in self.candidates(identifier) if c.verdict != "reject" and c.provider in enabled]
                        if track.status != "needs_review":
                            reason = "歌曲不在待审核状态，请选择待审核歌曲"
                        elif not candidates:
                            reason = "当前音源范围没有可用候选，请重新搜索后审核"
                        else:
                            track = self.choose(identifier, candidates[0].id)
                    elif action == "skip":
                        if track.file:
                            reason = "歌曲已有下载文件，保留下载记录"
                        elif track.status == "skipped":
                            reason = "歌曲已跳过，无需重复操作"
                        else:
                            track.status = "skipped"
                            self.save_track(track)
                    elif action == "unskip":
                        if track.status != "skipped":
                            reason = "歌曲未跳过，无需恢复"
                        else:
                            enabled = self.selected_providers(track.providers)
                            candidates = [c for c in self.candidates(identifier) if c.provider in enabled]
                            chosen = any(c.id == track.chosen_candidate_id for c in candidates)
                            track.status = "done" if track.file else "matched" if chosen else "needs_review" if any(c.verdict != "reject" for c in candidates) else "not_found" if candidates else "pending"
                            track.error = None
                            self.save_track(track)
                    elif action == "retry":
                        if track.status != "failed" or not track.chosen_candidate_id:
                            reason = "歌曲没有可重试的下载，请先选定版本"
                        else:
                            track.status, track.error = "matched", None
                            self.save_track(track)
                            retry.setdefault(track.playlist_id, []).append(identifier)
                    elif action == "redownload":
                        if not track.file_missing:
                            reason = "文件仍在原位置，无需重新下载" if track.file else "歌曲还没有下载过，请先下载"
                        elif not track.chosen_candidate_id:
                            reason = "歌曲没有已选定的版本，请重新搜索"
                        else:
                            # Keep the missing-file record until the job is reserved successfully.
                            redownload.setdefault(track.playlist_id, []).append(identifier)
                    if reason is None and action != "redownload" and track.model_dump(exclude={"updated_at"}) == before:
                        reason = "歌曲无需更改，请刷新清单后查看当前状态"
                    if reason:
                        skipped.append({"id": identifier, "reason": reason})
                    elif action != "redownload":
                        updated.append(identifier)
            except SongSeekError as error:
                if error.code not in ("track_busy", "not_found"):
                    raise
                skipped.append({"id": identifier, "reason": "歌曲不存在，请刷新清单" if error.code == "not_found" else str(error)})
        for playlist_id, track_ids in retry.items():
            self.jobs.start("download", playlist_id, track_ids)
        for playlist_id, track_ids in redownload.items():
            try:
                self.jobs.start("download", playlist_id, track_ids, redownload=True)
                updated.extend(track_ids)
            except SongSeekError as error:
                skipped.extend({"id": identifier, "reason": str(error)} for identifier in track_ids)
        return {"updated": [self.track(identifier) for identifier in updated], "skipped": skipped}

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
