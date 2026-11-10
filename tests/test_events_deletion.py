import json
import threading
import time

from songseek.events import Broadcaster


def read_event(listener, timeout=0.5):
    event = listener.get(timeout=timeout)
    lines = event.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0][len("event: "):], json.loads(lines[1][len("data: "):])


def test_track_delete_cancels_pending_and_broadcasts_before_updated_playlist():
    broadcaster = Broadcaster(interval=0.12)
    first, second = broadcaster.subscribe(), broadcaster.subscribe()
    track = {"id": 1, "playlist_id": 10, "status": "pending"}
    old_playlist = {"id": 10, "counts": {"total": 2}}
    try:
        broadcaster.emit("track", track)
        broadcaster.emit("playlist", old_playlist)
        broadcaster.emit("track", {**track, "status": "done"})
        broadcaster.emit("playlist", {"id": 10, "counts": {"total": 1}})
        broadcaster.delete_track(1, 10)
        broadcaster.emit("playlist", {"id": 10, "counts": {"total": 0}})
        # Work that produced a snapshot before deletion can reach emit late.
        broadcaster.emit("track", {**track, "status": "stale"})
        broadcaster.emit("track_progress", {"track_id": 1, "playlist_id": 10})
        for listener in (first, second):
            assert read_event(listener) == ("track", track)
            assert read_event(listener) == ("playlist", old_playlist)
            assert read_event(listener) == ("track_deleted", {"id": 1, "playlist_id": 10})
            assert read_event(listener) == ("playlist", {"id": 10, "counts": {"total": 0}})
        time.sleep(0.2)
        assert first.empty() and second.empty()
        assert ("track", 1) not in broadcaster.pending
        assert ("track", 1) not in broadcaster.last
    finally:
        broadcaster.close()


def test_playlist_delete_cancels_child_snapshots_and_preserves_other_events():
    broadcaster = Broadcaster(interval=0.12)
    listener = broadcaster.subscribe()
    try:
        broadcaster.emit("playlist", {"id": 10, "name": "old"})
        broadcaster.emit("track", {"id": 1, "playlist_id": 10, "status": "pending"})
        broadcaster.emit("track", {"id": 2, "playlist_id": 10, "status": "pending"})
        broadcaster.emit("track", {"id": 3, "playlist_id": 20, "status": "pending"})
        broadcaster.emit("job", {"id": 50, "playlist_id": 10, "status": "running"})
        broadcaster.emit("playlist", {"id": 10, "name": "stale"})
        broadcaster.emit("job", {"id": 50, "playlist_id": 10, "status": "done"})
        for track_id, playlist_id in ((1, 10), (2, 10), (3, 20)):
            broadcaster.emit("track", {"id": track_id, "playlist_id": playlist_id, "status": "done"})
        for _ in range(5):
            read_event(listener)
        # ID 2 is discovered from pending too, so even an incomplete captured list is cleaned.
        broadcaster.delete_playlist(10, [1])
        broadcaster.emit("playlist", {"id": 10, "name": "late"})
        broadcaster.emit("track", {"id": 1, "playlist_id": 10, "status": "late"})
        broadcaster.emit("track", {"id": 2, "playlist_id": 10, "status": "late"})
        broadcaster.emit("track", {"id": 4, "playlist_id": 10, "status": "late"})
        broadcaster.emit("track_progress", {"track_id": 1, "playlist_id": 10})
        broadcaster.emit("job", {"id": 50, "playlist_id": 10, "status": "done"})
        assert read_event(listener) == ("playlist_deleted", {"id": 10})
        assert read_event(listener) == ("track", {"id": 3, "playlist_id": 20, "status": "done"})
        time.sleep(0.1)
        assert listener.empty()
        assert all(key not in broadcaster.pending and key not in broadcaster.last
                   for key in (("playlist", 10), ("track", 1), ("track", 2), ("job", 50)))
    finally:
        broadcaster.close()


def test_cancelled_timer_cannot_consume_a_replacement_timer():
    broadcaster = Broadcaster(interval=10)
    listener = broadcaster.subscribe()
    try:
        broadcaster.emit("playlist", {"id": 10, "name": "first"})
        broadcaster.emit("playlist", {"id": 10, "name": "old pending"})
        old_timer = broadcaster.timers[("playlist", 10)]
        read_event(listener)
        broadcaster.delete_track(1, 10)  # Cancels the pending playlist snapshot.
        broadcaster.emit("playlist", {"id": 10, "name": "after delete"})
        broadcaster.emit("playlist", {"id": 10, "name": "new pending"})
        new_timer = broadcaster.timers[("playlist", 10)]
        broadcaster._flush(("playlist", 10), old_timer)  # A cancelled callback already waiting on the lock.
        assert read_event(listener) == ("track_deleted", {"id": 1, "playlist_id": 10})
        assert read_event(listener) == ("playlist", {"id": 10, "name": "after delete"})
        assert listener.empty()
        broadcaster._flush(("playlist", 10), new_timer)
        assert read_event(listener) == ("playlist", {"id": 10, "name": "new pending"})
    finally:
        broadcaster.close()


def test_delete_waits_for_an_in_flight_flush_and_sends_after_it():
    broadcaster = Broadcaster(interval=10)
    listener = broadcaster.subscribe()
    entered, release, deletion_started = threading.Event(), threading.Event(), threading.Event()
    original_send = broadcaster._send
    snapshot = {"id": 1, "playlist_id": 10, "status": "pending"}
    pending = {**snapshot, "status": "done"}
    threads = []
    try:
        broadcaster.emit("track", snapshot)
        broadcaster.emit("track", pending)
        timer = broadcaster.timers[("track", 1)]
        timer.cancel()
        assert read_event(listener) == ("track", snapshot)

        def blocked_send(kind, data):
            if kind == "track" and data == pending:
                entered.set()
                assert release.wait(2)
            original_send(kind, data)

        broadcaster._send = blocked_send
        flush = threading.Thread(target=broadcaster._flush, args=(("track", 1), timer))
        threads.append(flush)
        flush.start()
        assert entered.wait(1)

        def delete():
            deletion_started.set()
            broadcaster.delete_track(1, 10)

        deletion = threading.Thread(target=delete)
        threads.append(deletion)
        deletion.start()
        assert deletion_started.wait(1)
        assert listener.empty()
        release.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert read_event(listener) == ("track", pending)
        assert read_event(listener) == ("track_deleted", {"id": 1, "playlist_id": 10})
        assert listener.empty()
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
        broadcaster.close()
