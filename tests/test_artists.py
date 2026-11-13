import json
import threading
import time
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from songseek.artists import merge_songs
from songseek.errors import SongSeekError
from songseek.models import AudioSource, Lyrics, ProviderInfo, RawCandidate
from songseek.providers.kugou import KugouProvider
from songseek.providers.netease import NeteaseProvider
from songseek.providers.mock import MockProvider
from songseek.providers.vkeys_qq import VKeysQQProvider
from songseek.query import detect_artist, parse_query
from songseek.server import create_app


@pytest.fixture
def catalog(service, monkeypatch):
    service.providers = {"netease": NeteaseProvider(service.config), "kugou": KugouProvider(service.config),
                         "vkeys_qq": VKeysQQProvider(service.config)}
    service.config.providers = [ProviderInfo(id=p, label=p, order=i, max_quality="320k")
                                for i, p in enumerate(service.providers)]
    for provider in service.providers.values():
        monkeypatch.setattr(provider, "search", lambda *a: [])
        monkeypatch.setattr(provider, "artist_lookup", lambda *a: None)
    return service


def raw(provider="netease", title="晴天", artist="周杰伦", duration=269, source_id="1", **kwargs):
    return RawCandidate(provider, source_id, title, artist, duration=duration, **kwargs)


@pytest.mark.parametrize("query, expected", [
    ("周杰伦 晴天", ("晴天", "周杰伦")), ("晴天 周杰伦", ("晴天", "周杰伦")),
    ("周杰伦 - 晴天", ("晴天", "周杰伦")), ("晴天 - 周杰伦", ("晴天", "周杰伦")),
    ("陳奕迅 十年", ("十年", "陳奕迅")), ("Taylor Swift Love Story", ("Love Story", "Taylor Swift")),
])
def test_parse_both_orders(query, expected):
    results = [raw("youtube", "周杰伦 歌词版", "晴天"), raw(), raw(title="十年", artist="陈奕迅"),
               raw(title="Love Story", artist="Taylor Swift")]
    assert parse_query(query, results) == expected


def test_agreement_and_untrusted_titles():
    results = [raw("netease", "Dusk Till", "Dawn ZAYN"), raw("kugou", "Dusk Till Dawn", "ZAYN"),
               raw("vkeys_qq", "Dusk Till Dawn", "ZAYN")]
    assert parse_query("Dusk Till Dawn ZAYN", results) == ("Dusk Till Dawn", "ZAYN")
    assert parse_query("周杰伦 晴天", [raw("youtube", "周杰伦 歌词版", "晴天")]) == ("周杰伦 晴天", "")


@pytest.mark.parametrize("query, artist", [("周杰倫", "周杰伦"), ("Ｔａｙｌｏｒ Swift", "Taylor Swift")])
def test_artist_detection(query, artist):
    result = detect_artist(query, [raw(artist=artist)], [])
    assert result["name"] == artist
    assert detect_artist(query, [raw(title=query, artist=artist)], [{"name": artist, "aliases": []}]) is None


def test_artist_alias_and_title_veto():
    artist = {"name": "周杰伦", "aliases": ["Jay Chou"]}
    assert detect_artist("jay chou", [], [artist]) == artist
    assert detect_artist("七里香", [raw(title="七里香")], [{"name": "七里香", "aliases": []}]) is None
    assert detect_artist("周杰伦", [raw(), raw(artist="别人"), raw(artist="其他人")], []) is None


def test_merge_versions_duration_and_provider_agreement():
    results = {"netease": [raw(), raw(title="晴天 (Live)", source_id="live"),
                           raw(title="晴天 (DJ)", source_id="dj"), raw(title="晴天 (remix)", source_id="remix"),
                           raw(title="晴天 (伴奏)", source_id="inst"), raw(title="晴天 (clip)", source_id="clip"),
                           raw(title="晴天", album="演唱会", source_id="concert"),
                           raw(title="晴天", artist="翻唱者", source_id="other")],
               "kugou": [raw("kugou", duration=270), raw("kugou", duration=300, source_id="long")]}
    groups = merge_songs(results, {"name": "周杰伦", "aliases": []})
    assert len(groups) == 2
    assert [r.provider for r in groups[0]] == ["netease", "kugou"]
    assert groups[1][0].duration == 300


