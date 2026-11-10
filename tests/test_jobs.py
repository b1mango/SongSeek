import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from songseek.events import Broadcaster
from songseek.errors import SongSeekError
from songseek.models import DownloadedFile, Job, ParsedRow, now
from songseek.service import Service


def test_restart_recovers_tracks_and_jobs(service, config):
    playlist = service.create_playlist("恢复", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180), ParsedRow(title="后来")])
    first, second = service.tracks(playlist.id)
    first = service.search_track(first.id)["track"]
    first.status, second.status = "downloading", "searching"
    service.save_track(first)
    service.save_track(second)
    job = Job(id=0, kind="search", playlist_id=playlist.id, status="running", total=1, started_at=now())
    service.db.insert("jobs", job.model_dump())
    restarted = Service(config)
    try:
        assert restarted.track(first.id).status == "matched"
        assert restarted.track(second.id).status == "pending"
        assert restarted.jobs.list()[0].status == "error"
    finally:
        restarted.close()


def test_cancel_finishes_running_track_and_leaves_others(service, monkeypatch):
    service.config.update({"search_concurrency": 1})
    playlist = service.create_playlist("取消", "text", [ParsedRow(title=f"歌{i}") for i in range(3)])
    entered, release = threading.Event(), threading.Event()
    original = service.search_track
    calls = []
    def search(identifier, **kwargs):
        calls.append(identifier)
        entered.set()
        assert release.wait(3)
        return original(identifier, **kwargs)
    monkeypatch.setattr(service, "search_track", search)
    job = service.jobs.start("search", playlist.id)
    assert entered.wait(3)
    service.jobs.cancel(job.id)
    release.set()
    final = service.jobs.wait(job.id)
    assert final.status == "cancelled" and final.completed == 1
    assert len(calls) == 1
    assert [t.status for t in service.tracks(playlist.id)][1:] == ["pending", "pending"]


