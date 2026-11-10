from __future__ import annotations

import threading
from contextlib import ExitStack
from functools import partial
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .errors import SongSeekError, public_error
from .lyrics import has_lyrics_file
from .models import Job, LyricsInfo, now


class JobManager:
    def __init__(self, service):
        self.service = service
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="songseek-job")
        self.lock = threading.RLock()
        self.cancellations: dict[int, threading.Event] = {}
        self.reserved: set[int] = set()
        self.pending: dict[int, set[int]] = {}
        self.futures = {}
        self.capacity = threading.Condition()
        self.active = {"search": 0, "download": 0}

    def active_playlist(self, identifier: int) -> bool:
        return any(j.playlist_id == identifier and j.status == "running" for j in self.list())

    def track_active(self, identifier: int) -> bool:
        with self.lock:
            return identifier in self.reserved

    def get(self, identifier: int) -> Job:
        return Job(**self.service.db.get("jobs", identifier))

    def list(self) -> list[Job]:
        jobs = [Job(**data) for data in reversed(self.service.db.list("jobs"))]
        return [j for j in jobs if j.status == "running"] + [j for j in jobs if j.status != "running"][:20]

    def save(self, job: Job):
        self.service.db.save("jobs", job.model_dump())
        self.service.events.emit("job", job.model_dump())

    def start(self, kind: str, playlist_id: int, track_ids: list[int] | None = None, *, force: bool = False, lyrics: bool | None = None, set_lyrics: bool = False,
              providers: list[str] | None = None, redownload: bool = False) -> Job:
        tracks = self.service.tracks(playlist_id)
        by_id = {t.id: t for t in tracks}
        if track_ids is not None and any(i not in by_id for i in track_ids):
            raise SongSeekError("部分曲目不属于该清单，请刷新清单后重试", "invalid_tracks")

        def eligible(track):
            if kind == "lyrics":
                return (track.status == "done" and track.file and not track.file_missing and (force or not has_lyrics_file(track))
                        and self.service.wants_lyrics(track))
            if kind == "download":
                return (track.file_missing if redownload else track.status == "matched") and track.chosen_candidate_id is not None
            return track.status in ("pending", "not_found") if track_ids is None else track.status not in ("searching", "queued", "downloading", "done", "skipped")

        ready = threading.Event()
        with ExitStack() as locks:
            selected = []
            for track in sorted(tracks, key=lambda t: t.id):
                if (track_ids is None or track.id in track_ids) and eligible(track):
                    locks.enter_context(self.service.lock(track.id))
                    current = self.service.track(track.id)
                    if eligible(current):
                        selected.append(current)
            # 曲目锁总是在任务锁之前取得，与同步编辑、选择和搜索保持一致。
            with self.lock:
                if any(t.id in self.reserved for t in selected):
                    raise SongSeekError("曲目已在任务中，请等待当前任务完成后重试", "track_busy", 409)
                if redownload and track_ids is not None and len(selected) != len(set(track_ids)):
                    raise SongSeekError("部分歌曲状态已变化，请刷新清单后重试", "track_busy", 409)
                if not selected:
                    reasons = {"search": "所选歌曲都已搜索、已下载、已跳过或正在处理，请选择待搜索歌曲后重试",
                               "download": "所选歌曲都已下载、正在处理或未选定版本，请先选定待下载版本",
                               "lyrics": "所选歌曲未下载、已有歌词或未开启歌词，请下载歌曲、开启歌词或使用强制补歌词"}
                    raise SongSeekError(f"没有可处理的歌曲：{reasons[kind]}", "no_eligible_tracks", 409)
                ids = [t.id for t in selected]
                destination = self.service.downloader.destination(self.service.playlist(playlist_id).name) if kind == "download" else None
                job = Job(id=0, kind=kind, playlist_id=playlist_id, status="running", total=len(ids), started_at=now())
                with self.service.db.transaction():
                    job.id = self.service.db.insert("jobs", job.model_dump())
                    if kind == "download":
                        for track in selected:
                            if redownload and track.file_missing:
                                track.file, track.error = None, None
                            if set_lyrics:
                                track.lyrics_wanted = lyrics
                            track.status = "queued"
                            self.service.save_track(track)
                self.reserved.update(ids)
                self.pending[job.id] = set(ids)
                self.cancellations[job.id] = threading.Event()
                self.save(job)
                self.futures[job.id] = self.pool.submit(self._run, job.id, ids, force, destination, providers, ready)
        ready.set()
        return job

    def _execute(self, kind, cancel, operation, track_id):
        capacity_kind = "download" if kind == "lyrics" else kind
        with self.capacity:
            while self.active[capacity_kind] >= getattr(self.service.config.settings, f"{capacity_kind}_concurrency"):
                if cancel.is_set():
                    return False
                self.capacity.wait(.1)
            if cancel.is_set():
                return False
            self.active[capacity_kind] += 1
        try:
            with self.service.lock(track_id):
                try:
                    operation(track_id, in_job=True)
                except SongSeekError as error:
                    if error.code == "track_busy":
                        return False
                    self._track_failure(kind, track_id, error)
                except Exception as error:
                    self._track_failure(kind, track_id, error)
            return True
        except SongSeekError as error:
            if error.code == "track_busy":
                return False
            raise
        finally:
            with self.capacity:
                self.active[capacity_kind] -= 1
                self.capacity.notify_all()

    def _track_failure(self, kind, track_id, error):
        track = self.service.track(track_id)
        if kind == "lyrics":
            if has_lyrics_file(track):
                track.file.lyrics.refresh_error = "歌词获取或保存失败，请稍后重试"
            else:
                track.file.lyrics = LyricsInfo(source=track.file.provider or "", source_label=track.file.provider_label or "",
                                              error="歌词获取或保存失败，请稍后重试")
        else:
            track.status, track.error = "done" if track.file else "failed", public_error(error)
        self.service.save_track(track)

    def _release(self, identifier: int, track_id: int):
        try:
            with self.service.lock(track_id):
                track = self.service.track(track_id)
                with self.lock:
                    self.reserved.discard(track_id)
                    self.pending[identifier].discard(track_id)
                if track.status == "queued":
                    track.status, track.error = "matched", None
                    self.service.save_track(track)
        except SongSeekError as error:
            if error.code != "track_busy":
                raise
            with self.lock:
                self.reserved.discard(track_id)
                self.pending[identifier].discard(track_id)

    def _run(self, identifier: int, ids: list[int], force: bool, destination, providers, ready):
        ready.wait()
        job = self.get(identifier)
        cancel = self.cancellations[identifier]
        concurrency = self.service.config.settings.search_concurrency if job.kind == "search" else self.service.config.settings.download_concurrency
        remaining = iter(ids)
        active = {}
        operation = self.service.search_track if job.kind == "search" else self.service.download_track
        if job.kind == "download":
            operation = partial(operation, destination=destination, providers=providers)
        if job.kind == "lyrics":
            operation = partial(self.service.lyrics_track, force=force)
        try:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=f"songseek-{job.kind}") as pool:
                while True:
                    while len(active) < concurrency and not cancel.is_set():
                        track_id = next(remaining, None)
                        if track_id is None:
                            break
                        track = self.service.track(track_id)
                        job.current = f"{track.title} - {track.artist}".strip(" -")
                        active[pool.submit(self._execute, job.kind, cancel, operation, track_id)] = track_id
                    self.save(job)
                    if not active:
                        break
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        track_id = active.pop(future)
                        try:
                            if not future.result():
                                continue
                            track = self.service.track(track_id)
                            if (track.status in ("failed", "not_found") or (job.kind == "download" and track.error)
                                    or (job.kind == "lyrics" and track.file.lyrics and (track.file.lyrics.error or track.file.lyrics.refresh_error))):
                                job.failed += 1
                        except Exception:
                            job.failed += 1
                        finally:
                            self._release(identifier, track_id)
                        job.completed += 1
            job.status = "cancelled" if cancel.is_set() else "done"
        except Exception:
            job.status = "error"
        finally:
            for track_id in list(self.pending[identifier]):
                self._release(identifier, track_id)
            with self.lock:
                job.current, job.finished_at = None, now()
                self.save(job)

    def cancel(self, identifier: int) -> Job:
        with self.lock:
            job = self.get(identifier)
            if job.status != "running":
                return job
            self.cancellations[identifier].set()
            cancelled = self.futures[identifier].cancel()
        if cancelled:
            for track_id in list(self.pending[identifier]):
                self._release(identifier, track_id)
            job.status, job.current, job.finished_at = "cancelled", None, now()
            self.save(job)
        return job

    def wait(self, identifier: int) -> Job:
        future = self.futures[identifier]
        if not future.cancelled():
            future.result()
        return self.get(identifier)

    def close(self):
        for event in self.cancellations.values():
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=False)
