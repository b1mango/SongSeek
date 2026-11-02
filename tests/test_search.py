import threading
import time

import pytest

from songseek.errors import SongSeekError
from songseek.models import ParsedRow, ProviderInfo, RawCandidate
from songseek.providers.mock import MockProvider
from songseek.query import parse_query


@pytest.mark.parametrize("query, title, artist", [
    ("Immortals Fall Out Boy", "Immortals", "Fall Out Boy"),
    ("晴天 周杰伦", "晴天", "周杰伦"),
    ("漂洋过海来看你 李宗盛", "漂洋过海来看你", "李宗盛"),
    ("Dusk Till Dawn ZAYN", "Dusk Till Dawn", "ZAYN"),
    ("七里香", "七里香", ""),
])
def test_single_search_infers_from_results(service, monkeypatch, query, title, artist):
    calls = []
    def search(text, limit):
        calls.append(text)
        return [RawCandidate(provider="mock", source_id="a", title=title, artist=artist or "周杰伦", duration=180)]
    monkeypatch.setattr(service.providers["mock"], "search", search)
    result = service.search_one(query, 180)
    assert calls == [query]
    assert (result["track"].title, result["track"].artist) == (title, artist)
    assert result["candidates"][0].verdict == ("match" if artist else "review")


def test_query_explicit_and_unconfirmed():
    assert parse_query("Immortals - Fall Out Boy") == ("Immortals", "Fall Out Boy")
    assert parse_query("晴天 / 周杰伦") == ("晴天", "周杰伦")
    assert parse_query("Immortals Fall Out Boy") == ("Immortals Fall Out Boy", "")
    assert parse_query("七里香") == ("七里香", "")
    wrong = RawCandidate(provider="mock", source_id="a", title="Immortal", artist="Boy")
    assert parse_query("Immortals Fall Out Boy", [wrong]) == ("Immortals Fall Out Boy", "")


def test_query_infers_artist_alias():
    raw = RawCandidate(provider="mock", source_id="a", title="光年之外", artist="G.E.M.邓紫棋")
    assert parse_query("光年之外 邓紫棋", [raw]) == ("光年之外", "邓紫棋")


def test_no_duration_consensus_auto_chooses_original_over_covers(service, monkeypatch):
    other = MockProvider(service.config)
    service.providers["other"] = other
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="128k", order=1))
    originals = [RawCandidate(provider=p, source_id="original", title="光年之外", artist="G.E.M.邓紫棋", duration=d, bitrate=128)
                 for p, d in [("mock", 234), ("other", 235)]]
    covers = [RawCandidate(provider=p, source_id=artist, title="光年之外", artist=artist, duration=234, bitrate=320)
              for p in ("mock", "other") for artist in ("徐俊雅", "冯提莫", "陶心瑶")]
    for provider_id, provider in service.providers.items():
        monkeypatch.setattr(provider, "search", lambda *a, p=provider_id: [r for r in covers + originals if r.provider == p])
    result = service.search_one("光年之外 邓紫棋")
    target = result["track"]
    assert target.duration is None and target.status == "matched" and target.chosen_by == "auto"
    assert service.candidate(target.chosen_candidate_id).source_id == "original"
    assert [c.verdict for c in result["candidates"]] == ["match", "match"] + ["review"] * 6
    assert result["candidates"][0].checks[2].label == "多个音源时长一致 3:54"


def test_single_search_prefers_provider_order(service, monkeypatch):
    other = MockProvider(service.config)
    service.providers["other"] = other
    service.config.providers.append(ProviderInfo(id="other", label="其他", max_quality="128k", order=1))
    monkeypatch.setattr(service.providers["mock"], "search", lambda *a: [RawCandidate(
        provider="mock", source_id="a", title="Dusk Till Dawn", artist="ZAYN")])
    monkeypatch.setattr(other, "search", lambda *a: [RawCandidate(
        provider="other", source_id="b", title="Dusk Till", artist="Dawn ZAYN")])
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
