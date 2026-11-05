import base64
import csv
import io
import json
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import mutagen
import pytest
from fastapi.testclient import TestClient

from songseek import lyrics as lyrics_module
from songseek.downloader import digest
from songseek.lyrics import atomic_lrc, embed_file, merge_translation, normalize_lrc
from songseek.lrclib import LRCLib
from songseek.models import DownloadedFile, Lyrics, LyricsInfo, LyricsResponse, ParsedRow, ProviderInfo, RawCandidate, Settings
from songseek.providers.kugou import KugouProvider
from songseek.providers.base import USER_AGENT
from songseek.providers.mock import MockProvider
from songseek.providers.netease import NeteaseProvider
from songseek.providers.vkeys_qq import VKeysQQProvider
from songseek.server import create_app


@pytest.fixture(autouse=True)
def lrclib_http(monkeypatch):
    def install(handler):
        monkeypatch.setattr(LRCLib, "client", lambda self, proxy, timeout: httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False, headers={"User-Agent": USER_AGENT}))
    install(lambda request: httpx.Response(404))
    return install


@pytest.fixture
def track(service):
    return service.search_one("晴天 - 周杰伦", 180)["track"]


def add_candidate(service, track, provider_id, source_id, **values):
    if provider_id not in service.providers:
        service.providers[provider_id] = MockProvider(service.config)
        service.config.providers.append(ProviderInfo(id=provider_id, label=provider_id, max_quality="320k", order=len(service.config.providers)))
    raw = RawCandidate(provider=provider_id, source_id=source_id, title="晴天", artist="周杰伦", duration=180, **values)
    from songseek.matching import evaluate
    candidate = evaluate(track, raw, provider_id)
    candidate.id = service.db.insert("candidates", candidate.model_dump(), track_id=track.id, provider=provider_id,
                                     source_id=source_id, raw=json.dumps(asdict(raw)))
    service.db.save("candidates", candidate.model_dump())
    return candidate


def downloaded(service, track, tmp_path):
    path = tmp_path / f"{track.id}.mp3"
    path.write_bytes(b"audio")
    track.status = "done"
    track.file = DownloadedFile(path=str(path), size=5, format="mp3", bitrate=128, duration=180,
                                sha256=digest(path), provider="mock", provider_label="离线演示")
    return service.save_track(track)