def test_search_queries_bounded_deduplicated_and_provider_errors(service, monkeypatch):
    calls = []
    provider = service.providers["mock"]
    original = provider.search
    def search(query, limit):
        calls.append(query)
        if len(calls) == 1:
            raise ValueError("upstream should not leak")
        return original(query, limit)
    monkeypatch.setattr(provider, "search", search)
    playlist = service.create_playlist("失败隔离", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    result = service.search_track(service.tracks(playlist.id)[0].id)
    assert calls == ["晴天 周杰伦"]
    assert not result["candidates"] and result["track"].error
    assert "should not leak" not in result["track"].error


def test_sse_broadcast_is_thread_safe_and_job_throttled():
    broadcaster = Broadcaster()
    listener = broadcaster.subscribe()
    threads = [threading.Thread(target=broadcaster.emit, args=("track", {"id": i})) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert listener.qsize() == 10
    while not listener.empty():
        listener.get()
    for i in range(20):
        broadcaster.emit("job", {"id": 1, "completed": i})
    assert listener.qsize() == 1
    time.sleep(.28)
    assert listener.qsize() == 2
    listener.get()
    assert '"completed": 19' in listener.get()
    broadcaster.unsubscribe(listener)
    broadcaster.close()


def test_track_and_playlist_events_coalesce_per_id():
    broadcaster = Broadcaster(interval=.05)
    listener = broadcaster.subscribe()
    for status in ("queued", "downloading", "done"):
        broadcaster.emit("track", {"id": 1, "status": status})
    broadcaster.emit("track", {"id": 2, "status": "queued"})
    broadcaster.emit("playlist", {"id": 1, "counts": {"done": 0}})
    broadcaster.emit("playlist", {"id": 1, "counts": {"done": 1}})
    broadcaster.emit("track_progress", {"track_id": 1})
    broadcaster.emit("track_progress", {"track_id": 1})
    first = [listener.get_nowait() for _ in range(listener.qsize())]
    assert len(first) == 5  # first of each (kind, id) at once; progress is never held back
    time.sleep(.1)
    later = [listener.get_nowait() for _ in range(listener.qsize())]
    assert len(later) == 2
    assert any('"status": "done"' in event for event in later) and any('"done": 1' in event for event in later)
    broadcaster.emit("track", {"id": 3})
    broadcaster.emit("track", {"id": 3})
    broadcaster.close()
    time.sleep(.08)
    assert listener.qsize() == 1  # the held event is dropped on close, no timer fires afterwards


def test_concurrency_limit_shared_by_jobs(service, monkeypatch):
    service.config.update({"search_concurrency": 1})
    first = service.create_playlist("一", "text", [ParsedRow(title="一")])
    second = service.create_playlist("二", "text", [ParsedRow(title="二")])
    guard = threading.Lock()
    count, maximum = 0, 0
    def search(identifier, **kwargs):
        nonlocal count, maximum
        with guard:
            count += 1
            maximum = max(maximum, count)
        time.sleep(.04)
        with guard:
            count -= 1
    monkeypatch.setattr(service, "search_track", search)
    jobs = [service.jobs.start("search", p.id) for p in (first, second)]
    for job in jobs:
        assert service.jobs.wait(job.id).status == "done"
    assert maximum == 1


def test_nested_transactions_rollback_as_one_unit(service):
    import pytest
    with pytest.raises(RuntimeError), service.db.transaction():
        service.create_playlist("回滚", "text", [ParsedRow(title="不应保留")])
        raise RuntimeError("rollback")
    assert service.playlists() == []


def test_repeated_search_errors_are_deduplicated(service, monkeypatch):
    from songseek.errors import SongSeekError
    def search(*args):
        raise SongSeekError("上游接口返回 HTTP 500")
    monkeypatch.setattr(service.providers["mock"], "search", search)
    result = service.search_one("晴天 - 周杰伦")
    assert result["track"].error == "mock：上游接口返回 HTTP 500"


@pytest.mark.parametrize("status", ["searching", "queued", "downloading"])
def test_restart_recovers_interrupted_downloaded_track_as_done(service, config, status):
    track = service.search_one("晴天 - 周杰伦", 180)["track"]
    track.status = status
    track.file = DownloadedFile(path="/tmp/existing.mp3", size=1, format="mp3", bitrate=128, duration=180, sha256="old")
    service.save_track(track)
    restarted = Service(config)
    try:
        recovered = restarted.track(track.id)
        assert recovered.status == "done" and recovered.file == track.file
        assert "中断" in recovered.error
    finally:
        restarted.close()


@pytest.mark.parametrize("status", ["queued", "downloading"])
def test_restart_restores_original_choice_after_interrupted_version_change(service, config, tmp_path, status):
    track = service.search_one("晴天 - 周杰伦", 180)["track"]
    original_id, original_by = track.chosen_candidate_id, track.chosen_by
    original = tmp_path / "original.mp3"
    original.write_bytes(b"original audio")
    track.status = "done"
    track.file = DownloadedFile(path=str(original), size=original.stat().st_size, format="mp3", bitrate=128,
                                duration=180, sha256="old", candidate_id=original_id, chosen_by=original_by)
    service.save_track(track)
    alternate = next(candidate for candidate in service.candidates(track.id) if candidate.id != original_id)
    changed = service.choose(track.id, alternate.id)
    changed.status = status
    service.save_track(changed)

    restarted = Service(config)
    try:
        recovered = restarted.track(track.id)
        assert recovered.status == "done" and recovered.file.path == str(original)
        assert (recovered.chosen_candidate_id, recovered.chosen_by) == (original_id, original_by)
        assert recovered.chosen.id == original_id
        assert "中断" in recovered.error and original.read_bytes() == b"original audio"
    finally:
        restarted.close()


def test_startup_cleans_only_songseek_partials_to_one_playlist_level(config):
    output = Path(config.settings.output_dir)
    nested = output / "清单" / "更深目录"
    nested.mkdir(parents=True)
    removed = [output / ".songseek-root.part", output / "清单" / ".songseek-a.remux.part"]
    retained = [output / "other.part", output / ".songseek-keep.mp3", output / "song.mp3",
                nested / ".songseek-deep.part"]
    for path in removed + retained:
        path.write_text("content")
    (output / ".songseek-directory.part").mkdir()
    started = Service(config)
    try:
        assert all(not path.exists() for path in removed)
        assert all(path.read_text() == "content" for path in retained)
        assert (output / ".songseek-directory.part").is_dir()
    finally:
        started.close()


def test_startup_partial_cleanup_does_not_block_changing_inaccessible_directory(config, monkeypatch):
    output = Path(config.settings.output_dir)
    output.mkdir()
    original = Path.iterdir
    def inaccessible(path):
        if path == output:
            raise PermissionError("private details")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", inaccessible)
    started = Service(config)
    try:
        started.config.update({"output_dir": str(output.parent / "writable")})
        assert started.config.settings.output_dir.endswith("writable")
    finally:
        started.close()


def test_finished_track_released_while_next_track_is_running(service, monkeypatch):
    service.config.settings.search_concurrency = 1
    playlist = service.create_playlist("逐首释放", "text", [ParsedRow(title="第一首"), ParsedRow(title="第二首")])
    first, second = service.tracks(playlist.id)
    entered, release = threading.Event(), threading.Event()
    original = service.search_track
    def search(identifier, **kwargs):
        if identifier == second.id:
            entered.set()
            assert release.wait(5)
        return original(identifier, **kwargs)
    monkeypatch.setattr(service, "search_track", search)
    job = service.jobs.start("search", playlist.id)
    try:
        assert entered.wait(5)
        assert not service.jobs.track_active(first.id)
        assert service.jobs.track_active(second.id)
        service.update_track(first.id, {"note": "任务未结束也可编辑"})
        service.delete_track(first.id)
    finally:
        release.set()
    assert service.jobs.wait(job.id).status == "done"
    assert not service.jobs.reserved


def test_cancel_job_waiting_for_worker_restores_queue_immediately(service):
    track = service.search_one("晴天 - 周杰伦", 180)["track"]
    barrier, release = threading.Barrier(5), threading.Event()
    def occupy():
        barrier.wait(timeout=5)
        assert release.wait(5)
    blockers = [service.jobs.pool.submit(occupy) for _ in range(4)]
    barrier.wait(timeout=5)
    try:
        listener = service.events.subscribe()
        job = service.jobs.start("download", track.playlist_id)
        assert service.track(track.id).status == "queued"
        cancelled = service.jobs.cancel(job.id)
        assert cancelled.status == "cancelled" and cancelled.finished_at
        assert service.jobs.futures[job.id].cancelled()
        assert service.track(track.id).status == "matched"
        assert not service.jobs.track_active(track.id)
        assert service.jobs.wait(job.id).status == "cancelled"
        service.update_track(track.id, {"note": "立即解锁"})
        deadline = time.monotonic() + 2
        events = []
        while time.monotonic() < deadline:
            event = listener.get(timeout=2)
            events.append(event)
            if '"status": "cancelled"' in event:
                break
        assert any(event.startswith("event: job\n") and '"status": "cancelled"' in event for event in events)
    finally:
        release.set()
        for blocker in blockers:
            blocker.result()


def test_job_start_rechecks_stale_track_before_queue(service, monkeypatch):
    track = service.search_one("晴天 - 周杰伦", 180)["track"]
    snapshot = service.tracks(track.playlist_id)
    service.update_track(track.id, {"duration": 200})
    monkeypatch.setattr(service, "tracks", lambda identifier: snapshot)
    with pytest.raises(SongSeekError) as error:
        service.jobs.start("download", track.playlist_id)
    assert error.value.code == "no_eligible_tracks"
    assert service.track(track.id).status == "pending"
    assert not service.jobs.reserved and not service.jobs.list()


def test_job_submission_and_execution_never_overwrite_busy_track(service, monkeypatch):
    track = service.search_one("晴天 - 周杰伦", 180)["track"]
    snapshot = service.tracks(track.playlist_id)
    entered, release = threading.Event(), threading.Event()
    def search(*args):
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr(service.providers["mock"], "search", search)
    monkeypatch.setattr(service, "tracks", lambda identifier: snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        search_future = pool.submit(service.search_track, track.id)
        try:
            assert entered.wait(5)
            with pytest.raises(SongSeekError) as error:
                service.jobs.start("download", track.playlist_id)
            assert error.value.code == "track_busy"
            executed = service.jobs._execute("search", threading.Event(), service.search_track, track.id)
            assert executed is False
            assert service.track(track.id).status == "searching"
            assert not service.jobs.reserved and not service.jobs.list()
        finally:
            release.set()
        search_future.result()


@pytest.mark.parametrize("kind", ["search", "download", "lyrics"])
def test_no_eligible_tracks_rejects_without_creating_job(service, kind):
    playlist = service.create_playlist("空清单", "text", [])
    with pytest.raises(SongSeekError) as error:
        service.jobs.start(kind, playlist.id)
    assert error.value.code == "no_eligible_tracks" and error.value.status == 409
    assert str(error.value).startswith("没有可处理的歌曲：")
    assert not service.jobs.list() and not service.jobs.reserved and not service.jobs.futures


def test_download_job_freezes_output_directory_group_and_template(service, tmp_path, monkeypatch):
    import songseek.downloader as module
    service.config.update({"output_dir": str(tmp_path / "before"), "group_by_playlist": True, "filename_template": "旧-{title}", "download_concurrency": 1, "lyrics": False})
    playlist = service.create_playlist("旧清单", "text", [ParsedRow(title="第一首", artist="歌手", duration=180), ParsedRow(title="第二首", artist="歌手", duration=180)])
    for track in service.tracks(playlist.id):
        service.search_track(track.id)
    entered, release = threading.Event(), threading.Event()
    original = service.download_track
    calls = []
    def download(identifier, **kwargs):
        calls.append(identifier)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(identifier, **kwargs)
    monkeypatch.setattr(service, "download_track", download)
    monkeypatch.setattr(module, "transfer", lambda source, path, *args, **kwargs: path.write_bytes(b"audio"))
    monkeypatch.setattr(module, "verify", lambda *args: {"duration": 180, "bitrate": 128, "format": "mp3"})
    monkeypatch.setattr(module, "tag", lambda *args: None)
    monkeypatch.setattr(service.providers["mock"], "resolve", lambda raw: None)
    job = service.jobs.start("download", playlist.id)
    try:
        assert entered.wait(5)
        service.config.update({"output_dir": str(tmp_path / "after"), "group_by_playlist": False, "filename_template": "新-{title}"})
        service.rename_playlist(playlist.id, "新清单")
    finally:
        release.set()
    assert service.jobs.wait(job.id).failed == 0
    files = [Path(t.file.path) for t in service.tracks(playlist.id)]
    assert all(path.parent == tmp_path / "before" / "旧清单" and path.name.startswith("旧-") for path in files)
    next_track = service._add_track(playlist.id, ParsedRow(title="第三首", artist="歌手", duration=180), 3)
    service.search_track(next_track.id)
    later = service.jobs.start("download", playlist.id, [next_track.id])
    assert service.jobs.wait(later.id).failed == 0
    path = Path(service.track(next_track.id).file.path)
    assert path.parent == tmp_path / "after" and path.name.startswith("新-")
