from __future__ import annotations

import threading
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

    def start(self, kind: str, playlist_id: int, track_ids: list[int] | None = None, *, force: bool = False) -> Job:
        with self.lock:
            tracks = self.service.tracks(playlist_id)
            by_id = {t.id: t for t in tracks}
            if track_ids is not None and any(i not in by_id for i in track_ids):
                raise SongSeekError("部分曲目不属于该清单", "invalid_tracks")
            allowed = ("pending", "not_found") if kind == "search" else ("matched",)
            selected = [t for t in tracks if (track_ids is None or t.id in track_ids) and (t.status in allowed if track_ids is None or kind == "download" else t.status not in ("searching", "queued", "downloading", "done", "skipped"))]
            if kind == "lyrics":
                selected = [t for t in tracks if t.status == "done" and t.file
                            and (track_ids is None or t.id in track_ids) and (force or not has_lyrics_file(t))
                            and self.service.config.settings.lyrics]
            if any(t.id in self.reserved for t in selected):
                raise SongSeekError("曲目已在任务中", "track_busy", 409)
            ids = [t.id for t in selected]
            self.reserved.update(ids)
            job = Job(id=0, kind=kind, playlist_id=playlist_id, status="running", total=len(ids), started_at=now())
            job.id = self.service.db.insert("jobs", job.model_dump())
            self.cancellations[job.id] = threading.Event()
            if kind == "download":
                for track in selected:
                    track.status = "queued"
                    self.service.save_track(track)
            self.save(job)
            self.futures[job.id] = self.pool.submit(self._run, job.id, ids, force)
            return job

    def _execute(self, kind, cancel, operation, track_id):
        kind = "download" if kind == "lyrics" else kind
        with self.capacity:
            while self.active[kind] >= getattr(self.service.config.settings, f"{kind}_concurrency"):
                if cancel.is_set():
                    return False
                self.capacity.wait(.1)
            if cancel.is_set():
                return False
            self.active[kind] += 1
        try:
            operation(track_id, in_job=True)
            return True
        finally:
            with self.capacity:
                self.active[kind] -= 1
                self.capacity.notify_all()

    def _run(self, identifier: int, ids: list[int], force: bool):
        job = self.get(identifier)
        cancel = self.cancellations[identifier]
        concurrency = self.service.config.settings.search_concurrency if job.kind == "search" else self.service.config.settings.download_concurrency
        remaining = iter(ids)
        active = {}
        operation = self.service.search_track if job.kind == "search" else self.service.download_track
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
                            if track.status in ("failed", "not_found") or (job.kind == "lyrics" and track.file.lyrics and track.file.lyrics.error):
                                job.failed += 1
                        except Exception as error:
                            track = self.service.track(track_id)
                            if job.kind == "lyrics":
                                track.file.lyrics = LyricsInfo(source=track.file.provider or "", source_label=track.file.provider_label or "",
                                                              error="歌词获取或保存失败")
                            else:
                                track.status, track.error = "failed", public_error(error)
                            self.service.save_track(track)
                            job.failed += 1
                        job.completed += 1
            job.status = "cancelled" if cancel.is_set() else "done"
        except Exception:
            job.status = "error"
        finally:
            for track_id in ids:
                track = self.service.track(track_id)
                if track.status == "queued":
                    track.status = "matched"
                    self.service.save_track(track)
            with self.lock:
                self.reserved.difference_update(ids)
                job.current, job.finished_at = None, now()
                self.save(job)

    def cancel(self, identifier: int) -> Job:
        with self.lock:
            job = self.get(identifier)
            if job.status == "running":
                self.cancellations[identifier].set()
            # 当前单曲允许完成，任务在收尾后才变为 cancelled。
            return job

    def wait(self, identifier: int) -> Job:
        self.futures[identifier].result()
        return self.get(identifier)

    def close(self):
        for event in self.cancellations.values():
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=False)
