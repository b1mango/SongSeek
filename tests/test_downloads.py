import subprocess
import json
from pathlib import Path

import httpx
import mutagen
import pytest

from songseek.downloader import digest, publish, safe_name, tag, transfer, verify
from songseek.errors import SongSeekError
from songseek.models import AudioSource, DownloadedFile, ParsedRow, ProviderInfo


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
    service.config.providers.append(ProviderInfo(id="other", label="其他音源", max_quality="192k", order=1))
    raw = service.db.raw_candidate(matches[0].id) | {"provider": "other", "source_id": "alternate"}
    import json
    alternate.id = service.db.insert("candidates", alternate.model_dump(), track_id=track.id, provider="other", source_id="alternate", raw=json.dumps(raw))
    calls = []
    def download(target, raw, provider, playlist_name, on_progress=None, **kwargs):
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
    def fail(target, raw, provider, playlist_name, on_progress=None, **kwargs):
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


def test_full_decode_only_maps_audio(tone, monkeypatch):
    run = subprocess.run
    commands = []
    def capture(command, **kwargs):
        commands.append(command)
        return run(command, **kwargs)
    monkeypatch.setattr(subprocess, "run", capture)
    verify(tone)
    decode = commands[-1]
    assert decode[decode.index("-map") + 1] == "0:a"


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
    # 同一份音频已在目录里：直接复用，不再多出 (3)
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
    def fail(*args, **kwargs):
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