def install_artist(service, monkeypatch, name="周杰伦"):
    artist = {"id": "6452", "provider": "netease", "name": name, "aliases": ["Jay Chou"],
              "cover_url": "https://example.com/artist.jpg"}
    monkeypatch.setattr(service.providers["netease"], "artist_lookup", lambda *a: artist)
    monkeypatch.setattr(service.providers["netease"], "search", lambda *a: [raw(artist=name)])
    monkeypatch.setattr(service.providers["netease"], "artist_songs", lambda *a: [raw(artist=name)])


def test_artist_stream_no_tracks_tokens_and_complete_status(catalog, monkeypatch):
    install_artist(catalog, monkeypatch)
    with TestClient(create_app(service=catalog)) as client:
        events = [json.loads(line) for line in client.post("/api/search/stream", json={"query": "周杰伦"}).iter_lines()]
        assert events[0]["type"] == "start" and events[-1]["type"] == "artist_done"
        artist = next(e for e in events if e["type"] == "artist")
        assert artist["name"] == "周杰伦" and artist["cover_url"].startswith("/api/artist-songs/")
        song = events[-1]["songs"][0]
        assert song["providers"] == [{"id": "netease", "label": "netease"}]
        assert catalog.artist_songs.get(song["token"])[0].title == "晴天"
        assert not catalog.db.list("tracks") and not catalog.playlists()
        assert {e["provider"] for e in events if e["type"] == "source"} == set(catalog.providers)
        response = client.post("/api/search", json={"query": "周杰伦"}).json()
        assert response["mode"] == "artist" and response["artist"]["aliases"] == ["Jay Chou"]


@pytest.mark.parametrize("selected, expected", [(None, {"netease", "kugou"}), ([], {"netease", "kugou"}),
    (["kugou", "invalid", "vkeys_qq"], {"kugou"}), (["invalid"], set())])
def test_provider_selection(catalog, monkeypatch, selected, expected):
    calls = []
    catalog.config.providers[-1].enabled = False
    for p, provider in catalog.providers.items():
        monkeypatch.setattr(provider, "search", lambda *a, p=p: calls.append(p) or [raw(p)])
        monkeypatch.setattr(provider, "artist_lookup", lambda *a, p=p: calls.append("lookup:" + p))
    events = []
    result = catalog.search_one("晴天 - 周杰伦", 269, selected, on_event=events.append)
    assert result["mode"] == "song"
    assert {p for p in calls if not p.startswith("lookup:")} == expected
    assert {p["id"] for p in events[0]["providers"]} == expected
    assert {c.provider for c in result["candidates"]} == expected


def test_artist_provider_selection(catalog, monkeypatch):
    install_artist(catalog, monkeypatch)
    monkeypatch.setattr(catalog.providers["kugou"], "search", lambda *a: pytest.fail("unselected"))
    monkeypatch.setattr(catalog.providers["kugou"], "artist_lookup", lambda *a: pytest.fail("unselected"))
    result = catalog.search_one("周杰伦", providers=["netease"])
    assert result["mode"] == "artist"
    assert all(s["providers"] == [{"id": "netease", "label": "netease"}] for s in result["songs"])


def test_artist_detection_concurrent_budget(catalog, monkeypatch):
    entered = threading.Event()
    def search(*args):
        assert entered.wait(.5)
        return [raw()]
    def lookup(*args):
        entered.set()
        time.sleep(.95)
        return None
    monkeypatch.setattr(catalog.providers["netease"], "search", search)
    monkeypatch.setattr(catalog.providers["netease"], "artist_lookup", lookup)
    start = time.monotonic()
    result = catalog.search_one("周杰伦", providers=["netease"])
    assert result["mode"] == "artist" and time.monotonic() - start < .92


