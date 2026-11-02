import base64
import hashlib
import html

from ..errors import SongSeekError
from ..models import AudioSource, Lyrics, RawCandidate
from ..normalize import artists_match, title_key, tolerance, version_flags
from .base import BaseProvider, audio_url


def parse_search(data: dict) -> list[RawCandidate]:
    if data.get("status") != 1:
        raise SongSeekError("酷狗搜索失败", "provider_error")
    return [RawCandidate(provider="kugou", source_id=song["FileHash"], title=html.unescape(song["SongName"]),
                         artist=html.unescape(song["SingerName"]), album=song.get("AlbumName", ""),
                         duration=song.get("Duration") or None, bitrate=128,
                         web_url=f"https://www.kugou.com/song/#hash={song['FileHash']}",
                         extra={"album_id": song.get("AlbumID", ""),
                                "cover_url": (song.get("AlbumImage") or song.get("Image") or song.get("imgUrl") or song.get("album_img") or "").replace("{size}", "480")})
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

    def lyrics(self, raw: RawCandidate) -> Lyrics | None:
        return self._lyrics(raw.title, raw.artist, raw.duration, version_flags(raw.title), raw.source_id)

    def lyrics_keyword(self, track, duration: float) -> Lyrics | None:
        return self._lyrics(track.title, track.artist, duration, track.flags, "")

    def _lyrics(self, title, artist, duration, flags, song_hash) -> Lyrics | None:
        data = self.request("GET", "https://lyrics.kugou.com/search", params={
            "ver": 1, "man": "yes", "client": "pc", "hash": song_hash,
            "keyword": f"{artist} - {title}", "duration": round((duration or 0) * 1000)})
        for candidate in data.get("candidates", []):
            # 酷狗随 duration 参数使用毫秒；未指定时长的关键词结果可能使用秒。
            try:
                actual_duration = float(candidate.get("duration", 0)) / (1000 if duration is not None else 1)
            except (TypeError, ValueError):
                continue
            if (title_key(candidate.get("song", "")) != title_key(title)
                    or not artists_match(artist, candidate.get("singer", ""))
                    or set(version_flags(candidate.get("song", ""))) != set(flags)
                    or (duration is not None and not abs(actual_duration - duration) <= max(tolerance(duration), 6))):
                continue
            result = self.request("GET", "https://lyrics.kugou.com/download", params={
                "ver": 1, "client": "pc", "id": candidate["id"], "accesskey": candidate["accesskey"],
                "fmt": "lrc", "charset": "utf8"})
            if result.get("content"):
                return Lyrics(base64.b64decode(result["content"], validate=True).decode("utf-8-sig"))
        return None

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        ident = candidate.source_id
        return parse_audio(self.request("GET", "https://trackercdn.kugou.com/i/v2/", params={
            "key": hashlib.md5((ident + "kgcloudv2").encode()).hexdigest(), "hash": ident,
            "appid": 1005, "pid": 2, "cmd": 25, "behavior": "play", "album_id": candidate.extra.get("album_id", "")}))