@pytest.mark.parametrize("status, headers, total", [
    (200, {"content-length": "524288"}, 524288),
    (206, {"content-range": "bytes 0-524287/524288"}, 524288),
    (200, {}, None),
])
def test_transfer_progress(service, tmp_path, monkeypatch, status, headers, total):
    provider = service.providers["mock"]
    content = b"a" * (512 * 1024)
    def handle(request):
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))
    monkeypatch.setattr(provider, "client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handle)))
    calls = []
    target = tmp_path / "audio.part"
    def progress(downloaded, size):
        calls.append((downloaded, size))
    transfer(AudioSource("https://audio.example/song"), target, provider, on_progress=progress)
    assert calls == [(256 * 1024, total), (512 * 1024, total)]
    assert target.read_bytes() == content


@pytest.mark.parametrize("total", [3000, None])
def test_track_progress_payload_and_throttle(service, monkeypatch, total):
    target, _ = searched(service)
    listener = service.events.subscribe()
    clock = [0.0]
    monkeypatch.setattr("songseek.service.time.monotonic", lambda: clock[0])
    def download(track, raw, provider, playlist_name, on_progress, **kwargs):
        for timestamp, downloaded in [(0, 1000), (.1, 1500), (.25, 2000), (.26, 3000)]:
            clock[0] = timestamp
            on_progress("download", downloaded, total)
        on_progress("finishing", 3000, total)
        return DownloadedFile(path="/tmp/test.mp3", size=3000, format="mp3", bitrate=192, duration=180, sha256="test")
    monkeypatch.setattr(service.downloader, "download", download)
    result = service.download_track(target.id)
    assert result.status == "done"
    events = []
    while not listener.empty():
        kind, data = listener.get_nowait().strip().split("\n")
        events.append((kind.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    progress = [data for kind, data in events if kind == "track_progress"]
    counts = [1000, 2000, 3000] if total else [1000, 2000]
    expected = [{"track_id": target.id, "playlist_id": target.playlist_id, "phase": "download",
                 "downloaded": count, "total": total, "fraction": round(count / total, 3) if total else None}
                for count in counts]
    expected.append({"track_id": target.id, "playlist_id": target.playlist_id, "phase": "finishing",
                     "downloaded": 3000, "total": total, "fraction": 1 if total else None})
    assert progress == expected
    # 音频完成先入库，歌词收尾再保存一次 Track。
    assert [data["status"] for kind, data in events if kind == "track"] == ["downloading", "done", "done"]
    assert next(i for i, event in enumerate(events) if event == ("track_progress", expected[-1])) < next(
        i for i, (kind, data) in enumerate(events) if kind == "track" and data["status"] == "done")


def test_downloader_progress_finishing_before_processing(service, tone, monkeypatch):
    import songseek.downloader as module
    target, candidates = searched(service, 1)
    raw = service.raw_candidate(candidates[0].id)
    raw.provider = "bilibili"
    provider = service.providers["mock"]
    monkeypatch.setattr(provider, "resolve", lambda raw: AudioSource(tone.as_uri(), local_path=str(tone), ext="m4a"))
    calls = []
    monkeypatch.setattr(module, "remux", lambda *args: calls.append("remux"))
    actual_verify = module.verify
    def verify(path, duration):
        calls.append("verify")
        return actual_verify(path, duration)
    monkeypatch.setattr(module, "verify", verify)
    service.downloader.download(target, raw, provider, "测试", on_progress=lambda *args: calls.append(args))
    size = tone.stat().st_size
    assert calls == [("download", size, size), ("finishing", size, size), "remux", "verify"]


def test_preview_cache_does_not_emit_progress(service, tone, monkeypatch):
    import songseek.downloader as module
    target, candidates = searched(service, 1)
    raw = service.raw_candidate(candidates[0].id)
    provider = service.providers["mock"]
    content = tone.read_bytes()
    monkeypatch.setattr(provider, "client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=content))))
    actual_transfer = module.transfer
    callbacks = []
    def transfer(source, target, provider, max_size, on_progress=None):
        callbacks.append(on_progress)
        return actual_transfer(source, target, provider, max_size, on_progress)
    monkeypatch.setattr(module, "transfer", transfer)
    listener = service.events.subscribe()
    path = service.previews.get(raw, AudioSource("https://audio.example/song"), provider)
    assert path.read_bytes() == content
    assert callbacks == [None]
    assert listener.empty()


def test_track_carries_snapshot_of_chosen_candidate(service):
    track, candidates = searched(service)
    pick = candidates[-1]
    chosen = service.choose(track.id, pick.id)
    assert chosen.chosen_by == "user"
    assert chosen.chosen is not None
    assert (chosen.chosen.id, chosen.chosen.provider_label, chosen.chosen.quality, chosen.chosen.score) == (pick.id, pick.provider_label, pick.quality.label, pick.score)
    assert service.track(track.id).chosen == chosen.chosen
    service.delete_track(track.id)
    assert service.db.list("candidates", "track_id", track.id) == []


def test_selected_live_failure_never_falls_back_to_original_match(service, monkeypatch):
    target, candidates = searched(service)
    live = next(c for c in candidates if c.flags == ["live"])
    service.choose(target.id, live.id)
    calls = []
    def fail(track, raw, *args, **kwargs):
        calls.append(raw.source_id)
        raise SongSeekError("取流失败，请稍后重试")
    monkeypatch.setattr(service.downloader, "download", fail)
    result = service.download_track(target.id)
    assert result.status == "failed" and result.chosen_candidate_id == live.id
    assert calls == [live.source_id]


def test_switch_version_failure_restores_original_file_and_selection(service, tmp_path, monkeypatch):
    target, candidates = searched(service)
    old_path = tmp_path / "original.mp3"
    old_path.write_bytes(b"original")
    target.status = "done"
    target.file = DownloadedFile(path=str(old_path), size=8, format="mp3", bitrate=192, duration=180, sha256="old")
    original = service.save_track(target).model_copy(deep=True)
    live = next(c for c in candidates if c.flags == ["live"])
    changed = service.choose(target.id, live.id)
    assert changed.status == "matched" and changed.file.path == str(old_path)
    def fail(*args, **kwargs):
        assert old_path.read_bytes() == b"original"
        raise SongSeekError("取流失败，请稍后重试")
    monkeypatch.setattr(service.downloader, "download", fail)
    result = service.download_track(target.id)
    assert result.status == "done" and result.file.path == original.file.path
    assert (result.chosen_candidate_id, result.chosen_by) == (original.chosen_candidate_id, original.chosen_by)
    assert result.error.startswith("换版本下载失败，已保留原文件：")
    assert old_path.read_bytes() == b"original"


def test_switch_version_success_replaces_record_only_after_publish(service, tmp_path, monkeypatch):
    target, candidates = searched(service)
    old = tmp_path / "original.mp3"
    old.write_bytes(b"original")
    target.status = "done"
    target.file = DownloadedFile(path=str(old), size=8, format="mp3", bitrate=192, duration=180, sha256="old")
    service.save_track(target)
    live = next(c for c in candidates if c.flags == ["live"])
    service.choose(target.id, live.id)
    service.config.settings.lyrics = False
    def download(track, *args, **kwargs):
        assert service.track(track.id).file.path == str(old)
        return DownloadedFile(path=str(tmp_path / "new.mp3"), size=3, format="mp3", bitrate=128, duration=180, sha256="new")
    monkeypatch.setattr(service.downloader, "download", download)
    result = service.download_track(target.id)
    assert result.status == "done" and result.chosen_candidate_id == live.id
    assert result.file.path.endswith("new.mp3") and old.read_bytes() == b"original"


def test_publish_reserves_stem_across_extensions_lyrics_and_identical_bytes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    (tmp_path / "song.lrc").write_text("existing lyrics")
    partials = [tmp_path / f"part-{i}.part" for i in range(3)]
    for path in partials:
        path.write_bytes(b"same audio")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(publish, path, tmp_path / f"song.{ext}") for path, ext in zip(partials, ["mp3", "flac", "mp3"])]
        published = [future.result() for future in futures]
    # the .lrc's stem stays taken; the two identical mp3s collapse into one file; the flac gets its own stem
    assert all(path.stem != "song" and path.read_bytes() == b"same audio" for path in published)
    assert published[0] == published[2] and published[1].stem != published[0].stem
    assert (tmp_path / "song.lrc").read_text() == "existing lyrics"


@pytest.mark.parametrize("failure", ["permission", "full", "not_directory", "publish"])
def test_local_storage_failures_stop_fallback_with_actionable_error(service, tmp_path, monkeypatch, failure):
    import errno
    import songseek.downloader as module
    target, candidates = searched(service)
    first = next(c for c in candidates if c.verdict == "match")
    alternate = first.model_copy(update={"id": 0, "source_id": "alternate"})
    raw = service.db.raw_candidate(first.id) | {"source_id": "alternate"}
    service.db.insert("candidates", alternate.model_dump(), track_id=target.id, provider="mock", source_id="alternate", raw=json.dumps(raw))
    calls = []
    monkeypatch.setattr(service.providers["mock"], "resolve", lambda raw: calls.append(raw.source_id) or AudioSource("mock"))
    if failure == "not_directory":
        output = tmp_path / "not-directory"
        output.write_text("file")
        service.config.settings.output_dir = str(output)
    elif failure == "permission":
        original = module.os.open
        def open_file(path, *args):
            if str(path).endswith(".part"):
                raise PermissionError(errno.EACCES, "private details")
            return original(path, *args)
        monkeypatch.setattr(module.os, "open", open_file)
    elif failure == "full":
        def transfer(*args, **kwargs):
            raise OSError(errno.ENOSPC, "private disk details")
        monkeypatch.setattr(module, "transfer", transfer)
    else:
        monkeypatch.setattr(module, "transfer", lambda source, path, *args, **kwargs: path.write_bytes(b"audio"))
        monkeypatch.setattr(module, "verify", lambda *args: {"format": "mp3", "bitrate": 128, "duration": 180})
        monkeypatch.setattr(module, "tag", lambda *args: None)
        def publish(*args, **kwargs):
            raise OSError(errno.EROFS, "private details")
        monkeypatch.setattr(module, "publish", publish)
    with pytest.raises(SongSeekError) as error:
        service.downloader.download(target, service.raw_candidate(first.id), service.providers["mock"], "测试")
    assert error.value.code == "storage_error" and "输出目录" in str(error.value)
    calls.clear()
    result = service.download_track(target.id)
    assert result.status == "failed" and "输出目录" in result.error
    assert "private" not in result.error and len(calls) <= 1
    assert not list(tmp_path.rglob(".songseek-*.part"))


def test_published_audio_is_saved_before_lyrics_and_lyrics_failure_keeps_done(service, monkeypatch):
    target, _ = searched(service)
    file = DownloadedFile(path="/tmp/new.mp3", size=3, format="mp3", bitrate=128, duration=180, sha256="audio")
    monkeypatch.setattr(service.downloader, "download", lambda *args, **kwargs: file)
    def fail(track):
        saved = service.track(track.id)
        assert saved.status == "done" and saved.file.path == file.path
        raise RuntimeError("private details")
    monkeypatch.setattr(service.lyrics, "save", fail)
    result = service.download_track(target.id)
    assert result.status == "done" and result.error is None
    assert result.file.lyrics.error and "private" not in result.file.lyrics.error


@pytest.mark.parametrize("restricted", ["disabled", "scope"])
def test_download_fallback_respects_enabled_and_requested_sources(service, monkeypatch, restricted):
    target, candidates = searched(service)
    first = next(c for c in candidates if c.verdict == "match")
    alternate = first.model_copy(update={"id": 0, "provider": "other", "source_id": "alternate"})
    raw = service.db.raw_candidate(first.id) | {"provider": "other", "source_id": "alternate"}
    service.db.insert("candidates", alternate.model_dump(), track_id=target.id, provider="other", source_id="alternate", raw=json.dumps(raw))
    service.providers["other"] = service.providers["mock"]
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="320k", order=1, enabled=restricted != "disabled"))
    target.providers = None
    service.save_track(target)
    calls = []
    def fail(track, raw, *args, **kwargs):
        calls.append(raw.provider)
        raise SongSeekError("取流失败，请稍后重试")
    monkeypatch.setattr(service.downloader, "download", fail)
    result = service.download_track(target.id, providers=["mock"] if restricted == "scope" else None)
    assert result.status == "failed" and calls == ["mock"]
    assert any(c.provider == "other" for c in service.candidates(target.id))


def test_remux_disk_full_is_storage_error(tmp_path, monkeypatch):
    from songseek.downloader import remux
    path = tmp_path / ".songseek-a.part"
    path.write_bytes(b"audio")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stderr=b"No space left on device"))
    with pytest.raises(SongSeekError) as error:
        remux(path, "m4a")
    assert error.value.code == "storage_error" and "清理磁盘空间" in str(error.value)
    assert path.read_bytes() == b"audio"