def fake_http(monkeypatch, provider, handler):
    monkeypatch.setattr(provider, "client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False))


@pytest.mark.parametrize("provider_class,expected_path,payload", [
    (NeteaseProvider, "/api/song/lyric", {"code": 200, "lrc": {"lyric": "[00:01.00]original"}, "tlyric": {"lyric": "[00:01.00]译文"}}),
    (VKeysQQProvider, "/v2/music/tencent/lyric", {"code": 200, "data": {"lyric": "[00:01.00]original", "trans": "[00:01.00]译文"}}),
])
def test_provider_lyrics_fake_http(config, monkeypatch, provider_class, expected_path, payload):
    provider = provider_class(config)
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.path == expected_path
        assert "cookie" not in request.headers
        if provider.id == "netease":
            assert b"id=42" in request.content and b"tv=-1" in request.content
        else:
            assert request.url.params["mid"] == "42"
        return httpx.Response(200, json=payload)
    fake_http(monkeypatch, provider, handle)
    if provider.id == "netease":
        waits = []
        monkeypatch.setattr("songseek.providers.netease.wait_for_request", lambda: waits.append(True))
    result = provider.lyrics(RawCandidate(provider.id, "42", "晴天", extra={"fee": 1}))
    assert result == Lyrics("[00:01.00]original", "[00:01.00]译文")
    assert len(calls) == 1
    if provider.id == "netease":
        assert waits == [True]


def test_kugou_anonymous_search_decode_and_match(config, monkeypatch):
    provider = KugouProvider(config)
    requests = []
    def handle(request):
        requests.append(request)
        assert "cookie" not in request.headers
        if request.url.path == "/search":
            assert request.url.params["hash"] == "HASH"
            return httpx.Response(200, json={"candidates": [
                {"song": "别的歌", "singer": "伍佰", "duration": 364000},
                {"song": "白鸽", "singer": "别人", "duration": 364000},
                {"song": "白鸽", "singer": "伍佰", "duration": 400000},
                {"song": "白鸽", "singer": "伍佰", "duration": 364000, "id": "123", "accesskey": "fake-public-lyric-key"}]})
        assert request.url.params["id"] == "123"
        return httpx.Response(200, json={"content": base64.b64encode("[00:01.00]歌词".encode()).decode()})
    fake_http(monkeypatch, provider, handle)
    assert provider.lyrics(RawCandidate("kugou", "HASH", "白鸽", "伍佰", duration=364)).lrc == "[00:01.00]歌词"
    assert len(requests) == 2


@pytest.mark.parametrize("text", ["", "[ti:歌名]\n[00:00.00]", "[00:00.00]纯音乐，请欣赏", "暂无歌词"])
def test_empty_and_placeholder_rejected(track, text):
    assert normalize_lrc(text, track, 180) is None


def test_normalize_timestamps_headers_credits_offset_and_translation(track):
    text = '\ufeff[ti:保留标题]\n[offset:100]\n{"t":0,"c":[]}\n[00:03.5]后面  \n[00:01.00][00:02.00]开头\n'
    normalized = normalize_lrc(text, track, 180)
    assert normalized == "[ti:保留标题]\n[ar:周杰伦]\n[al:]\n[00:00.900]开头\n[00:01.900]开头\n[00:03.400]后面\n"
    translated = normalize_lrc("[00:00.90]first\n[00:03.40]last", track)
    merged, enabled = merge_translation(normalized, translated)
    assert enabled
    assert "[00:00.900]开头\n[00:00.900]first\n[00:01.900]开头" in merged
    assert normalize_lrc(merged, track) == merged
    assert merge_translation(normalized, None) == (normalized, False)
    assert normalize_lrc("[03:15.00]边界", track, 180)
    assert normalize_lrc("[03:15.01]越界", track, 180) is None


def test_selected_candidate_preferred_and_cache_expires(service, track, monkeypatch):
    calls = []
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: calls.append(raw.source_id) or Lyrics("[00:01.00]original"))
    alternate = add_candidate(service, track, "other", "other")
    monkeypatch.setattr(service.providers["other"], "lyrics", lambda raw: pytest.fail("should prefer chosen"))
    result = service.lyrics.preview(track.chosen_candidate_id)
    assert result.synced and result.source == "mock" and result.translation is None
    assert service.lyrics.preview(track.chosen_candidate_id) == result
    assert len(calls) == 1
    service.lyrics.cache[(track.chosen_candidate_id, 180)] = (time.monotonic() - 1, result)
    service.lyrics.preview(track.chosen_candidate_id)
    assert len(calls) == 2
    assert alternate.id != track.chosen_candidate_id


@pytest.mark.parametrize("primary", [None, Lyrics("纯音乐，请欣赏"), Lyrics("plain lyrics"), Lyrics("[03:16.00]wrong duration")])
def test_same_recording_synced_fallback(service, track, monkeypatch, primary):
    calls = []
    chosen = service.candidate(track.chosen_candidate_id)
    def primary_lyrics(raw):
        assert raw.source_id == chosen.source_id
        return primary
    monkeypatch.setattr(service.providers["mock"], "lyrics", primary_lyrics)
    add_candidate(service, track, "second", "second")
    add_candidate(service, track, "first", "first")
    service.config.providers[-1].order = -1
    for name in ("first", "second"):
        monkeypatch.setattr(service.providers[name], "lyrics", lambda raw: calls.append(raw.provider) or Lyrics("[00:01.00]同步"))
    result = service.lyrics.fetch(track, chosen)
    assert result.source == "first" and result.synced and calls == ["first"]


