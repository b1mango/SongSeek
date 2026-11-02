import json
import queue
import threading
import time


class Broadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.listeners: set[queue.Queue] = set()
        self.last_job: dict[int, float] = {}
        self.pending: dict[int, dict] = {}
        self.timers: dict[int, threading.Timer] = {}

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

    def _flush(self, identifier: int) -> None:
        with self.lock:
            self._send("job", self.pending.pop(identifier))
            self.last_job[identifier] = time.monotonic()
            self.timers.pop(identifier)

    def emit(self, kind: str, data: dict) -> None:
        with self.lock:
            if kind == "job":
                identifier = data["id"]
                if identifier in self.timers:
                    self.pending[identifier] = data
                    return
                delay = .25 - (time.monotonic() - self.last_job.get(identifier, 0))
                if delay > 0:
                    self.pending[identifier] = data
                    if identifier not in self.timers:
                        timer = threading.Timer(delay, self._flush, args=(identifier,))
                        timer.daemon = True
                        self.timers[identifier] = timer
                        timer.start()
                    return
                self.last_job[identifier] = time.monotonic()
            self._send(kind, data)

    def close(self) -> None:
        with self.lock:
            for timer in self.timers.values():
                timer.cancel()
