import json
import time
from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from songseek.models import DownloadedFile, ParsedRow, ProviderInfo, RawCandidate
from songseek.providers.mock import MockProvider
from songseek.server import create_app


@pytest.fixture
def matched(service):
    return service.search_one("晴天 - 周杰伦", 180)["track"]


def with_file(service, track):
    track.status = "done"
    track.file = DownloadedFile(path="/tmp/songseek-existing.mp3", size=1, format="mp3", bitrate=128, duration=180, sha256="old")
    return service.save_track(track)


@pytest.mark.parametrize("values", [{"album": "新专辑"}, {"note": "备注"}, {"lyrics_wanted": False}])
def test_metadata_edit_keeps_matches_and_file(service, matched, values):
    original = with_file(service, matched)
    ids = [c.id for c in service.candidates(original.id)]
    updated = service.update_track(original.id, values)
    assert updated.status == "done" and updated.file == original.file
    assert updated.chosen_candidate_id == original.chosen_candidate_id
    assert updated.candidate_count == original.candidate_count and updated.best_score == original.best_score
    assert [c.id for c in service.candidates(original.id)] == ids


@pytest.mark.parametrize("values", [{"title": "晴天 Live"}, {"artist": "另一位歌手"}, {"duration": None}])
@pytest.mark.parametrize("downloaded", [False, True])
def test_matching_edit_resets_candidates_but_keeps_download(service, matched, values, downloaded):
    original = with_file(service, matched) if downloaded else matched
    updated = service.update_track(original.id, values)
    assert updated.status == ("done" if downloaded else "pending")
    assert updated.file == original.file
    assert updated.chosen_candidate_id is updated.chosen_by is updated.chosen is None
    assert updated.best_score is None and updated.candidate_count == 0
    assert service.candidates(original.id) == []


def test_matching_edit_rolls_back_candidate_delete_with_track_save(service, matched, monkeypatch):
    candidates = service.candidates(matched.id)
    save = service.db.save
    def fail(table, data):
        if table == "tracks":
            raise RuntimeError("database unavailable")
        save(table, data)
    monkeypatch.setattr(service.db, "save", fail)
    with pytest.raises(RuntimeError):
        service.update_track(matched.id, {"artist": "新歌手"})
    assert service.track(matched.id) == matched
    assert service.candidates(matched.id) == candidates


def test_choosing_current_download_is_idempotent(service, matched):
    original = with_file(service, matched)
    listener = service.events.subscribe()
    assert service.choose(original.id, original.chosen_candidate_id) == original
    assert listener.empty()


def test_batch_protects_download_and_reports_only_changes(service, matched):
    original = with_file(service, matched)
    playlist = service.create_playlist("批量", "text", [ParsedRow(title="未知"), ParsedRow(title="另一首")])
    pending, skipped = service.tracks(playlist.id)
    skipped.status, skipped.error = "skipped", "以前的错误"
    service.save_track(skipped)
    result = service.batch([original.id, pending.id, skipped.id], "skip")
    assert [t.id for t in result["updated"]] == [pending.id]
    assert [row["id"] for row in result["skipped"]] == [original.id, skipped.id]
    assert all(row["reason"] for row in result["skipped"])
    assert service.track(original.id) == original
    result = service.batch([skipped.id, pending.id], "unskip")
    assert all(t.status == "pending" and t.error is None for t in result["updated"])
    assert result["skipped"] == []


def test_batch_accept_best_only_reviews_and_skips_unavailable(service, matched):
    review = service.search_one("后来 - 刘若英")["track"]
    empty = service.create_playlist("无候选", "text", [ParsedRow(title="不存在")])
    missing = service.tracks(empty.id)[0]
    missing.status = "needs_review"
    service.save_track(missing)
    result = service.batch([matched.id, review.id, missing.id, 99999], "accept_best")
    assert [t.id for t in result["updated"]] == [review.id]
    assert result["updated"][0].status == "matched"
    assert [row["id"] for row in result["skipped"]] == [matched.id, missing.id, 99999]