def test_fresh_netease_requires_title_artist_version_and_actual_duration(service, track, tmp_path, monkeypatch):
    track = downloaded(service, track, tmp_path)
    track.duration = 200
    provider = NeteaseProvider(service.config)
    service.providers["netease"] = provider
    service.config.providers.append(ProviderInfo(id="netease", label="网易云", max_quality="320k", order=1))
    calls = []
    def handle(request):
        if request.url.path == "/api/cloudsearch/pc":
            assert "晴天" in request.content.decode() or "%E6%99%B4" in request.content.decode()
            return httpx.Response(200, json={"code": 200, "result": {"songs": [
                {"id": 1, "name": "另一首", "ar": [{"name": "周杰伦"}], "dt": 180000},
                {"id": 2, "name": "晴天", "ar": [{"name": "别人"}], "dt": 180000},
                {"id": 3, "name": "晴天 (Live)", "ar": [{"name": "周杰伦"}], "dt": 180000},
                {"id": 4, "name": "晴天", "ar": [{"name": "周杰伦"}], "dt": 200000},
                {"id": 5, "name": "晴天", "ar": [{"name": "周杰伦"}]},
                {"id": 6, "name": "晴天", "ar": [{"name": "周杰伦"}], "dt": 182000}]}})
        calls.append(request.content)
        return httpx.Response(200, json={"code": 200, "lrc": {"lyric": "[02:59.00]匹配"}})
    fake_http(monkeypatch, provider, handle)
    monkeypatch.setattr("songseek.providers.netease.wait_for_request", lambda: None)
    result = service.lyrics.fetch(track, service.candidate(track.chosen_candidate_id))
    assert result.source == "netease"
    assert len(calls) == 1 and b"id=6" in calls[0]


def test_unsynced_returned_only_after_other_sources_miss(service, track, monkeypatch):
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: Lyrics("plain lyrics"))
    result = service.lyrics.preview(track.chosen_candidate_id)
    assert result.lrc.endswith("plain lyrics\n") and not result.synced


def test_timeout_and_failed_preview_cached_as_null(service, track, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def slow(raw):
        calls.append(raw.source_id)
        entered.set()
        release.wait(2)
        return Lyrics("[00:01]too late")
    monkeypatch.setattr(service.providers["mock"], "lyrics", slow)
    monkeypatch.setattr(lyrics_module, "LYRICS_TIMEOUT", .05)
    started = time.monotonic()
    try:
        result = service.lyrics.preview(track.chosen_candidate_id)
        assert entered.is_set() and result.lrc is None
        assert time.monotonic() - started < .5
        assert service.lyrics.preview(track.chosen_candidate_id).lrc is None
        assert len(calls) == 1
    finally:
        release.set()


def test_download_saves_sidecar_after_actual_fallback(service, track, tmp_path, monkeypatch):
    alternate = add_candidate(service, track, "other", "fallback")
    def download(target, raw, provider, playlist, on_progress=None):
        if raw.provider == "mock":
            raise ValueError("first source unavailable")
        return downloaded(service, target, tmp_path).file
    monkeypatch.setattr(service.downloader, "download", download)
    monkeypatch.setattr(service.providers["other"], "lyrics", lambda raw: Lyrics("[00:01]actual fallback"))
    result = service.download_track(track.id)
    assert result.status == "done" and result.chosen_candidate_id == alternate.id
    assert result.file.lyrics.source == "other"
    path = Path(result.file.lyrics.path)
    assert path == Path(result.file.path).with_suffix(".lrc")
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_text().endswith("[00:01.000]actual fallback\n")
    assert not list(tmp_path.glob(".lyrics-*"))


@pytest.mark.parametrize("failure", ["provider", "write", "embed"])
def test_lyrics_failures_never_fail_download(service, track, tmp_path, monkeypatch, failure):
    def fail(*args):
        raise OSError("private upstream details")
    monkeypatch.setattr(service.downloader, "download", lambda target, *args, **kwargs: downloaded(service, target, tmp_path).file)
    monkeypatch.setattr(service.providers["mock"], "lyrics", fail if failure == "provider" else lambda raw: Lyrics("[00:01]歌词"))
    if failure == "write":
        monkeypatch.setattr(lyrics_module, "atomic_lrc", fail)
    if failure == "embed":
        service.config.settings.lyrics_embed = True
        monkeypatch.setattr(lyrics_module, "embed_file", fail)
    result = service.download_track(track.id)
    assert result.status == "done" and result.error is None
    assert result.file.lyrics.error and "private" not in result.file.lyrics.error
    assert bool(result.file.lyrics.path) == (failure == "embed")


def test_settings_disabled_and_translation(service, track, tmp_path, monkeypatch):
    assert Settings().lyrics and not Settings().lyrics_embed and not Settings().lyrics_translation
    track = downloaded(service, track, tmp_path)
    calls = []
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: calls.append(raw.source_id) or Lyrics("[00:01]original", "[00:01]译文"))
    first = service.lyrics_track(track.id)
    assert not first.file.lyrics.translated
    assert "译文" not in Path(first.file.lyrics.path).read_text()
    service.config.settings.lyrics_translation = True
    second = service.lyrics_track(track.id, force=True)
    assert second.file.lyrics.translated
    assert "[00:01.000]original\n[00:01.000]译文" in Path(second.file.lyrics.path).read_text()
    service.lyrics.preview(track.chosen_candidate_id)
    before = len(calls)
    service.config.settings.lyrics = False
    assert service.lyrics.preview(track.chosen_candidate_id).lrc is None
    assert service.lyrics.read(second).lrc is None
    service.lyrics_track(track.id, force=True)
    assert len(calls) == before


