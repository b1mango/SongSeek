import threading
import time

import pytest

from songseek.errors import SongSeekError
from songseek.models import ParsedRow, ProviderInfo, RawCandidate
from songseek.providers.mock import MockProvider
from songseek.query import parse_query


@pytest.fixture
def catalog(service):
    service.providers = {"netease": service.providers["mock"]}
    service.config.providers = [ProviderInfo(id="netease", label="网易云", max_quality="320k", order=0)]
    return service


@pytest.mark.parametrize("query, title, artist", [
    ("Immortals Fall Out Boy", "Immortals", "Fall Out Boy"),
    ("晴天 周杰伦", "晴天", "周杰伦"),
    ("漂洋过海来看你 李宗盛", "漂洋过海来看你", "李宗盛"),
    ("Dusk Till Dawn ZAYN", "Dusk Till Dawn", "ZAYN"),
    ("七里香", "七里香", ""),
])
def test_single_search_infers_from_results(service, catalog, monkeypatch, query, title, artist):
    calls = []
    def search(text, limit):
        calls.append(text)
        return [RawCandidate(provider="netease", source_id="a", title=title, artist=artist or "周杰伦", duration=180)]
    monkeypatch.setattr(service.providers["netease"], "search", search)
    result = service.search_one(query, 180)
    assert calls == [query]
    assert (result["track"].title, result["track"].artist) == (title, artist)
    assert result["candidates"][0].verdict == ("match" if artist else "review")


def test_query_explicit_and_unconfirmed():
    assert parse_query("Immortals - Fall Out Boy") == ("Immortals", "Fall Out Boy")
    assert parse_query("晴天 / 周杰伦") == ("晴天", "周杰伦")
    assert parse_query("Immortals Fall Out Boy") == ("Immortals Fall Out Boy", "")
    assert parse_query("七里香") == ("七里香", "")
    wrong = RawCandidate(provider="netease", source_id="a", title="Immortal", artist="Boy")
    assert parse_query("Immortals Fall Out Boy", [wrong]) == ("Immortals Fall Out Boy", "")


def test_query_infers_artist_alias():
    raw = RawCandidate(provider="netease", source_id="a", title="光年之外", artist="G.E.M.邓紫棋")
    assert parse_query("光年之外 邓紫棋", [raw]) == ("光年之外", "邓紫棋")


def test_no_duration_consensus_auto_chooses_original_over_covers(service, catalog, monkeypatch):
    other = MockProvider(service.config)
    service.providers["kugou"] = other
    service.config.providers.append(ProviderInfo(id="kugou", label="其他", max_quality="128k", order=1))
    originals = [RawCandidate(provider=p, source_id="original", title="光年之外", artist="G.E.M.邓紫棋", duration=d, bitrate=128)
                 for p, d in [("netease", 234), ("kugou", 235)]]
    covers = [RawCandidate(provider=p, source_id=artist, title="光年之外", artist=artist, duration=234, bitrate=320)
              for p in ("netease", "kugou") for artist in ("徐俊雅", "冯提莫", "陶心瑶")]
    for provider_id, provider in service.providers.items():
        monkeypatch.setattr(provider, "search", lambda *a, p=provider_id: [r for r in covers + originals if r.provider == p])
    result = service.search_one("光年之外 邓紫棋")
    target = result["track"]
    assert target.duration is None and target.status == "matched" and target.chosen_by == "auto"
    assert service.candidate(target.chosen_candidate_id).source_id == "original"
    assert [c.verdict for c in result["candidates"]] == ["match", "match"] + ["review"] * 6
    assert result["candidates"][0].checks[2].label == "多个音源时长一致 3:54"