def test_batch_api_returns_skipped(service, matched):
    with TestClient(create_app(service=service)) as client:
        result = client.post("/api/tracks/batch", json={"ids": [matched.id], "action": "accept_best"})
        assert result.status_code == 200
        assert result.json()["updated"] == []
        assert result.json()["skipped"][0]["id"] == matched.id


def test_batch_does_not_report_unchanged_candidate_as_updated(service, matched):
    matched.status = "needs_review"
    service.save_track(matched)
    result = service.batch([matched.id], "accept_best")
    assert result["updated"] == [] and result["skipped"][0]["id"] == matched.id


def test_playlist_counts_use_sql_without_decoding_tracks(service, matched, monkeypatch):
    other = service._add_track(matched.playlist_id, ParsedRow(title="另一首"), 2)
    other.status = "skipped"
    service.save_track(other)
    original = service.db.list
    def list_rows(table, *args):
        assert table != "tracks", "counts 不应读取、解码整张曲目表"
        return original(table, *args)
    monkeypatch.setattr(service.db, "list", list_rows)
    counts = service.playlist(matched.playlist_id).counts
    assert counts["total"] == 2 and counts["matched"] == counts["skipped"] == 1
    assert counts["pending"] == 0


def test_playlist_events_only_on_status_or_track_count_changes(service, matched):
    listener = service.events.subscribe()
    service.update_track(matched.id, {"note": "备注", "lyrics_wanted": False, "album": "专辑"})
    assert listener.qsize() == 1
    assert listener.get_nowait().startswith("event: track\n")
    service.batch([matched.id], "skip")
    events = [listener.get_nowait() for _ in range(listener.qsize())]
    playlist_event = next(event for event in events if event.startswith("event: playlist\n"))
    assert json.loads(playlist_event.split("data: ")[1])["counts"]["skipped"] == 1
    service.delete_track(matched.id)
    deleted = listener.get_nowait()
    assert deleted.startswith("event: track_deleted\n")
    assert json.loads(deleted.split("data: ")[1]) == {"id": matched.id, "playlist_id": matched.playlist_id}
    event = listener.get_nowait()
    assert event.startswith("event: playlist\n")
    assert json.loads(event.split("data: ")[1])["counts"]["total"] == 0


def test_adding_single_search_row_updates_playlist_count(service):
    first = service.search_one("晴天 - 周杰伦", 180)["track"]
    listener = service.events.subscribe()
    service.search_one("后来 - 刘若英", 180)
    events = [listener.get_nowait() for _ in range(listener.qsize())]
    counts = [json.loads(event.split("data: ")[1])["counts"] for event in events if event.startswith("event: playlist\n")]
    assert counts[0]["total"] == 2 and counts[0]["pending"] == 1
    assert service.playlist(first.playlist_id).counts["total"] == 2


def test_choosing_failed_current_candidate_makes_it_downloadable_again(service, matched):
    matched.status, matched.error = "failed", "取流失败"
    service.save_track(matched)
    again = service.choose(matched.id, matched.chosen_candidate_id)
    assert again.status == "matched" and again.error is None


def test_file_missing_is_derived_from_disk_and_never_stored(service, matched, tmp_path):
    audio = tmp_path / "晴天.mp3"
    audio.write_bytes(b"x")
    matched.status = "done"
    matched.file = DownloadedFile(path=str(audio), size=1, format="mp3", bitrate=128, duration=180, sha256="x")
    service.save_track(matched)
    assert service.track(matched.id).file_missing is False
    audio.unlink()
    assert service.track(matched.id).file_missing is True
    assert "file_missing" not in service.db.get("tracks", matched.id)
    # an edit round-trips the dumped track (which carries the flag) without tripping extra="forbid"
    assert service.update_track(matched.id, {"note": "搬家了"}).file_missing is True


