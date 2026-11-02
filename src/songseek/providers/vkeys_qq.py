import re
from urllib.parse import urlsplit

import httpx

from ..errors import SongSeekError
from ..models import AudioSource, Lyrics, RawCandidate
from .base import BaseProvider, audio_url


def parse_song(song: dict) -> RawCandidate:
    # QQ 的 name 会省略 Live 等后缀，必须优先完整 title。
    return RawCandidate(provider="vkeys_qq", source_id=song["songMID"],
                        title=song["title"], artist=song["singer"], album=song.get("album", ""),
                        duration=song.get("interval") or None, bitrate=320,
                        web_url=f"https://y.qq.com/n/ryqq/songDetail/{song['songMID']}")


def parse_search(data: dict) -> list[RawCandidate]:
    if data.get("code") != 0:
        if data.get("code") == 110000:
            raise SongSeekError("QQ 音乐搜索失败（VKeys 110000：上游 HTTP 客户端异常）", "provider_error")
        raise SongSeekError("QQ 音乐搜索失败", "provider_error")
    songs = data.get("data", {}).get("list", [])
    return [parse_song(song) for song in songs]


def parse_audio(data: dict, candidate: RawCandidate) -> AudioSource:
    if data.get("code") != 0 or not data.get("data"):
        raise SongSeekError("VKeys 取流失败", "provider_error")
    song = data["data"]
    quality = re.fullmatch(r"(\d+(?:\.\d+)?)kbps", str(song.get("kbps", "")), re.IGNORECASE)
    if song.get("songMID") != candidate.source_id or not quality or float(quality[1]) <= 0:
        raise SongSeekError("VKeys 返回的歌曲 ID 或音质不符", "provider_error")
    url = audio_url(song.get("url", ""))
    if not urlsplit(url).hostname.endswith(".qqmusic.qq.com"):
        raise SongSeekError("VKeys 未返回 QQ 音频地址", "provider_error")
    return AudioSource(url=url.replace("http://", "https://", 1), headers={"Referer": "https://y.qq.com/"}, ext="mp3", bitrate=round(float(quality[1])))


class VKeysQQProvider(BaseProvider):
    id = "vkeys_qq"
    referer = "https://y.qq.com/"

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        for attempt in range(3):
            try:
                data = self.request("GET", "https://api.vkeys.cn/music/tencent/search/song", params={"keyword": query, "page": 1}, timeout=4)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 429 or attempt == 2:
                    raise
            else:
                limited = data.get("code") == 429 or any(word in str(data.get("message", data.get("msg", ""))) for word in ("频繁", "限流"))
                if not limited and (data.get("code") != 110000 or attempt == 2):
                    return parse_search(data)[:limit]
                if limited and attempt == 2:
                    raise SongSeekError("QQ 音乐搜索限流，稍后重试", "rate_limited")
            self.backoff(.4 * 2 ** attempt)

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        data = self.request("GET", "https://api.vkeys.cn/music/tencent/song/link", params={"mid": candidate.source_id, "quality": 8})
        return parse_audio(data, candidate)

    def lyrics(self, raw: RawCandidate) -> Lyrics | None:
        data = self.request("GET", "https://api.vkeys.cn/v2/music/tencent/lyric", params={"mid": raw.source_id})
        if data.get("code") == 200 and data.get("data", {}).get("lyric"):
            return Lyrics(data["data"]["lyric"], data["data"].get("trans"))
        return None