def test_tag_wrapped_disk_error_is_storage_error(tone, service, monkeypatch):
    import errno
    from mutagen import MutagenError
    track, candidates = searched(service)
    audio = mutagen.File(tone)
    def save():
        raise MutagenError(OSError(errno.ENOSPC, "private details"))
    monkeypatch.setattr(audio, "save", save)
    monkeypatch.setattr(mutagen, "File", lambda *args: audio)
    with pytest.raises(SongSeekError) as error:
        tag(tone, track, service.raw_candidate(candidates[0].id))
    assert error.value.code == "storage_error" and "清理磁盘空间" in str(error.value)


def test_download_rechecks_sources_disabled_mid_attempt(service, monkeypatch):
    target, candidates = searched(service)
    chosen = next(c for c in candidates if c.verdict == "match")
    alternate = chosen.model_copy(update={"id": 0, "source_id": "alternate"})
    raw = service.db.raw_candidate(chosen.id) | {"source_id": "alternate"}
    service.db.insert("candidates", alternate.model_dump(), track_id=target.id, provider="mock", source_id="alternate", raw=json.dumps(raw))
    calls = []
    def fail(track, raw, *args, **kwargs):
        calls.append(raw.source_id)
        service.config.providers[0].enabled = False
        raise SongSeekError("取流失败，请启用音源后重试")
    monkeypatch.setattr(service.downloader, "download", fail)
    assert service.download_track(target.id).status == "failed"
    assert calls == [chosen.source_id]