def test_redownload_requeues_only_missing_files(service, matched, monkeypatch, tmp_path):
    missing = with_file(service, matched)  # /tmp/songseek-existing.mp3 does not exist
    assert missing.file_missing
    present = service.search_one("后来 - 刘若英", 180)["track"]
    audio = tmp_path / "后来.mp3"
    audio.write_bytes(b"x")
    present.status = "done"
    present.file = DownloadedFile(path=str(audio), size=1, format="mp3", bitrate=128, duration=180, sha256="x")
    service.save_track(present)
    pending = service.create_playlist("没下过", "text", [ParsedRow(title="夜曲", artist="周杰伦")])
    started = []
    def start(kind, playlist_id, ids, **kwargs):
        started.append((kind, playlist_id, ids, kwargs))
        track = service.track(ids[0])
        assert track.file_missing
        track.status, track.file = "queued", None
        service.save_track(track)
    monkeypatch.setattr(service.jobs, "start", start)
    result = service.batch([missing.id, present.id, service.tracks(pending.id)[0].id], "redownload")
    assert [t.id for t in result["updated"]] == [missing.id]
    assert [s["reason"] for s in result["skipped"]] == ["文件仍在原位置，无需重新下载", "歌曲还没有下载过，请先下载"]
    track = service.track(missing.id)
    assert track.status == "queued" and track.file is None and track.chosen_candidate_id == matched.chosen_candidate_id
    assert started == [("download", missing.playlist_id, [missing.id], {"redownload": True})]


def test_redownload_keeps_missing_record_when_job_cannot_start(service, matched, monkeypatch):
    from songseek.errors import SongSeekError
    missing = with_file(service, matched)

    def busy(*args, **kwargs):
        raise SongSeekError("曲目已在任务中，请稍后重试", "track_busy", 409)

    monkeypatch.setattr(service.jobs, "start", busy)
    result = service.batch([missing.id], "redownload")
    current = service.track(missing.id)
    assert result["updated"] == [] and result["skipped"][0]["id"] == missing.id
    assert "任务" in result["skipped"][0]["reason"]
    assert current.status == "done" and current.file == missing.file and current.file_missing


def test_redownload_job_reserves_missing_track_before_clearing_record(service, matched):
    missing = with_file(service, matched)
    result = service.batch([missing.id], "redownload")
    assert [track.id for track in result["updated"]] == [missing.id]
    jobs = service.jobs.list()
    assert len(jobs) == 1 and jobs[0].kind == "download"
    service.jobs.wait(jobs[0].id)
    current = service.track(missing.id)
    assert current.status == "done" and not current.file_missing and current.file.path != missing.file.path


def test_failed_swap_on_missing_file_does_not_claim_to_keep_it(service, matched, monkeypatch):
    original = with_file(service, matched)
    other = next(c for c in service.candidates(original.id) if c.id != original.chosen_candidate_id)
    service.choose(original.id, other.id)
    def broken(*args, **kwargs):
        raise ConnectionError("cdn down")
    monkeypatch.setattr(service.downloader, "download", broken)
    result = service.download_track(original.id)
    assert result.status == "failed" and result.file is None
    assert "保留原文件" not in result.error


def test_lyrics_job_ignores_missing_files(service, matched):
    with_file(service, matched)  # /tmp/songseek-existing.mp3 does not exist
    with pytest.raises(Exception) as error:
        service.jobs.start("lyrics", matched.playlist_id, force=True)
    assert getattr(error.value, "code", None) == "no_eligible_tracks"


