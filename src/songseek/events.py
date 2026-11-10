import json
import queue
import threading
import time
from collections.abc import Iterable

COALESCED = ("job", "track", "playlist")  # state snapshots: only the latest per id matters


class Broadcaster:
    def __init__(self, interval: float = .25):
        self.interval = interval
        self.lock = threading.Lock()
        self.listeners: set[queue.Queue] = set()
        self.last: dict[tuple[str, int], float] = {}
        self.pending: dict[tuple[str, int], dict] = {}
        self.timers: dict[tuple[str, int], threading.Timer] = {}
        # Database IDs are AUTOINCREMENT: a deleted ID cannot represent a new record.
        self.deleted_tracks: set[int] = set()
        self.deleted_playlists: set[int] = set()

    def subscribe(self) -> queue.Queue:
        listener = queue.Queue()
        with self.lock:
            self.listeners.add(listener)
        return listener

    def unsubscribe(self, listener: queue.Queue) -> None:
        with self.lock:
            self.listeners.discard(listener)

    def _send(self, kind: str, data: dict) -> None:
        event = f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for listener in self.listeners:
            listener.put(event)

    def _flush(self, key: tuple[str, int], timer: threading.Timer) -> None:
        with self.lock:
            if self.timers.get(key) is not timer:
                return  # cancelled or replaced while this callback was waiting
            self.timers.pop(key)
            self._send(key[0], self.pending.pop(key))
            self.last[key] = time.monotonic()

    def _discard(self, key: tuple[str, int]) -> None:
        timer = self.timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        self.pending.pop(key, None)
        self.last.pop(key, None)

    def delete_track(self, track_id: int, playlist_id: int) -> None:
        """Call after deleting a track from the database, before publishing its playlist snapshot."""
        with self.lock:
            self.deleted_tracks.add(track_id)
            self._discard(("track", track_id))
            self._discard(("playlist", playlist_id))
            self._send("track_deleted", {"id": track_id, "playlist_id": playlist_id})

    def delete_playlist(self, playlist_id: int, track_ids: Iterable[int]) -> None:
        """Call after deleting a playlist; track_ids are its track IDs captured before deletion."""
        with self.lock:
            self.deleted_playlists.add(playlist_id)
            child_ids = set(track_ids)
            self._discard(("playlist", playlist_id))
            child_ids.update(key[1] for key, data in self.pending.items()
                             if key[0] == "track" and data.get("playlist_id") == playlist_id)
            for key, data in list(self.pending.items()):
                if key[0] == "job" and data.get("playlist_id") == playlist_id:
                    self._discard(key)
            self.deleted_tracks.update(child_ids)
            for track_id in child_ids:
                self._discard(("track", track_id))
            self._send("playlist_deleted", {"id": playlist_id})

    def emit(self, kind: str, data: dict) -> None:
        """Snapshot events go out at once, then at most one per interval per id; the last one always arrives."""
        with self.lock:
            if kind == "playlist" and data["id"] in self.deleted_playlists:
                return
            if kind == "job" and data.get("playlist_id") in self.deleted_playlists:
                return
            if kind in ("track", "track_progress"):
                track_id = data["id"] if kind == "track" else data["track_id"]
                if track_id in self.deleted_tracks or data.get("playlist_id") in self.deleted_playlists:
                    return
            if kind in COALESCED and self.interval > 0:
                key = (kind, data["id"])
                if key in self.timers:
                    self.pending[key] = data
                    return
                delay = self.interval - (time.monotonic() - self.last.get(key, float("-inf")))
                if delay > 0:
                    self.pending[key] = data
                    timer = threading.Timer(delay, lambda: self._flush(key, timer))
                    timer.daemon = True
                    self.timers[key] = timer
                    timer.start()
                    return
                self.last[key] = time.monotonic()
            self._send(kind, data)

    def close(self) -> None:
        with self.lock:
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()
            self.pending.clear()