@pytest.mark.parametrize("grouped", [False, True])
def test_startup_recovers_published_audio_without_database_record(service, grouped):
    target, candidates = searched(service)
    service.config.settings.group_by_playlist = grouped
    chosen = next(c for c in candidates if c.verdict == "match")
    published = service.downloader.download(target, service.raw_candidate(chosen.id), service.providers["mock"], "测试",
        on_publish=lambda path, sha: service.db.prepare_publication(target.id, chosen.id, str(path.resolve()), sha))
    # Simulate a hard stop after publish(), before download_track() can save the DownloadedFile.
    target = service.track(target.id)
    target.status, target.file = "downloading", None
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    recovered = service.track(target.id)
    assert recovered.status == "done" and recovered.error is None
    assert recovered.file.path == published.path and Path(recovered.file.path).is_file()
    assert recovered.file.candidate_id == chosen.id and recovered.file.chosen_by == "auto"
    assert recovered.file.duration == 180 and recovered.file.sha256 == published.sha256


def test_startup_recovers_same_recording_fallback_candidate(service):
    target, candidates = searched(service)
    chosen = next(c for c in candidates if c.verdict == "match")
    alternate = chosen.model_copy(update={"id": 0, "source_id": "alternate"})
    raw = service.db.raw_candidate(chosen.id) | {"source_id": "alternate"}
    alternate.id = service.db.insert("candidates", alternate.model_dump(), track_id=target.id, provider="mock", source_id="alternate", raw=json.dumps(raw))
    published = service.downloader.download(target, service.raw_candidate(alternate.id), service.providers["mock"], "测试",
        on_publish=lambda path, sha: service.db.prepare_publication(target.id, alternate.id, str(path.resolve()), sha))
    target = service.track(target.id)
    target.status, target.file = "downloading", None
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    recovered = service.track(target.id)
    assert recovered.status == "done" and recovered.file.path == published.path
    assert recovered.chosen_candidate_id == alternate.id and recovered.chosen_by == "auto"
    assert recovered.file.candidate_id == alternate.id