@pytest.mark.parametrize("extension,codec", [("mp3", "libmp3lame"), ("flac", "flac"), ("ogg", "vorbis"), ("m4a", "aac")])
def test_embed_real_audio_tags_and_refresh_digest(tmp_path, extension, codec):
    path = tmp_path / f"audio.{extension}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-ac", "2", "-c:a", codec,
                    "-strict", "-2", str(path)], check=True, capture_output=True)
    file = DownloadedFile(path=str(path), size=path.stat().st_size, format=extension, bitrate=128, duration=1, sha256=digest(path))
    old_digest = file.sha256
    embed_file(file, "[ti:标题]\n[00:00.100]hello\n[00:00.200]世界\n")
    audio = mutagen.File(path)
    if extension == "mp3":
        assert audio.tags.getall("USLT")[0].text == "hello\n世界"
        assert audio.tags.getall("SYLT")[0].text == [("hello", 100), ("世界", 200)]
    elif extension == "m4a":
        assert audio.tags["©lyr"] == ["hello\n世界"]
    else:
        assert "[00:00.200]世界" in audio.tags["LYRICS"][0]
    assert file.sha256 != old_digest and file.sha256 == digest(path)
    assert file.size == path.stat().st_size


def test_failed_embedding_preserves_audio(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"original")
    file = DownloadedFile(path=str(path), size=8, format="mp3", bitrate=128, duration=1, sha256=digest(path))
    def broken(temporary, lrc):
        temporary.write_bytes(b"corrupted")
        raise OSError("failure")
    monkeypatch.setattr(lyrics_module, "embed_lyrics", broken)
    with pytest.raises(OSError):
        embed_file(file, "[00:01]hello")
    assert path.read_bytes() == b"original"
    assert file.sha256 == digest(path) and not list(tmp_path.glob(".lyrics-*"))


