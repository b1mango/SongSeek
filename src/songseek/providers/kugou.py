import hashlib
import html

from ..errors import SongSeekError
from ..models import AudioSource, RawCandidate
from .base import BaseProvider, audio_url


def parse_search(data: dict) -> list[RawCandidate]:
    if data.get("status") != 1:
        raise SongSeekError("酷狗搜索失败", "provider_error")
    return [RawCandidate(provider="kugou", source_id=song["FileHash"], title=html.unescape(song["SongName"]),
                         artist=html.unescape(song["SingerName"]), album=song.get("AlbumName", ""),
                         duration=song.get("Duration") or None, bitrate=128,
                         web_url=f"https://www.kugou.com/song/#hash={song['FileHash']}", extra={"album_id": song.get("AlbumID", "")})
            for song in data.get("data", {}).get("lists", [])]


def parse_audio(data: dict) -> AudioSource:
    urls = data.get("url")
    if not urls:
        raise SongSeekError("酷狗未返回匿名音频地址", "provider_error")
    return AudioSource(audio_url(urls[0]), {"Referer": "https://www.kugou.com/"}, "mp3", 128)


class KugouProvider(BaseProvider):
    id = "kugou"
    referer = "https://www.kugou.com/"

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        return parse_search(self.request("GET", "https://songsearch.kugou.com/song_search_v2", params={"keyword": query, "page": 1, "pagesize": limit}))[:limit]

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        ident = candidate.source_id
        return parse_audio(self.request("GET", "https://trackercdn.kugou.com/i/v2/", params={
            "key": hashlib.md5((ident + "kgcloudv2").encode()).hexdigest(), "hash": ident,
            "appid": 1005, "pid": 2, "cmd": 25, "behavior": "play", "album_id": candidate.extra.get("album_id", "")}))
