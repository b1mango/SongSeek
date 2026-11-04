import asyncio
import json
import threading

from fastapi.testclient import TestClient

from songseek.errors import SongSeekError
from songseek.models import ProviderInfo, RawCandidate
from songseek.providers.mock import MockProvider
from songseek.server import create_app


def add_provider(service, monkeypatch, identifier, search):
    provider = MockProvider(service.config)
    service.providers[identifier] = provider
    service.config.providers.append(ProviderInfo(id=identifier, label=identifier, max_quality="128k", order=len(service.providers)))
    monkeypatch.setattr(provider, "search", search)


def test_stream_flushes_fast_result_before_slow_provider_finishes(service, monkeypatch):
    fast_sent = threading.Event()
    slow_finished = threading.Event()
    monkeypatch.setattr(service.providers["mock"], "search", lambda *a: [RawCandidate("mock", "fast", "晴天", "周杰伦", duration=180)])

    def slow(*args):
        assert fast_sent.wait(2), "快音源的 NDJSON 行没有及时发送"
        slow_finished.set()
        return [RawCandidate("slow", "slow", "晴天", "周杰伦", duration=180)]

    add_provider(service, monkeypatch, "slow", slow)
    service.config.providers.append(ProviderInfo(id="disabled", label="禁用", max_quality="128k", order=9, enabled=False))
    app = create_app(service=service)

    async def observed(scope, receive, send):
        async def capture(message):
            if message["type"] == "http.response.body":
                for line in message.get("body", b"").splitlines():
                    event = json.loads(line)
                    if event["type"] == "progress" and event["provider"] == "mock":
                        assert not slow_finished.is_set()
                        assert event["candidates"][0]["verdict"] == "match"
                        fast_sent.set()
            await send(message)
        await app(scope, receive, capture)

    # TestClient buffers its body; observe ASGI sends as well to prove actual flushing.
    with TestClient(observed) as client:
        with client.stream("POST", "/api/search/stream", json={"query": "晴天 - 周杰伦", "duration": 180}) as response:
            events = [json.loads(line) for line in response.iter_lines()]
        assert response.headers["content-type"] == "application/x-ndjson"
        assert [e["type"] for e in events] == ["start", "progress", "progress", "done"]
        assert [p["id"] for p in events[0]["providers"]] == ["mock", "slow"]
        first = events[1]["candidates"][0]
        final = events[-1]
        assert first["id"] > 0 and first["preview_url"]
        assert {c["provider"] for c in final["candidates"]} == {"mock", "slow"}
        assert next(c for c in final["candidates"] if c["provider"] == "mock")["id"] == first["id"]
        assert final["track"] == service.track(events[0]["track_id"]).model_dump()
        assert final["candidates"] == [c.model_dump() for c in service.candidates(events[0]["track_id"])]
        assert final["track"]["status"] == "matched"


def test_stream_provider_errors_and_timeout(service, monkeypatch):
    monkeypatch.setattr("songseek.service.SEARCH_TIMEOUT", .1)
    release = threading.Event()
    def slow(*args):
        release.wait(2)
        return []
    def failed(*args):
        raise SongSeekError("音源暂不可用")
    add_provider(service, monkeypatch, "slow", slow)
    add_provider(service, monkeypatch, "broken", failed)
    try:
        with TestClient(create_app(service=service)) as client:
            events = [json.loads(line) for line in client.post("/api/search/stream", json={"query": "晴天 - 周杰伦", "duration": 180}).iter_lines()]
            release.set()
            progress = {e["provider"]: e for e in events if e["type"] == "progress"}
            assert progress["slow"]["error"] == "搜索超时" and not progress["slow"]["ok"]
            assert progress["broken"]["error"] == "音源暂不可用" and not progress["broken"]["ok"]
            assert progress["slow"]["candidates"]
            assert events[-1]["type"] == "done" and events[-1]["track"]["status"] == "matched"
    finally:
        release.set()


def test_stream_fatal_error_resets_status(service, monkeypatch):
    def fail(*args):
        raise SongSeekError("无法保存候选")
    monkeypatch.setattr(service, "_rank_search", fail)
    with TestClient(create_app(service=service)) as client:
        events = [json.loads(line) for line in client.post("/api/search/stream", json={"query": "晴天"}).iter_lines()]
        assert [e["type"] for e in events] == ["start", "error"]
        assert events[-1]["message"] == "无法保存候选"
        assert service.track(events[0]["track_id"]).status == "failed"
        assert client.post("/api/search/stream", json={"query": " "}).status_code == 422


def test_stream_disconnect_finishes_search(service, monkeypatch):
    release = threading.Event()
    def search(*args):
        release.wait(2)
        return [RawCandidate("mock", "a", "晴天", "周杰伦", duration=180)]
    monkeypatch.setattr(service.providers["mock"], "search", search)
    app = create_app(service=service)
    events = []

    async def run():
        disconnected = asyncio.Event()
        request_sent = False
        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b'{"query":"test"}', "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                events.append(json.loads(message["body"]))
                disconnected.set()
                release.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/api/search/stream", "raw_path": b"/api/search/stream",
                 "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1234), "server": ("testserver", 80)}
        await app(scope, receive, send)
    with TestClient(app):
        asyncio.run(run())
        assert events[0]["type"] == "start"
        assert service.track(events[0]["track_id"]).status not in ("pending", "searching")
