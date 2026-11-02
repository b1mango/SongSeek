from urllib.parse import urlsplit, urlunsplit

from ..errors import SongSeekError
from ..models import AudioSource, RawCandidate
from ..normalize import artist_set, key
from .base import BaseProvider, audio_url


def parse_search(data: dict) -> list[RawCandidate]:
    return [RawCandidate(provider="zpod", source_id=str(song["id"]), title=song["title"],
                         artist=song.get("artist", ""), album=song.get("album", ""),
                         duration=song.get("duration") or None, bitrate=128) for song in data["items"]]


def parse_audio(data: dict, candidate: RawCandidate) -> AudioSource:
    if key(data.get("title", "")) != key(candidate.title) or artist_set(data.get("artist", "")) != artist_set(candidate.artist):
        raise SongSeekError("zpod 播放目录与搜索结果不一致", "provider_error")
    parsed = urlsplit(data.get("streamUrl", ""))
    if not parsed.path.startswith("/api/stream/"):
        raise SongSeekError("zpod 未返回有效的流地址", "provider_error")
    url = urlunsplit(("https", "zpod.net", "/proxy/hk" + parsed.path, parsed.query, ""))
    return AudioSource(audio_url(url), {"Referer": "https://zpod.net/", "Range": "bytes=0-"}, "mp3", 128)


class ZpodProvider(BaseProvider):
    id = "zpod"
    referer = "https://zpod.net/"

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        return parse_search(self.request("GET", "https://zpod.net/proxy/hk/api/search", params={"q": query}))[:limit]

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        return parse_audio(self.request("GET", "https://zpod.net/proxy/hk/api/play", params={"id": candidate.source_id}), candidate)
