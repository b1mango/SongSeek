from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from songseek.errors import SongSeekError
from songseek.models import AudioSource, ProviderInfo, RawCandidate
from songseek.providers.mock import MockProvider
from songseek.server import create_app


def setup_candidates(service, monkeypatch, raws):
    service.providers = {}
    service.config.providers = []
    for identifier in dict.fromkeys(r.provider for r in raws):
        provider = MockProvider(service.config)
        service.providers[identifier] = provider
        service.config.providers.append(ProviderInfo(id=identifier, label="网易云" if identifier == "netease" else identifier,
                                                    max_quality="320k", order=len(service.config.providers)))
        monkeypatch.setattr(provider, "search", lambda *a, p=identifier: [r for r in raws if r.provider == p])
    return service.search_one("白鸽 - 伍佰 & China Blue", 367)["candidates"]


def raw(provider, source="a", **changes):
    return RawCandidate(**({"provider": provider, "source_id": source, "title": "白鸽", "artist": "伍佰 & China Blue",
                           "duration": 367, "bitrate": 128} | changes))


def assert_slots_free(service):
    for provider in service.providers.values():
        assert provider.semaphore.acquire(blocking=False)
        assert provider.semaphore.acquire(blocking=False)
        assert not provider.semaphore.acquire(blocking=False)
        provider.semaphore.release()
        provider.semaphore.release()


@pytest.mark.parametrize("failure", ["resolve", "client", "send", "http", "content", "encoding", "local"])
def test_preview_fallback_range_and_cleanup(service, monkeypatch, failure):
    candidates = setup_candidates(service, monkeypatch, [raw("netease", extra={"fee": 1}), raw("vkeys_qq")])
    chosen = next(c for c in candidates if c.provider == "netease")
    clients, responses, attempts = [], [], []

    def resolve(candidate):
        attempts.append(candidate.provider)
        if candidate.provider == "netease" and failure == "resolve":
            raise SongSeekError("需要会员才能播放")
        if candidate.provider == "netease" and failure == "local":
            return AudioSource("", local_path=str(service.config.home / "missing.mp3"))
        return AudioSource("https://audio.example/song", {"Referer": "https://required.example/"})

    def make_client(provider, **kwargs):
        if provider == "netease" and failure == "client":
            raise httpx.ConnectError("private upstream detail")
        def handle(request):
            assert request.headers["Range"] == "bytes=3-6"
            assert request.headers["Referer"] == "https://required.example/"
            if provider == "netease":
                if failure == "send":
                    raise httpx.ConnectError("private upstream detail")
                response = httpx.Response(503 if failure == "http" else 200,
                                          headers={"content-type": "text/html" if failure == "content" else "audio/mpeg",
                                                   "content-encoding": "gzip" if failure == "encoding" else "identity"},
                                          stream=httpx.ByteStream(b"bad"))
            else:
                response = httpx.Response(206, headers={"content-type": "audio/mpeg", "content-range": "bytes 3-6/10",
                                                        "accept-ranges": "bytes", "content-length": "4"}, stream=httpx.ByteStream(b"abcd"))
            responses.append(response)
            return response
        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    for identifier, provider in service.providers.items():
        monkeypatch.setattr(provider, "resolve", resolve)
        monkeypatch.setattr(provider, "client", lambda p=identifier, **kw: make_client(p, **kw))
    picked_before = service.track(chosen.track_id).chosen_candidate_id
    with TestClient(create_app(service=service)) as client:
        response = client.get(chosen.preview_url, headers={"Range": "bytes=3-6"})
        assert response.status_code == 206 and response.content == b"abcd"
        assert response.headers["content-range"] == "bytes 3-6/10"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["X-Preview-Source"] == quote("vkeys_qq", safe="")
        assert response.headers["X-Preview-Fallback"] == "1"
        assert attempts == ["netease", "vkeys_qq"]
        assert service.track(chosen.track_id).chosen_candidate_id == picked_before  # previewing never changes the pick
        assert all(c.is_closed for c in clients) and all(r.is_closed for r in responses)
        assert_slots_free(service)