def test_api_jobs_events_csv_and_force(service, track, tmp_path, monkeypatch):
    track = downloaded(service, track, tmp_path)
    calls = []
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: calls.append(raw.source_id) or Lyrics("[00:01]歌词"))
    listener = service.events.subscribe()
    with TestClient(create_app(service=service)) as client:
        response = client.get(f"/api/candidates/{track.chosen_candidate_id}/lyrics")
        assert response.status_code == 200
        assert set(response.json()) == {"lrc", "synced", "source", "source_label", "translation"}
        result = client.post(f"/api/tracks/{track.id}/lyrics").json()
        assert result["file"]["lyrics"]["path"].endswith(".lrc")
        before = len(calls)
        saved = client.get(f"/api/tracks/{track.id}/lyrics").json()
        assert saved["lrc"] == Path(result["file"]["lyrics"]["path"]).read_text()
        assert len(calls) == before
        client.post(f"/api/tracks/{track.id}/lyrics")
        assert len(calls) == before
        job = client.post(f"/api/playlists/{track.playlist_id}/lyrics").json()
        assert job["kind"] == "lyrics" and job["total"] == 0
        service.jobs.wait(job["id"])
        job = client.post(f"/api/playlists/{track.playlist_id}/lyrics", json={"force": True}).json()
        final = service.jobs.wait(job["id"])
        assert final.status == "done" and final.completed == 1 and final.failed == 0
        assert len(calls) == before + 1
        rows = list(csv.DictReader(io.StringIO(service.export_csv(track.playlist_id).lstrip("\ufeff"))))
        assert rows[0]["歌词"] == result["file"]["lyrics"]["path"]
        events = []
        while not listener.empty():
            events.append(listener.get())
        assert any('"lyrics"' in event and '"file"' in event for event in events)
        assert client.put("/api/settings", json={"lyrics": False, "lyrics_embed": True, "lyrics_translation": True}).status_code == 200
        assert client.get(f"/api/candidates/{track.chosen_candidate_id}/lyrics").json()["lrc"] is None
        assert client.get("/api/candidates/9999/lyrics").status_code == 404


def test_backfill_misses_count_failed_without_changing_download(service, track, tmp_path):
    track = downloaded(service, track, tmp_path)
    job = service.jobs.start("lyrics", track.playlist_id)
    final = service.jobs.wait(job.id)
    assert final.status == "done" and final.completed == final.failed == 1
    result = service.track(track.id)
    assert result.status == "done" and result.error is None
    assert result.file.lyrics.path is None and result.file.lyrics.error == "没有找到匹配的歌词"


def test_missing_sidecar_is_backfilled(service, track, tmp_path, monkeypatch):
    track = downloaded(service, track, tmp_path)
    track.file.lyrics = LyricsInfo(path=str(tmp_path / "missing.lrc"), source="mock", source_label="mock")
    service.save_track(track)
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: Lyrics("[00:01]found"))
    job = service.jobs.start("lyrics", track.playlist_id)
    assert service.jobs.wait(job.id).completed == 1
    assert Path(service.track(track.id).file.lyrics.path).is_file()


def test_disabled_download_does_not_request_lyrics(service, track, tmp_path, monkeypatch):
    service.config.settings.lyrics = False
    monkeypatch.setattr(service.downloader, "download", lambda target, *args, **kwargs: downloaded(service, target, tmp_path).file)
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: pytest.fail("lyrics disabled"))
    result = service.download_track(track.id)
    assert result.status == "done" and result.file.lyrics is None