def test_search_failed_only_when_no_source_answered(service, monkeypatch):
    playlist = service.create_playlist("网络", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track = service.tracks(playlist.id)[0]
    provider = service.providers["mock"]
    original = provider.search
    def down(query, limit):
        raise ConnectionError("offline")
    monkeypatch.setattr(provider, "search", down)
    failed = service.search_track(track.id)["track"]
    assert failed.status == "not_found" and failed.search_failed and failed.error
    monkeypatch.setattr(provider, "search", lambda query, limit: [])
    empty = service.search_track(track.id)["track"]
    assert empty.status == "not_found" and not empty.search_failed
    monkeypatch.setattr(provider, "search", down)
    service.search_track(track.id)
    monkeypatch.setattr(provider, "search", original)
    found = service.search_track(track.id)["track"]
    assert found.status != "not_found" and not found.search_failed


def test_add_tracks_appends_and_skips_duplicates(service):
    playlist = service.create_playlist("追加", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    rows = [{"title": "晴天", "artist": "周杰伦", "duration": 181}, {"title": "红豆", "artist": "王菲"},
            {"title": "红豆 ", "artist": "王菲"}, {"title": "晴天", "artist": "周杰伦", "duration": 240}]
    listener = service.events.subscribe()
    with TestClient(create_app(service=service)) as client:
        body = client.post(f"/api/playlists/{playlist.id}/tracks", json={"rows": rows}).json()
        assert [(t["title"], t["position"], t["status"]) for t in body["added"]] == [("红豆", 2, "pending"), ("晴天", 3, "pending")]
        assert body["duplicates"] == 2
        kinds = []
        while not listener.empty():
            kinds.append(listener.get_nowait().split("\n")[0])
        assert kinds.count("event: track") == 2 and kinds.count("event: playlist") == 1
        assert client.post(f"/api/playlists/{playlist.id}/tracks", json={"rows": rows[:1]}).json() == {"added": [], "duplicates": 1}
        assert service.playlist(playlist.id).counts["total"] == 3
        assert client.post("/api/playlists/999/tracks", json={"rows": rows[:1]}).status_code == 404
        assert client.post(f"/api/playlists/{playlist.id}/tracks", json={"rows": []}).status_code == 422


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_search_failure_ignores_historical_rejected_candidates(service, monkeypatch, failure):
    playlist = service.create_playlist("历史候选", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track_id = service.tracks(playlist.id)[0].id
    provider = service.providers["mock"]
    monkeypatch.setattr(provider, "search", lambda *args: [RawCandidate("mock", "old", "其他歌曲", "周杰伦", duration=180)])
    first = service.search_track(track_id)
    assert first["track"].status == "not_found"
    assert [c.verdict for c in first["candidates"]] == ["reject"]
    old_id = first["candidates"][0].id
    other = MockProvider(service.config)
    service.providers["other"] = other
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="128k", order=1))

    kwargs = {}
    if failure == "error":
        def down(*args):
            raise ConnectionError("offline")
        monkeypatch.setattr(provider, "search", down)
        monkeypatch.setattr(other, "search", down)
    else:
        pending = {Future(): identifier for identifier in ("mock", "other")}
        kwargs["prepared"] = (pending.copy(), time.monotonic() - 1)
    events = []
    result = service.search_track(track_id, on_event=events.append, **kwargs)
    track = result["track"]
    assert track.status == "not_found" and track.search_failed and track.error
    assert all(f"{identifier}：" in track.error for identifier in ("mock", "other"))
    assert [c.id for c in result["candidates"]] == [old_id]
    assert service.track(track_id).search_failed
    assert events[-1]["type"] == "done" and events[-1]["track"]["search_failed"]
    if failure == "timeout":
        assert all(future.cancelled() for future in pending) and "搜索超时" in track.error


@pytest.mark.parametrize("response", ["empty", "reject"])
def test_search_failure_requires_all_sources_to_fail(service, monkeypatch, response):
    playlist = service.create_playlist("部分音源可用", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track_id = service.tracks(playlist.id)[0].id
    other = MockProvider(service.config)
    service.providers["other"] = other
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="128k", order=1))

    def down(*args):
        raise ConnectionError("offline")

    monkeypatch.setattr(service.providers["mock"], "search", down)
    candidates = [] if response == "empty" else [RawCandidate("other", "wrong", "其他歌曲", "周杰伦", duration=180)]
    monkeypatch.setattr(other, "search", lambda *args: candidates)
    result = service.search_track(track_id)
    assert result["track"].status == "not_found"
    assert result["track"].error and not result["track"].search_failed
    assert all(c.verdict == "reject" for c in result["candidates"])


