from urllib.parse import urlsplit

from ..errors import SongSeekError
from ..models import AudioSource, RawCandidate
from .base import BaseProvider, audio_url


def parse_song(song: dict) -> RawCandidate:
    # QQ 的 name 会省略 Live 等后缀，必须优先完整 title。
    return RawCandidate(provider="vkeys_qq", source_id=song["songMID"],
                        title=song["title"], artist=song["singer"], album=song.get("album", ""),
                        duration=song.get("interval") or None, bitrate=320,
                        web_url=f"https://y.qq.com/n/ryqq/songDetail/{song['songMID']}")


def parse_search(data: dict) -> list[RawCandidate]:
    if data.get("code") != 0:
        raise SongSeekError("QQ 音乐搜索失败", "provider_error")
    songs = data.get("data", {}).get("list", [])
    return [parse_song(song) for song in songs]


def parse_audio(data: dict, candidate: RawCandidate) -> AudioSource:
    if data.get("code") != 0 or not data.get("data"):
        raise SongSeekError("VKeys 取流失败", "provider_error")
    song = data["data"]
    if song.get("songMID") != candidate.source_id or song.get("kbps") != "320kbps":
        raise SongSeekError("VKeys 返回的歌曲 ID 或音质不符", "provider_error")
    url = audio_url(song.get("url", ""))
    if not urlsplit(url).hostname.endswith(".qqmusic.qq.com"):
        raise SongSeekError("VKeys 未返回 QQ 音频地址", "provider_error")
    return AudioSource(url=url.replace("http://", "https://", 1), headers={"Referer": "https://y.qq.com/"}, ext="mp3", bitrate=320)


class VKeysQQProvider(BaseProvider):
    id = "vkeys_qq"
    referer = "https://y.qq.com/"

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        data = self.request("GET", "https://api.vkeys.cn/music/tencent/search/song", params={"keyword": query, "page": 1})
        return parse_search(data)[:limit]

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        data = self.request("GET", "https://api.vkeys.cn/music/tencent/song/link", params={"mid": candidate.source_id, "quality": 8})
        return parse_audio(data, candidate)