def test_atomic_sidecar_failure_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "song.lrc"
    path.write_text("previous")
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(lyrics_module.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_lrc(path, "replacement")
    assert path.read_text() == "previous" and not list(tmp_path.glob(".lyrics-*"))


def test_preview_http_failure_tries_next_provider(service, track, monkeypatch):
    provider = NeteaseProvider(service.config)
    service.providers["mock"] = provider
    fake_http(monkeypatch, provider, lambda request: httpx.Response(500))
    monkeypatch.setattr("songseek.providers.netease.wait_for_request", lambda: None)
    add_candidate(service, track, "other", "next")
    monkeypatch.setattr(service.providers["other"], "lyrics", lambda raw: Lyrics("[00:01]fallback"))
    assert service.lyrics.preview(track.chosen_candidate_id).source == "other"


def test_cancel_backfill_finishes_current_and_preserves_other_tracks(service, track, tmp_path, monkeypatch):
    first = downloaded(service, track, tmp_path)
    second = service._add_track(track.playlist_id, ParsedRow(title="另一首", artist="歌手", duration=180), 2)
    second = service.search_track(second.id)["track"]
    downloaded(service, second, tmp_path)
    service.config.settings.download_concurrency = 1
    entered, release = threading.Event(), threading.Event()
    original = service.lyrics_track
    def operation(identifier, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(identifier, **kwargs)
    monkeypatch.setattr(service, "lyrics_track", operation)
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: Lyrics("[00:01]歌词"))
    job = service.jobs.start("lyrics", track.playlist_id)
    assert entered.wait(3)
    service.jobs.cancel(job.id)
    release.set()
    final = service.jobs.wait(job.id)
    assert final.status == "cancelled" and final.completed == 1
    assert service.track(first.id).file.lyrics.path
    assert service.track(second.id).status == "done" and service.track(second.id).file.lyrics is None


def test_get_missing_saved_lyrics_uses_actual_duration_and_cache(service, track, tmp_path, monkeypatch):
    track = downloaded(service, track, tmp_path)
    track.file.duration = 100
    service.save_track(track)
    calls = []
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: calls.append(raw.source_id) or Lyrics("[02:00]too long for file"))
    assert service.lyrics.preview(track.chosen_candidate_id).lrc is not None
    assert service.lyrics.read(track).lrc is None
    assert service.lyrics.read(track).lrc is None
    assert len(calls) == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_keyword_fallback_after_vkeys_505(service, track, tmp_path, monkeypatch, lrclib_http, enabled):
    track = downloaded(service, track, tmp_path)
    track.file.duration = 269.7
    chosen = add_candidate(service, track, "vkeys_qq", "jay")
    provider = VKeysQQProvider(service.config)
    service.providers["vkeys_qq"] = provider
    calls = []
    def limited(request):
        calls.append("vkeys")
        return httpx.Response(200, json={"code": 505})
    fake_http(monkeypatch, provider, limited)
    kugou = KugouProvider(service.config)
    service.providers["kugou"] = kugou
    service.config.providers.append(ProviderInfo(id="kugou", label="酷狗", max_quality="320k", order=2, enabled=enabled))
    def keyword(request):
        calls.append(request.url.path)
        if request.url.path == "/search":
            assert request.url.params["hash"] == ""
            assert request.url.params["keyword"] == "周杰伦 - 晴天"
            assert request.url.params["duration"] == "269700"
            return httpx.Response(200, json={"candidates": [
                {"song": "晴天 (Live)", "singer": "周杰伦", "duration": 269700},
                {"song": "晴天", "singer": "别人", "duration": 269700},
                {"song": "另一首", "singer": "周杰伦", "duration": 269700},
                {"song": "晴天", "singer": "周杰伦", "duration": 263699},
                {"song": "晴天", "singer": "周杰伦", "duration": "invalid"},
                {"song": "晴天", "singer": "周杰伦", "duration": 265070, "id": "good", "accesskey": "fake"}]})
        assert request.url.params["id"] == "good"
        return httpx.Response(200, json={"content": base64.b64encode(b"[00:01]keyword").decode()})
    fake_http(monkeypatch, kugou, keyword)
    def lrclib(request):
        calls.append("lrclib")
        return httpx.Response(200, json={"syncedLyrics": "[00:01]lrclib"})
    lrclib_http(lrclib)
    result = service.lyrics.fetch(track, chosen)
    assert result.synced
    assert (result.source, result.source_label) == (("kugou", "酷狗") if enabled else ("lrclib", "LRCLIB"))
    assert calls == (["vkeys", "/search", "/download"] if enabled else ["vkeys", "lrclib"])


@pytest.mark.parametrize("payload,synced", [
    ({"syncedLyrics": "[00:01]synced", "plainLyrics": "plain"}, True),
    ({"plainLyrics": "plain"}, False),
    ({"instrumental": True, "syncedLyrics": "[00:01]instrumental"}, None),
])
def test_lrclib_get(track, lrclib_http, payload, synced):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.path == "/api/get"
        assert dict(request.url.params) == {"track_name": "晴天", "artist_name": "周杰伦", "album_name": "", "duration": "269"}
        assert request.headers["User-Agent"] == USER_AGENT
        return httpx.Response(200, json=payload)
    lrclib_http(handle)
    result = LRCLib().lyrics(track, 269.7, time.monotonic() + 8)
    assert len(calls) == 1
    if synced is None:
        assert result is None
    else:
        assert result.lrc == (payload["syncedLyrics"] if synced else payload["plainLyrics"])