def test_search_failure_does_not_override_successful_fallback_response(service, monkeypatch):
    playlist = service.create_playlist("备用搜索", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track_id = service.tracks(playlist.id)[0].id
    calls = []

    def search(query, limit):
        calls.append(query)
        if query == "晴天":
            raise ConnectionError("offline")
        return []

    monkeypatch.setattr(service.providers["mock"], "search", search)
    result = service.search_track(track_id)
    assert calls == ["晴天 周杰伦", "晴天"]
    assert result["track"].status == "not_found" and result["track"].error
    assert not result["track"].search_failed


@pytest.mark.parametrize("history, expected_status", [
    ("match", "matched"), ("review", "needs_review"), ("chosen_reject", "matched"), ("file", "done"),
])
def test_search_failure_flag_requires_final_not_found(service, monkeypatch, history, expected_status):
    playlist = service.create_playlist("已有结果", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track = service.tracks(playlist.id)[0]
    provider = service.providers["mock"]
    if history == "file":
        with_file(service, track)
    else:
        title = "其他歌曲" if history == "chosen_reject" else "晴天"
        artist = "另一位歌手" if history == "review" else "周杰伦"
        monkeypatch.setattr(provider, "search", lambda *args: [RawCandidate("mock", "old", title, artist, duration=180)])
        first = service.search_track(track.id)
        if history == "chosen_reject":
            assert first["candidates"][0].verdict == "reject"
            service.choose(track.id, first["candidates"][0].id)

    def down(*args):
        raise ConnectionError("offline")

    monkeypatch.setattr(provider, "search", down)
    result = service.search_track(track.id)
    assert result["track"].status == expected_status and result["track"].error
    assert not result["track"].search_failed
    assert not service.track(track.id).search_failed


def test_search_with_no_sources_is_not_reported_as_a_source_failure(service):
    playlist = service.create_playlist("无音源", "text", [ParsedRow(title="晴天")])
    track_id = service.tracks(playlist.id)[0].id
    result = service.search_track(track_id, providers=[])
    assert result["track"].status == "not_found"
    assert not result["track"].search_failed and result["track"].error is None


@pytest.mark.parametrize("existing_duration, new_duration, duplicate", [
    (None, None, True),
    (None, 180, False),
    (180, None, False),
    (180, 180, True),
    (180, 178, True),
    (180, 182, True),
    (180, 177.9, False),
    (180, 182.1, False),
])
@pytest.mark.parametrize("compare_with", ["playlist", "same_batch"])
def test_add_tracks_duration_deduplication(service, existing_duration, new_duration, duplicate, compare_with):
    original = ParsedRow(title="紅豆", artist="ＦＡＹＥ", duration=existing_duration)
    new = ParsedRow(title="红豆", artist="faye", duration=new_duration)
    initial = [original] if compare_with == "playlist" else []
    rows = [new] if compare_with == "playlist" else [original, new]
    playlist = service.create_playlist("时长判重", "text", initial)
    result = service.add_tracks(playlist.id, rows)
    tracks = service.tracks(playlist.id)
    assert result["duplicates"] == int(duplicate)
    assert [(t.title, t.artist, t.duration, t.position) for t in tracks] == [
        (row.title, row.artist, row.duration, index)
        for index, row in enumerate([original] + ([] if duplicate else [new]), 1)
    ]
    assert len(result["added"]) == len(rows) - int(duplicate)


@pytest.mark.parametrize("new", [
    {"title": "另一首歌", "artist": "faye"},
    {"title": "红豆", "artist": "另一位歌手"},
])
def test_add_tracks_keeps_different_normalized_title_or_artist(service, new):
    original = ParsedRow(title="紅豆", artist="ＦＡＹＥ")
    playlist = service.create_playlist("歌名歌手判重", "text", [original])
    result = service.add_tracks(playlist.id, [ParsedRow(**new)])
    assert len(result["added"]) == 1 and result["duplicates"] == 0