def test_tokens_expiry_lru_and_404(service, monkeypatch):
    monkeypatch.setattr("songseek.artists.TOKEN_CAPACITY", 2)
    first, second = service.artist_songs.put([raw()]), service.artist_songs.put([raw()])
    service.artist_songs.get(first)
    third = service.artist_songs.put([raw()])
    assert service.artist_songs.get(third)
    with pytest.raises(SongSeekError):
        service.artist_songs.get(second)
    service.artist_songs.cache[first] = (time.monotonic() - 1, [raw()])
    with TestClient(create_app(service=service)) as client:
        for token in (first, "unknown"):
            for endpoint in ("preview", "cover", "lyrics", "download"):
                response = getattr(client, "post" if endpoint == "download" else "get")(f"/api/artist-songs/{token}/{endpoint}")
                assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_token_preview_cover_lyrics_fallback(catalog, monkeypatch):
    a, b = raw(extra={"cover_url": "https://example.com/a.jpg"}), raw("kugou", extra={"cover_url": "https://example.com/b.jpg"})
    token = catalog.artist_songs.put([a, b])
    def fail(*a):
        raise SongSeekError("需要会员", "needs_member")
    monkeypatch.setattr(catalog.providers["netease"], "resolve", fail)
    monkeypatch.setattr(catalog.providers["kugou"], "resolve", lambda *a: AudioSource("https://example.com/audio"))
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == "/a.jpg":
            return httpx.Response(404)
        if request.url.path == "/b.jpg":
            return httpx.Response(200, content=b"\xff\xd8\xffcover")
        return httpx.Response(206, stream=httpx.ByteStream(b"audio"), headers={"content-type": "audio/mpeg", "content-range": "bytes 0-4/5"})
    for provider in catalog.providers.values():
        monkeypatch.setattr(provider, "client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), trust_env=False))
    monkeypatch.setattr(catalog.providers["netease"], "lyrics", lambda *a: None)
    monkeypatch.setattr(catalog.providers["kugou"], "lyrics", lambda *a: Lyrics("[00:01]歌词"))
    catalog.config.settings.lyrics = False
    with TestClient(create_app(service=catalog)) as client:
        base = f"/api/artist-songs/{token}"
        response = client.get(base + "/preview", headers={"Range": "bytes=0-4"})
        assert response.status_code == 206 and response.content == b"audio"
        assert response.headers["x-preview-fallback"] == "1" and requests[-1].headers["range"] == "bytes=0-4"
        assert client.get(base + "/cover").content == b"\xff\xd8\xffcover"
        before = len(requests)
        assert client.get(base + "/cover").status_code == 200
        assert len(requests) == before + 1  # missing first cover, cached fallback
        lyrics = client.get(base + "/lyrics").json()
        assert lyrics["source"] == "kugou" and lyrics["synced"]
        assert not catalog.db.list("tracks")


def test_token_preview_search_fallback_cached_and_reused(catalog, monkeypatch):
    originals = [raw(), raw("kugou")]
    alternative = raw("vkeys_qq", extra={"cover_url": "https://example.com/cover.jpg"})
    token = catalog.artist_songs.put(originals)
    calls = []
    def search(query, limit):
        calls.append(query)
        return [alternative]
    def fail(candidate):
        raise SongSeekError("需要会员")
    for identifier in ("netease", "kugou"):
        monkeypatch.setattr(catalog.providers[identifier], "resolve", fail)
        monkeypatch.setattr(catalog.providers[identifier], "lyrics", lambda *a: None)
    provider = catalog.providers["vkeys_qq"]
    monkeypatch.setattr(provider, "search", search)
    monkeypatch.setattr(provider, "resolve", lambda *a: AudioSource("https://example.com/audio"))
    monkeypatch.setattr(provider, "lyrics", lambda *a: Lyrics("[00:01]晴天"))
    def handle(request):
        if request.url.path == "/cover.jpg":
            return httpx.Response(200, content=b"\xff\xd8\xffcover")
        assert request.headers["range"] == "bytes=0-4"
        return httpx.Response(206, stream=httpx.ByteStream(b"audio"),
                              headers={"content-type": "audio/mpeg", "content-range": "bytes 0-4/5"})
    monkeypatch.setattr(provider, "client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handle)))
    catalog.config.providers[-1].label = "QQ 音乐（VKeys）"
    with TestClient(create_app(service=catalog)) as client:
        base = f"/api/artist-songs/{token}"
        for _ in range(2):
            response = client.get(base + "/preview", headers={"Range": "bytes=0-4"})
            assert response.status_code == 206 and response.content == b"audio"
            assert response.headers["x-preview-fallback"] == "1"
            assert response.headers["x-preview-source"] == quote("QQ 音乐（VKeys）", safe="")
        assert calls == ["晴天 周杰伦"]
        assert catalog.artist_songs.get(token) == [*originals, alternative]
        assert client.get(base + "/cover").content == b"\xff\xd8\xffcover"
        assert client.get(base + "/lyrics").json()["source"] == "vkeys_qq"
        # A later artist-list update must retain an already discovered recording.
        catalog.artist_songs.put(originals, token)
        catalog.artist_songs.append(token, alternative)
        assert catalog.artist_songs.get(token) == [*originals, alternative]


@pytest.mark.parametrize("changes", [{"duration": 300}, {"title": "晴天 (Live)"},
                                    {"album": "Live 演唱会"}, {"extra": {"version_evidence": "Live"}},
                                    {"artist": "其他歌手"}, {"title": "其他歌曲"}])
def test_token_preview_rejects_other_recordings(catalog, monkeypatch, changes):
    token = catalog.artist_songs.put([raw(), raw("kugou")])
    attempts = []
    def fail(candidate):
        attempts.append(candidate.provider)
        raise SongSeekError("无法播放")
    for provider in catalog.providers.values():
        monkeypatch.setattr(provider, "resolve", fail)
    monkeypatch.setattr(catalog.providers["vkeys_qq"], "search", lambda *a: [raw("vkeys_qq", **changes)])
    with TestClient(create_app(service=catalog)) as client:
        response = client.get(f"/api/artist-songs/{token}/preview")
        assert response.status_code == 502
        assert attempts == ["netease", "kugou"]
        assert len(catalog.artist_songs.get(token)) == 2


@pytest.mark.parametrize("search_failure", [False, True])
def test_token_preview_all_sources_fail(catalog, monkeypatch, search_failure):
    token = catalog.artist_songs.put([raw(), raw("kugou")])
    def fail(candidate):
        raise SongSeekError(f"{candidate.provider}无法播放")
    def search(*args):
        if search_failure:
            raise SongSeekError("搜索不可用")
        return [raw("vkeys_qq")]
    for provider in catalog.providers.values():
        monkeypatch.setattr(provider, "resolve", fail)
    monkeypatch.setattr(catalog.providers["vkeys_qq"], "search", search)
    with TestClient(create_app(service=catalog)) as client:
        response = client.get(f"/api/artist-songs/{token}/preview")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "preview_failed"
        message = response.json()["error"]["message"]
        assert "netease无法播放" in message and "kugou无法播放" in message
        assert ("搜索不可用" if search_failure else "vkeys_qq无法播放") in message
        assert len(catalog.artist_songs.get(token)) == 2


def test_token_preview_provider_order_budget_and_first_success(catalog, monkeypatch):
    for identifier in ("zpod", "bilibili"):
        catalog.providers[identifier] = MockProvider(catalog.config)
        catalog.config.providers.append(ProviderInfo(id=identifier, label=identifier, order=-1, max_quality="128k"))
    catalog.config.providers[-1].enabled = False
    token = catalog.artist_songs.put([raw()])
    searches, attempts, deadlines = [], [], []
    clock = [100.0]
    monkeypatch.setattr("songseek.artists.time.monotonic", lambda: clock[0])
    # The token was created using the real clock, so remains valid with this test clock.
    def search(identifier, *args):
        searches.append(identifier)
        deadlines.append(catalog.providers[identifier]._search.deadline)
        return [raw(identifier)]
    for identifier, provider in catalog.providers.items():
        monkeypatch.setattr(provider, "search", lambda *a, p=identifier: search(p, *a))
    def open_preview(candidate, label, fallback):
        attempts.append(candidate.provider)
        if candidate.provider == "zpod":
            assert fallback
            return "audio"
        raise SongSeekError("无法播放")
    assert catalog.artist_songs.preview(token, open_preview) == "audio"
    assert searches == ["kugou", "vkeys_qq", "zpod"]
    assert deadlines == [105.0] * 3
    assert attempts == ["netease", "kugou", "vkeys_qq", "zpod"]
    searches.clear()
    token = catalog.artist_songs.put([raw()])
    def slow_search(*args):
        clock[0] += 5
        return [raw("kugou")]
    monkeypatch.setattr(catalog.providers["kugou"], "search", slow_search)
    with pytest.raises(SongSeekError) as error:
        catalog.artist_songs.preview(token, open_preview)
    assert error.value.status == 502
    assert not searches


@pytest.mark.parametrize("selected", [None, ["netease"]])
def test_token_download_reuses_cached_fallback_with_provider_selection(catalog, monkeypatch, selected):
    token = catalog.artist_songs.put([raw(), raw("vkeys_qq")])
    monkeypatch.setattr(catalog, "download_track", lambda track_id, **kw: catalog.track(track_id))
    result = catalog.download_artist_song(token, lyrics=False, providers=selected)
    candidates = catalog.candidates(result["track"].id)
    assert {c.provider for c in candidates} == ({"netease", "vkeys_qq"} if selected is None else {"netease"})
    assert result["job"] is not None
    catalog.jobs.wait(result["job"].id)


@pytest.mark.parametrize("auto", [True, False])
def test_token_download_search_and_job(catalog, monkeypatch, auto):
    catalog.config.settings.auto_approve = auto
    catalog.config.settings.lyrics = False
    calls = []
    monkeypatch.setattr(catalog.providers["netease"], "search", lambda query, limit: calls.append(query) or [raw()])
    # Leave real job queuing and state transitions in place, replace only the download operation.
    monkeypatch.setattr(catalog, "download_track", lambda track_id, **kw: catalog.track(track_id))
    token = catalog.artist_songs.put([raw()])
    with TestClient(create_app(service=catalog)) as client:
        response = client.post(f"/api/artist-songs/{token}/download", json={"lyrics": True})
        assert response.status_code == 200
        result = response.json()
        assert calls == ["晴天 - 周杰伦"]
        assert result["track"]["duration"] == 269 and result["track"]["lyrics_wanted"] is True
        assert catalog.playlist(result["track"]["playlist_id"]).name == "单曲搜索"
        if auto:
            assert result["job"]["kind"] == "download"
            catalog.jobs.wait(result["job"]["id"])
        else:
            assert result["job"] is None and result["track"]["status"] == "needs_review"


@pytest.mark.parametrize("kind", ["netease", "kugou"])
def test_artist_provider_http(catalog, monkeypatch, kind):
    provider = (NeteaseProvider if kind == "netease" else KugouProvider)(catalog.config)
    seen = []
    def handle(request):
        seen.append(request)
        if request.url.path == "/api/search/get":
            assert request.url.params["type"] == "100"
            return httpx.Response(200, json={"code": 200, "result": {"artists": [
                {"id": 1, "name": "周杰伦", "alias": ["Jay Chou"], "picUrl": "https://example.com/cover"}]}})
        if request.url.path == "/api/artist/top/song":
            return httpx.Response(200, json={"code": 200, "songs": [
                {"id": 2, "name": "晴天", "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美"}, "dt": 269000}]})
        if request.url.path == "/api/v3/search/singer":
            return httpx.Response(200, json={"status": 1, "data": [{"singername": "周杰伦", "singerid": 1}]})
        assert request.url.params["sorttype"] == "2"
        return httpx.Response(200, json={"status": 1, "data": {"info": [
            {"filename": "周杰伦 - 晴天", "hash": "abc", "duration": 269, "album_name": "叶惠美"}]}})
    monkeypatch.setattr(provider, "client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handle), trust_env=False))
    artist = provider.artist_lookup("Jay Chou" if kind == "netease" else "周杰伦")
    assert artist["name"] == "周杰伦"
    songs = provider.artist_songs(artist)
    assert songs[0].title == "晴天" and songs[0].duration == 269
    assert len(seen) == 2
    if kind == "kugou":
        assert all(request.url.scheme == "https" and request.url.host == "mobiles.kugou.com" for request in seen)


def test_merge_demotes_featured_and_drops_ensembles():
    def raw(provider, sid, title, artist):
        return RawCandidate(provider, sid, title, artist, duration=200)

    results = {
        "netease": [raw("netease", "1", "布拉格广场", "蔡依林 & 周杰伦"),
                    raw("netease", "2", "手牵手", " & ".join(["王力宏", "陶喆", "周杰伦", "蔡依林", "孙燕姿"])),
                    raw("netease", "3", "稻香", "周杰伦")],
        "kugou": [raw("kugou", "a", "发如雪", "周杰伦"), raw("kugou", "b", "晴天", "周杰伦")],
    }
    titles = [g[0].title for g in merge_songs(results, {"name": "Jay Chou", "aliases": ["周杰伦"]})]
    assert "手牵手" not in titles
    assert titles.index("布拉格广场") > titles.index("晴天")
    assert titles[0] in {"稻香", "发如雪"}