def test_startup_recovers_exact_chosen_candidate_without_artist(service):
    target, candidates = searched(service)
    chosen = next(c for c in candidates if c.verdict == "match")
    chosen.artist = ""
    service.db.save("candidates", chosen.model_dump())
    published = service.downloader.download(target, service.raw_candidate(chosen.id), service.providers["mock"], "测试",
        on_publish=lambda path, sha: service.db.prepare_publication(target.id, chosen.id, str(path.resolve()), sha))
    target.status, target.file = "downloading", None
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    recovered = service.track(target.id)
    assert recovered.status == "done" and recovered.file.path == published.path
    assert recovered.chosen_candidate_id == chosen.id


def test_startup_does_not_claim_untagged_same_name_file(service):
    target, _ = searched(service)
    path = Path(service.config.settings.output_dir) / "周杰伦 - 晴天.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"an unrelated user file")
    target.status, target.file = "downloading", None
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    recovered = service.track(target.id)
    assert recovered.status == "matched" and recovered.file is None
    assert recovered.error == "上次运行中断，可重新执行"
    assert path.read_bytes() == b"an unrelated user file"


def test_startup_does_not_claim_other_playlists_recording(service):
    target, candidates = searched(service)
    chosen = next(c for c in candidates if c.verdict == "match")
    old = service.downloader.download(target, service.raw_candidate(chosen.id), service.providers["mock"], "另一份清单")
    target.status, target.file = "downloading", None  # stopped before this attempt published anything
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    recovered = service.track(target.id)
    assert recovered.status == "matched" and recovered.file is None
    assert Path(old.path).is_file()


def test_startup_recovers_published_replacement_instead_of_old_file(service, tmp_path):
    target, candidates = searched(service)
    old_path = tmp_path / "old.mp3"
    old_path.write_bytes(b"old recording")
    target.status = "done"
    target.file = DownloadedFile(path=str(old_path), size=old_path.stat().st_size, format="mp3", bitrate=128,
                                 duration=180, sha256="old", candidate_id=target.chosen_candidate_id, chosen_by=target.chosen_by)
    service.save_track(target)
    replacement = next(c for c in candidates if c.flags == ["live"])
    target = service.choose(target.id, replacement.id)
    published = service.downloader.download(target, service.raw_candidate(replacement.id), service.providers["mock"], "测试",
        on_publish=lambda path, sha: service.db.prepare_publication(target.id, replacement.id, str(path.resolve()), sha))
    target.status = "downloading"
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    recovered = service.track(target.id)
    assert recovered.status == "done" and recovered.file.path == published.path
    assert recovered.chosen_candidate_id == replacement.id and recovered.chosen_by == "user"
    assert old_path.read_bytes() == b"old recording"


def test_startup_rejects_published_file_changed_after_prepare(service):
    target, candidates = searched(service)
    chosen = next(c for c in candidates if c.verdict == "match")
    published = service.downloader.download(target, service.raw_candidate(chosen.id), service.providers["mock"], "测试",
        on_publish=lambda path, sha: service.db.prepare_publication(target.id, chosen.id, str(path.resolve()), sha))
    Path(published.path).write_bytes(b"file was replaced")
    target.status, target.file = "downloading", None
    service.db.save("tracks", target.model_dump(exclude={"file_missing"}))
    service._recover()
    assert service.track(target.id).status == "matched" and service.track(target.id).file is None
