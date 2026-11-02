import json
import re
import random
import threading
import time
from urllib.parse import parse_qs, urlsplit

from ..errors import SongSeekError
from ..models import AudioSource, Lyrics, ParsedRow, ParseResult, RawCandidate
from .base import BaseProvider, audio_url

URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。）)]+", re.I)
SHORT_HOSTS = ("163cn.tv", "163cn.link")
PLAYLIST_ROUTE = re.compile(r"playlist(?:/(\d+)|\?([^#]*))", re.I)

_request_lock = threading.Lock()
_last_request = 0.0


def rate_limited(data: dict) -> bool:
    return data.get("code") == 405 or "操作频繁" in str(data.get("msg", "")) or "操作频繁" in str(data.get("message", ""))


def wait_for_request() -> None:
    global _last_request
    with _request_lock:
        delay = .3 - (time.monotonic() - _last_request)
        if delay > 0:
            time.sleep(delay)
        _last_request = time.monotonic()


def parse_song(song: dict) -> RawCandidate:
    artists = song.get("ar", song.get("artists", []))
    album = song.get("al", song.get("album", {}))
    milliseconds = song.get("dt", song.get("duration"))
    return RawCandidate(provider="netease", source_id=str(song["id"]), title=song["name"],
                        artist=" & ".join(a["name"] for a in artists), album=album.get("name", ""),
                        duration=milliseconds / 1000 if milliseconds else None, bitrate=320,
                        popularity=min(1, max(0, song["pop"] / 100)) if song.get("pop") is not None else None,
                        web_url=f"https://music.163.com/#/song?id={song['id']}",
                        extra={"fee": song.get("fee", 0), "cover_url": album.get("picUrl")})


def parse_search(data: dict) -> list[RawCandidate]:
    if rate_limited(data):
        raise SongSeekError("网易云限流（操作频繁），稍后重试", "rate_limited")
    if data.get("code") != 200:
        raise SongSeekError("网易云搜索失败", "provider_error")
    return [parse_song(song) for song in data.get("result", {}).get("songs", [])]