@pytest.mark.parametrize("fallback", [False, True])
def test_preview_416_is_preserved_without_further_fallback(service, monkeypatch, fallback):
    candidates = setup_candidates(service, monkeypatch, [raw("netease"), raw("vkeys_qq"), raw("kugou")])
    chosen = next(c for c in candidates if c.provider == "netease")
    attempts, clients, responses = [], [], []
    def resolve(candidate):
        attempts.append(candidate.provider)
        if fallback and candidate.provider == "netease":
            raise SongSeekError("需要会员")
        return AudioSource("https://audio.example/song")
    def make_client(**kwargs):
        def handle(request):
            response = httpx.Response(416, headers={"Content-Range": "bytes */10"}, stream=httpx.ByteStream(b""))
            responses.append(response)
            return response
        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client
    for provider in service.providers.values():
        monkeypatch.setattr(provider, "resolve", resolve)
        monkeypatch.setattr(provider, "client", make_client)
    with TestClient(create_app(service=service)) as client:
        response = client.get(chosen.preview_url, headers={"Range": "bytes=100-"})
        assert response.status_code == 416 and response.headers["Content-Range"] == "bytes */10"
        assert response.headers["X-Preview-Source"] == quote("vkeys_qq" if fallback else "网易云", safe="")
        assert response.headers.get("X-Preview-Fallback") == ("1" if fallback else None)
        assert attempts == (["netease", "vkeys_qq"] if fallback else ["netease"])
        assert all(c.is_closed for c in clients) and all(r.is_closed for r in responses)
        assert_slots_free(service)


def test_preview_same_recording_filter_order_rank_and_attempt_limit(service, monkeypatch):
    candidates = setup_candidates(service, monkeypatch, [
        raw("netease"), raw("vkeys_qq", "low"), raw("vkeys_qq", "high", bitrate=320),
        raw("vkeys_qq", "live", title="白鸽 Live"), raw("vkeys_qq", "cover", artist="其他歌手"),
        raw("vkeys_qq", "duration", duration=300), raw("vkeys_qq", "title", title="其他"),
        raw("kugou", bitrate=320), raw("zpod", bitrate=320),
    ])
    chosen = next(c for c in candidates if c.provider == "netease")
    attempts = []
    def fail(candidate):
        attempts.append((candidate.provider, candidate.source_id))
        raise SongSeekError("需要会员" if candidate.provider == "netease" else "无法播放")
    for provider in service.providers.values():
        monkeypatch.setattr(provider, "resolve", fail)
    with TestClient(create_app(service=service)) as client:
        response = client.get(chosen.preview_url)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "preview_failed"
        assert "网易云：需要会员" in response.json()["error"]["message"]
        assert attempts == [("netease", "a"), ("vkeys_qq", "high"), ("vkeys_qq", "low"), ("kugou", "a")]
        assert_slots_free(service)


def test_preview_no_same_recording_specific_error(service, monkeypatch):
    candidates = setup_candidates(service, monkeypatch, [raw("netease"), raw("kugou", title="白鸽 Live")])
    chosen = next(c for c in candidates if c.provider == "netease")
    def fail(candidate):
        raise SongSeekError("需要会员")
    monkeypatch.setattr(service.providers["netease"], "resolve", fail)
    with TestClient(create_app(service=service)) as client:
        response = client.get(chosen.preview_url)
        assert response.status_code == 502
        assert response.json()["error"]["message"] == "网易云：需要会员；其他音源没有同一版本可试听"


@pytest.mark.parametrize("cached", [False, True])
def test_preview_local_fallback_with_range(service, monkeypatch, tmp_path, cached):
    alternative = "youtube" if cached else "kugou"
    candidates = setup_candidates(service, monkeypatch, [raw("netease"), raw(alternative)])
    chosen = next(c for c in candidates if c.provider == "netease")
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"0123456789")
    def fail(candidate):
        raise SongSeekError("需要会员")
    monkeypatch.setattr(service.providers["netease"], "resolve", fail)
    monkeypatch.setattr(service.providers[alternative], "resolve", lambda candidate: AudioSource(
        "https://audio.example/song", local_path=None if cached else str(path)))
    if cached:
        monkeypatch.setattr(service.previews, "get", lambda *args: path)
    picked_before = service.track(chosen.track_id).chosen_candidate_id
    with TestClient(create_app(service=service)) as client:
        response = client.get(chosen.preview_url, headers={"Range": "bytes=3-6"})
        assert response.status_code == 206 and response.content == b"3456"
        assert response.headers["X-Preview-Fallback"] == "1"
        assert response.headers["X-Preview-Source"] == alternative
        assert_slots_free(service)


def test_preview_stream_read_failure_closes_resources(service, monkeypatch):
    candidates = setup_candidates(service, monkeypatch, [raw("netease")])
    provider = service.providers["netease"]
    clients, responses = [], []
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"audio"
            raise httpx.ReadError("upstream interrupted")
    def make_client(**kwargs):
        def handle(request):
            response = httpx.Response(200, headers={"content-type": "audio/mpeg"}, stream=BrokenStream())
            responses.append(response)
            return response
        client = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client
    monkeypatch.setattr(provider, "resolve", lambda candidate: AudioSource("https://audio.example/song"))
    monkeypatch.setattr(provider, "client", make_client)
    with TestClient(create_app(service=service)) as client:
        response = client.get(candidates[0].preview_url)
        assert response.status_code == 200 and response.content == b"audio"
        assert all(c.is_closed for c in clients) and all(r.is_closed for r in responses)
        assert_slots_free(service)