def test_lrclib_search_filters_and_prefers_synced(track, lrclib_http):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/get":
            return httpx.Response(404)
        assert dict(request.url.params) == {"track_name": "晴天", "artist_name": "周杰伦"}
        base = {"trackName": "晴天", "artistName": "周杰伦", "duration": 180, "syncedLyrics": "[00:01]wrong"}
        invalid = [{"trackName": "晴天 (Live)"}, {"trackName": "另一首"}, {"artistName": "别人"},
                   {"duration": 186.01}, {"duration": None}, {"duration": "nan"}, {"instrumental": True}]
        return httpx.Response(200, json=[{**base, **change} for change in invalid] + [
            {**base, "syncedLyrics": None, "plainLyrics": "plain"},
            {**base, "duration": 174, "syncedLyrics": "[00:01]accepted"}])
    lrclib_http(handle)
    assert LRCLib().lyrics(track, 180, time.monotonic() + 8).lrc == "[00:01]accepted"
    assert calls == ["/api/get", "/api/search"]


@pytest.mark.parametrize("proxy,error", [("http://proxy.invalid:8080", True), ("", True), ("http://proxy.invalid:8080", False)])
def test_lrclib_direct_then_proxy(track, monkeypatch, proxy, error):
    routes = []
    def client(self, route, timeout):
        routes.append(route)
        assert 0 < timeout <= 3
        def handle(request):
            if error and route is None:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(404 if request.url.path == "/api/get" else 200, json=[])
        return httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)
    monkeypatch.setattr(LRCLib, "client", client)
    source = LRCLib(proxy)
    if error and not proxy:
        with pytest.raises(httpx.ConnectError):
            source.lyrics(track, 180, time.monotonic() + 8)
        assert routes == [None]
    else:
        assert source.lyrics(track, 180, time.monotonic() + 8) is None
        assert routes == ([None, proxy, None, proxy] if error else [None, None])


def test_lrclib_synced_beats_provider_plain(service, track, monkeypatch, lrclib_http):
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: Lyrics("plain"))
    lrclib_http(lambda request: httpx.Response(200, json={"syncedLyrics": "[00:01]synced"}))
    result = service.lyrics.preview(track.chosen_candidate_id)
    assert result.synced and result.source == "lrclib" and result.source_label == "LRCLIB"


def test_save_negative_result_reused_by_read(service, track, tmp_path, monkeypatch, lrclib_http):
    track = downloaded(service, track, tmp_path)
    calls = []
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: calls.append("provider"))
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(404 if request.url.path == "/api/get" else 200, json=[])
    lrclib_http(handle)
    assert service.lyrics.save(track).error == "没有找到匹配的歌词"
    assert service.lyrics.read(track).lrc is None
    assert service.lyrics.preview(track.chosen_candidate_id, downloaded_track=track).lrc is None
    assert calls == ["provider", "/api/get", "/api/search"]


def test_lrclib_uses_remaining_total_budget(service, track, monkeypatch, lrclib_http):
    entered, release = threading.Event(), threading.Event()
    def handle(request):
        entered.set()
        release.wait(2)
        return httpx.Response(404)
    lrclib_http(handle)
    monkeypatch.setattr(lyrics_module, "LYRICS_TIMEOUT", .05)
    started = time.monotonic()
    try:
        assert service.lyrics.preview(track.chosen_candidate_id).lrc is None
        assert entered.is_set() and time.monotonic() - started < .5
    finally:
        release.set()


def test_misses_expire_sooner_than_hits(service, track, monkeypatch):
    from songseek.lyrics import CACHE_TTL, MISS_TTL

    answers = iter([LyricsResponse(), LyricsResponse(lrc="[00:01.00]x\n", synced=True)])
    monkeypatch.setattr(service.lyrics, "fetch", lambda *_: next(answers))
    assert service.lyrics.preview(track.chosen_candidate_id).lrc is None
    (expires, _), = service.lyrics.cache.values()
    assert expires - time.monotonic() <= MISS_TTL < CACHE_TTL
    service.lyrics.cache = {key: (time.monotonic() - 1, value) for key, (_, value) in service.lyrics.cache.items()}
    assert service.lyrics.preview(track.chosen_candidate_id).synced
    (expires, _), = service.lyrics.cache.values()
    assert expires - time.monotonic() > MISS_TTL