def parse_audio(data: dict, candidate: RawCandidate) -> AudioSource:
    songs = data.get("data") or []
    if data.get("code") != 200 or not songs:
        raise SongSeekError("网易云未返回音频，需要会员或当前不可播放", "needs_member")
    song = songs[0]
    if str(song.get("id")) != candidate.source_id:
        raise SongSeekError("网易云返回的歌曲 ID 不一致", "provider_error")
    if not song.get("url") or song.get("freeTrialInfo") or song.get("code") != 200:
        raise SongSeekError("需要会员或歌曲当前不可播放，未使用试听片段", "needs_member")
    url = audio_url(song["url"])
    hostname = urlsplit(url).hostname
    if not (hostname.endswith(".music.126.net") or hostname.endswith(".music.163.com")):
        raise SongSeekError("网易云返回了非预期音频地址", "provider_error")
    return AudioSource(url=url.replace("http://", "https://", 1), headers={"Referer": "https://music.163.com/"},
                       ext=song.get("type", "mp3"), bitrate=int(song.get("br", 0)) // 1000 or None)


class NeteaseProvider(BaseProvider):
    id = "netease"
    referer = "https://music.163.com/"

    def call(self, path: str, authenticated: bool = False, **data) -> dict:
        headers = {}
        if authenticated and self.config.settings.netease_cookie_set:
            headers["Cookie"] = self.config.cookie_path.read_text("utf-8").strip()
        for attempt in range(4):
            wait_for_request()
            result = self.request("POST", "https://music.163.com" + path, data=data, headers=headers, follow_redirects=False)
            if not rate_limited(result):
                return result
            if attempt < 3:
                self.backoff(random.uniform(.5, .75) * 2 ** attempt)
        raise SongSeekError("网易云限流（操作频繁），稍后重试", "rate_limited")

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        return parse_search(self.call("/api/cloudsearch/pc", s=query, type=1, limit=limit, offset=0))[:limit]

    def lyrics(self, raw: RawCandidate) -> Lyrics | None:
        data = self.call("/api/song/lyric", id=raw.source_id, lv=1, tv=-1)
        if data.get("code") == 200 and data.get("lrc", {}).get("lyric"):
            return Lyrics(data["lrc"]["lyric"], data.get("tlyric", {}).get("lyric"))
        return None

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        authenticated = self.config.settings.netease_cookie_set
        if not authenticated and candidate.extra.get("fee") in (1, 4):
            raise SongSeekError("需要会员，请在设置中填写有播放权限的网易云 Cookie", "needs_member")
        path = "/api/song/enhance/player/url/v1" if authenticated else "/api/song/enhance/player/url"
        values = {"ids": json.dumps([int(candidate.source_id)])}
        values.update({"level": "exhigh", "encodeType": "mp3"} if authenticated else {"br": 320000})
        return parse_audio(self.call(path, authenticated=authenticated, **values), candidate)

    def expand_short_link(self, url: str) -> str:
        try:
            with self.client() as client:
                response = client.get(url, follow_redirects=False)
        except Exception as error:
            raise SongSeekError("短链接展开失败，请复制完整歌单链接", "invalid_playlist_url") from error
        location = response.headers.get("location", "")
        if not location:
            raise SongSeekError("短链接展开失败，请复制完整歌单链接", "invalid_playlist_url")
        return location

    def playlist_id(self, text: str) -> str:
        found = URL_PATTERN.search(text.strip())
        if not found:
            raise SongSeekError("没有找到链接，请粘贴网易云歌单链接", "invalid_playlist_url")
        url = found.group(0)
        if urlsplit(url).hostname in SHORT_HOSTS:
            url = self.expand_short_link(url)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not (host == "music.163.com" or host.endswith(".music.163.com")):
            raise SongSeekError("只支持网易云公开歌单链接", "invalid_playlist_url")
        for route in (parsed.fragment, parsed.path + "?" + parsed.query):
            match = PLAYLIST_ROUTE.search(route)
            if match:
                identifier = match.group(1) or parse_qs(match.group(2) or "").get("id", [""])[0]
                if identifier.isdigit():
                    return identifier
                raise SongSeekError("歌单链接缺少有效 ID", "invalid_playlist_url")
        raise SongSeekError("这不是歌单链接（可能是单曲或专辑），请打开歌单后复制链接", "invalid_playlist_url")

    def playlist(self, url: str) -> ParseResult:
        identifier = self.playlist_id(url)
        data = self.call("/api/v6/playlist/detail", id=identifier, n=1000, s=0)
        if data.get("code") != 200 or not data.get("playlist"):
            raise SongSeekError("无法读取公开歌单", "provider_error")
        playlist = data["playlist"]
        songs = {str(s["id"]): s for s in playlist.get("tracks", [])}
        ids = [str(item["id"]) for item in playlist.get("trackIds", [])] or list(songs)
        missing = [identifier for identifier in ids if identifier not in songs]
        for offset in range(0, len(missing), 200):
            batch = [int(n) for n in missing[offset:offset + 200]]
            details = self.call("/api/v3/song/detail", c=json.dumps([{"id": n} for n in batch]), ids=json.dumps(batch))
            if details.get("code") != 200:
                raise SongSeekError("歌单曲目补全失败，请重试", "provider_error")
            songs.update({str(s["id"]): s for s in details.get("songs", [])})
        rows, warnings = [], []
        for identifier in ids:
            if identifier not in songs:
                warnings.append(f"歌曲 {identifier} 不再公开，已忽略")
                continue
            raw = parse_song(songs[identifier])
            rows.append(ParsedRow(title=raw.title, artist=raw.artist, album=raw.album, duration=raw.duration))
        return ParseResult(name=playlist["name"], source="netease", rows=rows, warnings=warnings)
