import subprocess
from pathlib import Path

import httpx
import mutagen
import pytest

from songseek.downloader import digest, publish, safe_name, tag, transfer, verify
from songseek.errors import SongSeekError
from songseek.models import AudioSource, DownloadedFile, ParsedRow


def searched(service, duration=180):
    playlist = service.create_playlist("测试", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=duration)])
    track = service.tracks(playlist.id)[0]
    result = service.search_track(track.id)
    return result["track"], result["candidates"]


@pytest.mark.parametrize("duration", [180, None])
def test_selected_failure_falls_back_to_same_recording(service, monkeypatch, duration):
    track, candidates = searched(service, duration)
    matches = [c for c in candidates if c.checks[1].ok and c.duration == 180 and not c.flags]
    assert len(matches) == 1
    service.choose(track.id, matches[0].id)
    alternate = matches[0].model_copy(deep=True)
    alternate.id = 0
    alternate.provider = "other"
    alternate.source_id = "alternate"
    service.providers["other"] = service.providers["mock"]
    raw = service.db.raw_candidate(matches[0].id) | {"provider": "other", "source_id": "alternate"}
    import json
    alternate.id = service.db.insert("candidates", alternate.model_dump(), track_id=track.id, provider="other", source_id="alternate", raw=json.dumps(raw))
    calls = []
    def download(target, raw, provider, playlist_name):
        calls.append(raw.source_id)
        if len(calls) == 1:
            raise SongSeekError("取流失败")
        return DownloadedFile(path="/tmp/test.mp3", size=1, format="mp3", bitrate=192, duration=180, sha256="test")
    monkeypatch.setattr(service.downloader, "download", download)
    result = service.download_track(track.id)
    assert result.status == "done"
    assert calls == [matches[0].source_id, "alternate"]
    assert result.chosen_candidate_id == alternate.id
    assert result.chosen_by == "auto"


def test_never_falls_back_to_unrelated_review(service, monkeypatch):
    track, candidates = searched(service)
    calls = []
    def fail(target, raw, provider, playlist_name):
        calls.append(raw.source_id)
        raise SongSeekError("取流失败")
    monkeypatch.setattr(service.downloader, "download", fail)
    result = service.download_track(track.id)
    assert result.status == "failed"
    assert len(calls) == 1
    assert len(result.error) <= 300 and "取流失败" in result.error


@pytest.fixture
def tone(tmp_path):
    path = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-b:a", "128k", str(path)], check=True, capture_output=True)
    return path


def test_duration_mismatch_blocks_publish(service, tone, monkeypatch):
    track, candidates = searched(service)
    provider = service.providers["mock"]
    monkeypatch.setattr(provider, "resolve", lambda raw: AudioSource(tone.as_uri(), local_path=str(tone)))
    result = service.download_track(track.id)
    assert result.status == "failed" and "时长" in result.error
    output = Path(service.config.settings.output_dir)
    assert not list(output.glob("*.mp3"))
    assert not list(output.glob("*.part"))


def test_full_decode_and_id3(tone, service):
    info = verify(tone, 1)
    assert info["format"] == "mp3" and info["bitrate"] == 128
    track, candidates = searched(service)
    raw = service.raw_candidate(candidates[0].id)
    tag(tone, track, raw)
    tags = mutagen.File(tone).tags
    assert str(tags["TIT2"]) == "晴天"
    assert str(tags["TXXX:SOURCE"]).startswith("provider=mock; id=")


@pytest.mark.parametrize("mask, mode", [(0o022, 0o644), (0o002, 0o644), (0o027, 0o640), (0o077, 0o600)])
def test_download_actual_quality_and_file_permissions(tone, service, monkeypatch, mask, mode):
    import os
    target, candidates = searched(service, 1)
    raw = service.raw_candidate(candidates[0].id)
    raw.provider = "vkeys_qq"
    provider = service.providers["mock"]
    monkeypatch.setattr(provider, "resolve", lambda raw: AudioSource(tone.as_uri(), local_path=str(tone), bitrate=319))
    previous = os.umask(mask)
    try:
        result = service.downloader.download(target, raw, provider, "测试")
    finally:
        os.umask(previous)
    assert result.bitrate == 128 and result.format == "mp3"
    assert Path(result.path).stat().st_mode & 0o777 == mode


def test_atomic_publish_duplicates_and_different_bytes(tmp_path, monkeypatch):
    target = tmp_path / "song.mp3"
    target.write_bytes(b"existing")
    partial = tmp_path / "audio.part"
    partial.write_bytes(b"different")
    import songseek.downloader as module
    original = module.os.replace
    calls = []
    def replace(source, destination):
        assert target.read_bytes() == b"existing"
        calls.append((source, destination))
        original(source, destination)
    monkeypatch.setattr(module.os, "replace", replace)
    second = publish(partial, target)
    assert second.name == "song (2).mp3" and len(calls) == 1
    partial.write_bytes(b"different")
    assert publish(partial, target) == second
    assert len(calls) == 1
    assert digest(second) != digest(target)


def test_file_name_sanitization():
    assert safe_name('../A:B\\C?*"<>|') == "_A_B_C______"
    assert safe_name("CON") == "_CON"
    assert len(safe_name("歌" * 500).encode()) <= 180