@pytest.mark.parametrize("setting,wanted,expected", [(False, None, False), (True, None, True),
    (False, True, True), (True, True, True), (False, False, False), (True, False, False)])
def test_track_lyrics_precedence(service, track, tmp_path, monkeypatch, setting, wanted, expected):
    service.config.settings.lyrics = setting
    track.lyrics_wanted = wanted
    service.save_track(track)
    file = downloaded(service, track.model_copy(deep=True), tmp_path).file
    track.status = "matched"
    service.save_track(track)
    monkeypatch.setattr(service.downloader, "download", lambda *a, **kw: file)
    calls = []
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: calls.append(raw) or Lyrics("[00:01]歌词"))
    result = service.download_track(track.id)
    assert result.status == "done"
    assert bool(calls) is expected
    assert bool(result.file.lyrics and result.file.lyrics.path) is expected


def test_explicit_lyrics_works_with_global_and_track_off(service, track, tmp_path, monkeypatch):
    track = downloaded(service, track, tmp_path)
    track.lyrics_wanted = False
    service.save_track(track)
    service.config.settings.lyrics = False
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: Lyrics("[00:01]明确请求"))
    with TestClient(create_app(service=service)) as client:
        response = client.post(f"/api/tracks/{track.id}/lyrics")
        assert response.status_code == 200
        assert Path(response.json()["file"]["lyrics"]["path"]).is_file()


def test_patch_lyrics_keeps_candidates_and_persists(service, track):
    with TestClient(create_app(service=service)) as client:
        for wanted in (True, False, None):
            response = client.patch(f"/api/tracks/{track.id}", json={"lyrics_wanted": wanted})
            assert response.status_code == 200
            value = response.json()
            assert value["lyrics_wanted"] is wanted
            assert value["chosen_candidate_id"] == track.chosen_candidate_id
            assert value["status"] == "matched" and value["candidate_count"] == track.candidate_count
            assert service.db.get("tracks", track.id)["lyrics_wanted"] is wanted


@pytest.mark.parametrize("setting,wanted,total", [(True, False, 0), (False, True, 1), (False, None, 0)])
def test_batch_lyrics_obeys_track_choice(service, track, tmp_path, monkeypatch, setting, wanted, total):
    track = downloaded(service, track, tmp_path)
    track.lyrics_wanted = wanted
    service.save_track(track)
    service.config.settings.lyrics = setting
    monkeypatch.setattr(service.providers["mock"], "lyrics", lambda raw: Lyrics("[00:01]批量"))
    job = service.jobs.start("lyrics", track.playlist_id)
    assert job.total == total
    assert service.jobs.wait(job.id).failed == 0


@pytest.mark.parametrize("body,expected", [({}, True), ({"lyrics": False}, False), ({"lyrics": None}, None)])
def test_download_body_applies_before_queue(service, track, monkeypatch, body, expected):
    track.lyrics_wanted = True
    service.save_track(track)
    observed = []
    monkeypatch.setattr(service, "download_track", lambda track_id, **kw: observed.append(service.track(track_id).lyrics_wanted))
    with TestClient(create_app(service=service)) as client:
        response = client.post(f"/api/playlists/{track.playlist_id}/download", json={"track_ids": [track.id], **body})
        assert response.status_code == 200
        service.jobs.wait(response.json()["id"])
        assert observed == [expected]


def test_legacy_track_json_migrates(service, track):
    from songseek.db import Database
    old = track.model_dump(exclude={"lyrics_wanted"})
    service.db.save("tracks", old)
    reopened = Database(service.config.home / "songseek.db")
    try:
        assert reopened.get("tracks", track.id)["lyrics_wanted"] is None
    finally:
        reopened.close()