def test_single_search_prefers_provider_order(service, catalog, monkeypatch):
    other = MockProvider(service.config)
    service.providers["kugou"] = other
    service.config.providers.append(ProviderInfo(id="kugou", label="其他", max_quality="128k", order=1))
    monkeypatch.setattr(service.providers["netease"], "search", lambda *a: [RawCandidate(
        provider="netease", source_id="a", title="Dusk Till Dawn", artist="ZAYN")])
    monkeypatch.setattr(other, "search", lambda *a: [RawCandidate(
        provider="kugou", source_id="b", title="Dusk Till", artist="Dawn ZAYN")])
    result = service.search_one("Dusk Till Dawn ZAYN")
    assert (result["track"].title, result["track"].artist) == ("Dusk Till Dawn", "ZAYN")


@pytest.mark.parametrize("first, expected_calls", [("usable", 1), ("empty", 2), ("wrong", 2), ("error", 1)])
def test_title_fallback_only_when_needed(service, monkeypatch, first, expected_calls):
    playlist = service.create_playlist("测试", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    calls = []
    def search(query, limit):
        calls.append(query)
        if len(calls) == 1:
            if first == "error":
                raise SongSeekError("请求超时")
            if first == "empty":
                return []
        return [RawCandidate(provider="mock", source_id=query, title="其他" if first == "wrong" else "晴天", artist="周杰伦", duration=180)]
    monkeypatch.setattr(service.providers["mock"], "search", search)
    service.search_track(service.tracks(playlist.id)[0].id)
    assert len(calls) == expected_calls


def test_deadline_retains_first_results_and_other_providers(service, monkeypatch):
    monkeypatch.setattr("songseek.service.SEARCH_TIMEOUT", .1)
    slow = MockProvider(service.config)
    service.providers["slow"] = slow
    service.config.providers.append(ProviderInfo(id="slow", label="慢音源", max_quality="128k", order=1))
    release = threading.Event()
    def search(query, limit):
        if query == "晴天":
            release.wait(1)
        return [RawCandidate(provider="slow", source_id="a", title="其他", artist="周杰伦", duration=180)]
    monkeypatch.setattr(slow, "search", search)
    playlist = service.create_playlist("测试", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    started = time.monotonic()
    try:
        result = service.search_track(service.tracks(playlist.id)[0].id)
        assert time.monotonic() - started < .5
        assert {c.provider for c in result["candidates"]} == {"mock", "slow"}
        assert result["track"].status == "matched"
        assert result["track"].error == "slow：搜索超时"
    finally:
        release.set()


def test_incremental_title_fallback_keeps_ids_and_lock(service, monkeypatch):
    playlist = service.create_playlist("测试", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track_id = service.tracks(playlist.id)[0].id
    calls, events = [], []

    def search(query, limit):
        calls.append(query)
        return [RawCandidate("mock", "same", "其他" if len(calls) == 1 else "晴天", "周杰伦", duration=180)]

    def emit(event):
        events.append(event)
        if event["type"] == "progress":
            candidate = event["candidates"][0]
            assert service.candidate(candidate["id"]).model_dump() == candidate
            assert candidate["preview_url"] == f"/api/candidates/{candidate['id']}/preview"
            errors = []
            def mutate():
                try:
                    service.update_track(track_id, {"note": "忙碌"})
                except SongSeekError as error:
                    errors.append(error.code)
            thread = threading.Thread(target=mutate)
            thread.start()
            thread.join()
            assert errors == ["track_busy"]

    monkeypatch.setattr(service.providers["mock"], "search", search)
    result = service.search_track(track_id, on_event=emit)
    assert calls == ["晴天 周杰伦", "晴天"]
    assert [e["type"] for e in events] == ["start", "progress", "progress", "done"]
    assert events[1]["candidates"][0]["id"] == events[2]["candidates"][0]["id"]
    assert result["track"].status == "matched"
    assert events[-1] == {"type": "done", "track": result["track"].model_dump(),
                          "candidates": [c.model_dump() for c in result["candidates"]]}


@pytest.mark.parametrize("fee, cookie_set, expected", [(1, False, True), (4, False, True), (0, False, False), (8, False, False), (1, True, False), (4, True, False)])
def test_member_flag_persisted(service, monkeypatch, fee, cookie_set, expected):
    service.config.settings.netease_cookie_set = cookie_set
    service.providers = {"netease": service.providers["mock"]}
    service.config.providers = [ProviderInfo(id="netease", label="网易云", max_quality="320k", order=0)]
    monkeypatch.setattr(service.providers["netease"], "search", lambda *a: [RawCandidate(
        "netease", "member", "晴天", "周杰伦", duration=180, extra={"fee": fee})])
    events = []
    result = service.search_one("晴天 周杰伦", 180, on_event=events.append)
    candidate = result["candidates"][0]
    assert candidate.needs_member is expected
    assert candidate.verdict == "match"
    assert service.candidate(candidate.id).needs_member is expected
    assert events[1]["candidates"][0]["needs_member"] is expected
    old = candidate.model_dump(exclude={"needs_member"})
    service.db.save("candidates", old)
    assert service.candidate(candidate.id).needs_member is False


def test_repeated_single_search_reuses_its_row(service):
    first = service.search_one("晴天 - 周杰伦", 180)["track"]
    again = service.search_one("晴天 - 周杰伦", 180)["track"]
    assert again.id == first.id
    assert len(service.tracks(first.playlist_id)) == 1
    other = service.search_one("晴天 (Live) - 周杰伦", 180)["track"]
    assert other.id != first.id


def test_researching_a_downloaded_track_keeps_it_done(service):
    from songseek.models import DownloadedFile
    track = service.search_one("晴天 - 周杰伦", 180)["track"]
    track.status, track.file = "done", DownloadedFile(path="/tmp/x.mp3", size=1, format="mp3", bitrate=320, duration=180, sha256="x")
    service.save_track(track)
    result = service.search_track(track.id)
    assert result["track"].status == "done"
    assert service.search_one("晴天 - 周杰伦", 180)["track"].id == track.id


@pytest.mark.parametrize("restriction", ["disabled", "scope"])
def test_research_keeps_history_but_only_auto_selects_current_sources(service, monkeypatch, restriction):
    primary = service.providers["mock"]
    other = MockProvider(service.config)
    service.providers["other"] = other
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="128k", order=1))
    def rows(provider, bitrate):
        return [RawCandidate(provider, "song", "晴天", artist="周杰伦", duration=180, bitrate=bitrate)]
    monkeypatch.setattr(primary, "search", lambda *args: rows("mock", 320))
    monkeypatch.setattr(other, "search", lambda *args: rows("other", 128))
    first = service.search_one("晴天 - 周杰伦", 180)["track"]
    assert service.candidate(first.chosen_candidate_id).provider == "mock"
    if restriction == "disabled":
        service.config.providers[0].enabled = False
    result = service.search_track(first.id, providers=["other"] if restriction == "scope" else None)
    assert service.candidate(result["track"].chosen_candidate_id).provider == "other"
    assert {c.provider for c in result["candidates"]} == {"mock", "other"}
    assert service.track(first.id).providers == (["other"] if restriction == "scope" else None)


def test_duration_consensus_excludes_historical_sources_outside_scope(service, monkeypatch):
    service.providers["other"] = MockProvider(service.config)
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="320k", order=1))
    for identifier, provider in service.providers.items():
        monkeypatch.setattr(provider, "search", lambda *args, p=identifier: [RawCandidate(p, "song", "晴天", artist="周杰伦", duration=180)])
    first = service.search_one("晴天 - 周杰伦")["track"]
    assert first.status == "matched"
    result = service.search_track(first.id, providers=["mock"])
    assert result["track"].status == "needs_review" and result["track"].chosen_candidate_id is None
    assert {c.provider for c in result["candidates"]} == {"mock", "other"}


def test_auto_selection_rechecks_source_disabled_during_search(service, monkeypatch):
    def search(*args):
        service.config.providers[0].enabled = False
        return [RawCandidate("mock", "song", "晴天", artist="周杰伦", duration=180)]
    monkeypatch.setattr(service.providers["mock"], "search", search)
    result = service.search_one("晴天 - 周杰伦", 180)
    assert result["track"].chosen_candidate_id is None
    assert len(result["candidates"]) == 1
