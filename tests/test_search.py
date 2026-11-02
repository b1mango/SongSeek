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
