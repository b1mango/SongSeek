import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from songseek.errors import SongSeekError
from songseek.models import RawCandidate
from songseek.providers import bilibili, kugou, mock, netease, vkeys_qq, youtube, zpod


def test_netease_parsers():
    result = netease.parse_search({"code": 200, "result": {"songs": [{"id": 42, "name": "晴天", "artists": [{"name": "周杰伦"}], "album": {"name": "叶惠美"}, "duration": 269300, "pop": 95, "fee": 1}]}})[0]
    assert result.duration == 269.3 and result.popularity == .95
    assert result.extra["fee"] == 1
    data = {"code": 200, "data": [{"id": 42, "code": 200, "url": "http://m7.music.126.net/a.mp3", "type": "mp3", "br": 320000}]}
    assert netease.parse_audio(data, result).bitrate == 320
    data["data"][0]["freeTrialInfo"] = {"start": 0}
    with pytest.raises(SongSeekError, match="会员"):
        netease.parse_audio(data, result)


def test_netease_anonymous_never_reads_cookie(config, monkeypatch):
    provider = netease.NeteaseProvider(config)
    monkeypatch.setattr(provider, "call", lambda *a, **kw: pytest.fail("会员曲目不应请求匿名取流"))
    with pytest.raises(SongSeekError, match="需要会员"):
        provider.resolve(RawCandidate("netease", "42", "晴天", extra={"fee": 1}))


def test_qq_complete_title_and_link_validation():
    data = {"code": 0, "req_1": {"code": 0, "data": {"body": {"song": {"list": [{"mid": "mid", "name": "晴天", "title": "晴天 (Live)", "singer": [{"name": "周杰伦"}], "album": {"title": "演唱会"}, "interval": 269}]}}}}}
    raw = vkeys_qq.parse_search(data)[0]
    assert raw.title == "晴天 (Live)"
    link = {"code": 0, "data": {"songMID": "mid", "kbps": "320kbps", "url": "http://ws.stream.qqmusic.qq.com/a.mp3"}}
    assert vkeys_qq.parse_audio(link, raw).url.startswith("https://")
    for change in [{"songMID": "other"}, {"kbps": "128kbps"}, {"url": "https://qqmusic.qq.com.example.org/a"}, {"url": "file:///tmp/a"}]:
        with pytest.raises(SongSeekError):
            vkeys_qq.parse_audio({"code": 0, "data": link["data"] | change}, raw)


def test_kugou_parsers():
    raw = kugou.parse_search({"status": 1, "data": {"lists": [{"FileHash": "hash", "SongName": "A &amp; B", "SingerName": "甲", "Duration": 200, "AlbumID": "42"}]}})[0]
    assert raw.title == "A & B" and raw.extra["album_id"] == "42"
    assert kugou.parse_audio({"url": ["https://fs.kugou.com/a"]}).bitrate == 128
    with pytest.raises(SongSeekError):
        kugou.parse_audio({"error": "not found"})


def test_zpod_parsers():
    raw = zpod.parse_search({"items": [{"id": "42", "title": "晴天", "artist": "周杰伦"}]})[0]
    assert raw.duration is None
    source = zpod.parse_audio({"title": "晴天", "artist": "周杰倫", "streamUrl": "https://hk.zpod.net/api/stream/abc"}, raw)
    assert source.url == "https://zpod.net/proxy/hk/api/stream/abc"
    assert source.headers["Range"] == "bytes=0-"
    with pytest.raises(SongSeekError):
        zpod.parse_audio({"title": "其他", "artist": "周杰伦"}, raw)


def test_bilibili_parsers():
    data = {"code": 0, "data": {"result": [{"bvid": "BV123", "title": '<em class="keyword">周杰伦</em> - 晴天', "duration": "4:29", "play": 500000}, {"title": "推广"}]}}
    raw = bilibili.parse_search(data)[0]
    assert (raw.title, raw.artist, raw.duration) == ("晴天", "周杰伦", 269)
    source = bilibili.parse_audio({"code": 0, "data": {"dash": {"audio": [{"baseUrl": "https://cdn.example/a", "bandwidth": 64000}, {"base_url": "https://cdn.example/b", "bandwidth": 160000}]}}}, raw.web_url)
    assert source.bitrate == 160
    assert source.headers["Referer"] == raw.web_url


def test_youtube_parsers_and_system_command(config, monkeypatch):
    data = {"entries": [{"id": "abcdefghijk", "title": "周杰伦 - 晴天", "duration": 269, "view_count": 1000000}]}
    raw = youtube.parse_search(data)[0]
    assert raw.title == "晴天" and raw.artist == "周杰伦"
    source = youtube.parse_audio({"formats": [{"acodec": "opus", "vcodec": "none", "url": "https://cdn.example/a", "ext": "webm", "abr": 160}]})
    assert source.ext == "opus" and source.bitrate == 160
    from subprocess import CompletedProcess
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess(command, 0, json.dumps(data), "")
    monkeypatch.setattr(youtube.subprocess, "run", run)
    monkeypatch.setenv("HTTPS_PROXY", "http://do-not-use.invalid")
    youtube.YouTubeProvider(config).search("晴天 周杰伦", 2)
    command, kwargs = calls[0]
    assert command[command.index("--proxy") + 1] == ""
    assert command[-1] == "ytsearch2:晴天 周杰伦"
    assert "HTTPS_PROXY" not in kwargs["env"]


def test_mock_is_deterministic():
    first = mock.make_candidates("晴天 周杰伦")
    assert first == mock.make_candidates("晴天 周杰伦")
    assert len(first) == 5
    assert first[1].duration - first[0].duration == 6
    assert mock.make_candidates("不存在 周杰伦") == []


def test_http_proxy_and_concurrency(config, monkeypatch):
    provider = kugou.KugouProvider(config)
    lock = threading.Lock()
    active, maximum = 0, 0
    def response(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(.02)
        with lock:
            active -= 1
        return httpx.Response(200, json={"status": 1, "data": {"lists": []}})
    arguments = []
    real = httpx.Client
    def client(**kwargs):
        arguments.append(kwargs.copy())
        kwargs.pop("proxy")
        return real(**kwargs, transport=httpx.MockTransport(response))
    monkeypatch.setattr("songseek.providers.base.httpx.Client", client)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(provider.search, ["a"] * 6))
    assert maximum == 2
    assert all(item["trust_env"] is False and item["proxy"] is None for item in arguments)
