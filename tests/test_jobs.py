import queue
import threading
import time

from songseek.events import Broadcaster
from songseek.models import Job, ParsedRow, now
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
    assert calls == ["晴天 周杰伦", "晴天"]
    assert result["candidates"] and result["track"].error
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