def test_transfer_sends_referer_and_range_and_rejects_partial(config, tmp_path, monkeypatch):
    from songseek.providers.zpod import ZpodProvider
    provider = ZpodProvider(config)
    def handler(request):
        assert request.headers["Range"] == "bytes=0-"
        assert request.headers["Referer"] == "https://zpod.net/"
        return httpx.Response(206, headers={"content-range": "bytes 0-1/10"}, content=b"ab")
    monkeypatch.setattr(provider, "client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SongSeekError, match="部分"):
        transfer(AudioSource("https://audio.example/song", {"Range": "bytes=0-"}), tmp_path / "x.part", provider)


@pytest.mark.parametrize("ext, codec", [("m4a", "aac"), ("flac", "flac"), ("opus", "libopus")])
def test_tags_on_other_containers(service, tmp_path, ext, codec):
    path = tmp_path / f"tone.{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.2", "-c:a", codec, str(path)], check=True, capture_output=True)
    target, candidates = searched(service)
    tag(path, target, service.raw_candidate(candidates[0].id))
    audio = mutagen.File(path)
    assert audio["\xa9nam" if ext == "m4a" else "title"] == ["晴天"]
    assert verify(path)["duration"] > 0


def test_preview_cache_evicts_oldest(config, tone):
    import os
    from songseek.downloader import PreviewCache
    from songseek.models import RawCandidate
    from songseek.providers.mock import MockProvider
    cache = PreviewCache(config, limit=tone.stat().st_size * 2)
    provider = MockProvider(config)
    paths = []
    for i in range(3):
        raw = RawCandidate("mock", str(i), "晴天")
        path = cache.get(raw, AudioSource(tone.as_uri(), local_path=str(tone)), provider)
        os.utime(path, (i + 1, i + 1))
        paths.append(path)
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()
    assert sum(p.stat().st_size for p in cache.directory.iterdir()) <= cache.limit


def test_video_audio_remux_is_lossless_container_change(tmp_path):
    from songseek.downloader import probe, remux
    path = tmp_path / "fragment.part"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.2", "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", str(path)], check=True, capture_output=True)
    before = probe(path)
    remux(path, "m4a")
    after = verify(path)
    assert before["codec"] == after["codec"] == "aac"
    assert after["format"] == "m4a"
    assert abs(before["duration"] - after["duration"]) < .1


def test_decoded_duration_mismatch_falls_back_to_match(service, tone, monkeypatch):
    import json
    import songseek.downloader as module
    target, candidates = searched(service)
    first = next(c for c in candidates if c.verdict == "match")
    alternate = first.model_copy(update={"id": 0, "source_id": "alternate"})
    raw = service.db.raw_candidate(first.id) | {"source_id": "alternate"}
    alternate.id = service.db.insert("candidates", alternate.model_dump(), track_id=target.id, provider="mock", source_id="alternate", raw=json.dumps(raw))
    calls = []
    def resolve(raw):
        calls.append(raw.source_id)
        return AudioSource(tone.as_uri(), local_path=str(tone))
    monkeypatch.setattr(service.providers["mock"], "resolve", resolve)
    actual_probe = module.probe
    def probe(path):
        return actual_probe(path) | {"duration": 191 if len(calls) == 1 else 180}
    monkeypatch.setattr(module, "probe", probe)
    result = service.download_track(target.id)
    assert result.status == "done" and result.chosen_candidate_id == alternate.id
    assert calls == [first.source_id, "alternate"]
    assert result.file.duration == 180
    assert len(list(Path(service.config.settings.output_dir).glob("*.mp3"))) == 1


def test_repeated_download_errors_are_deduplicated(service, monkeypatch):
    import json
    target, candidates = searched(service)
    first = next(c for c in candidates if c.verdict == "match")
    alternate = first.model_copy(update={"id": 0, "source_id": "alternate"})
    raw = service.db.raw_candidate(first.id) | {"source_id": "alternate"}
    service.db.insert("candidates", alternate.model_dump(), track_id=target.id, provider="mock", source_id="alternate", raw=json.dumps(raw))
    def fail(*args):
        raise SongSeekError("取流失败")
    monkeypatch.setattr(service.downloader, "download", fail)
    result = service.download_track(target.id)
    assert result.status == "failed"
    assert "试了 2 个候选" in result.error and result.error.count("取流失败") == 1


@pytest.mark.parametrize('failure', ['http', 'timeout', 'invalid', 'oversized', None])
def test_cover_fetch_is_best_effort(tone, service, monkeypatch, failure):
    target, candidates = searched(service, 1)
    raw = service.raw_candidate(candidates[0].id)
    raw.provider = 'vkeys_qq'
    raw.extra['cover_url'] = 'https://cover.test/image'
    provider = service.providers['mock']
    monkeypatch.setattr(provider, 'resolve', lambda raw: AudioSource(tone.as_uri(), local_path=str(tone)))
    cover = b'\xff\xd8\xff\xe0cover'
    def handle(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        if failure == 'http':
            return httpx.Response(403)
        if failure == 'invalid':
            return httpx.Response(200, content=b'<html>no cover</html>')
        if failure == 'oversized':
            return httpx.Response(200, content=cover, headers={'content-length': str(6 * 1024 * 1024)})
        return httpx.Response(200, content=cover)
    monkeypatch.setattr(provider, 'client', lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handle)))
    result = service.downloader.download(target, raw, provider, '测试')
    audio = mutagen.File(result.path)
    assert str(audio.tags['TIT2']) == target.title
    pictures = audio.tags.getall('APIC')
    assert bool(pictures) == (failure is None)
    if pictures:
        assert pictures[0].data == cover
