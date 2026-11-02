import html
import re
import threading
import time

import httpx

from ..errors import SongSeekError
from ..models import AudioSource, RawCandidate
from .base import BaseProvider, audio_url

_buvid3 = ""
_buvid_lock = threading.Lock()


def video_title(title: str, artist: str = "") -> tuple[str, str]:
    title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
    title = re.sub(r"\s*[\[(【](?:official\s*(?:music\s*)?(?:video|audio|mv)|歌词版?|高音质|高清音质)[\])】]", "", title, flags=re.I)
    quoted = re.search(r"[《「]([^》」]+)[》」]", title)
    parts = re.split(r"\s+[-–—|]\s+", title, maxsplit=1)
    if parts and len(parts) == 2:
        artist = artist or parts[0].strip()
        title = parts[1].strip()
    elif quoted and not re.search(r"bass\s+tab|贝斯谱", quoted[1], re.I):
        prefix = title[:quoted.start()].strip(" ·-—:：")
        artist = artist or prefix
        title = quoted[1] + " " + title[quoted.end():]
    return title.strip(), artist


def parse_search(data: dict) -> list[RawCandidate]:
    if data.get("code") != 0:
        raise SongSeekError("哔哩哔哩搜索被限制", "provider_error")
    results = []
    for video in data.get("data", {}).get("result", []):
        if not video.get("bvid") or not video.get("duration"):
            continue
        title, artist = video_title(video["title"])
        cover = video.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        duration = sum(float(n) * 60 ** i for i, n in enumerate(reversed(str(video["duration"]).split(":"))))
        results.append(RawCandidate(provider="bilibili", source_id=video["bvid"], title=title, artist=artist,
                       duration=duration, format="m4a", bitrate=160,
                       popularity=min(1, video.get("play", 0) / 1000000),
                       web_url=f"https://www.bilibili.com/video/{video['bvid']}",
                       extra={"version_evidence": html.unescape(video["title"]), "cover_url": cover}))
    return results


def parse_audio(data: dict, referer: str) -> AudioSource:
    if data.get("code") != 0:
        raise SongSeekError("哔哩哔哩匿名取流失败", "provider_error")
    audios = data.get("data", {}).get("dash", {}).get("audio", [])
    if not audios:
        raise SongSeekError("哔哩哔哩未返回独立音轨", "provider_error")
    source = max(audios, key=lambda a: a["bandwidth"])
    return AudioSource(audio_url(source.get("baseUrl", source.get("base_url", ""))), {"Referer": referer}, "m4a", round(source["bandwidth"] / 1000))


class BilibiliProvider(BaseProvider):
    id = "bilibili"
    referer = "https://www.bilibili.com/"

    def headers(self) -> dict[str, str]:
        global _buvid3
        with _buvid_lock:
            if not _buvid3:
                data = super().request("GET", "https://api.bilibili.com/x/frontend/finger/spi")
                _buvid3 = data.get("data", {}).get("b_3", "")
                if data.get("code") != 0 or not _buvid3:
                    raise SongSeekError("哔哩哔哩匿名设备初始化失败", "provider_error")
        return super().headers() | {"Cookie": f"buvid3={_buvid3}"}

    def request(self, method: str, url: str, **kwargs) -> dict:
        headers = self.headers() | kwargs.pop("headers", {})
        for attempt in range(2):
            try:
                data = super().request(method, url, headers=headers, **kwargs)
                if data.get("code") not in (-412, 412):
                    return data
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 412:
                    raise
            if attempt == 0:
                self.backoff(.3)
        raise SongSeekError("哔哩哔哩请求被限制（412），稍后重试", "rate_limited")

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        return parse_search(self.request("GET", "https://api.bilibili.com/x/web-interface/search/type", params={"search_type": "video", "keyword": query, "page": 1, "page_size": limit}))[:limit]

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        view = self.request("GET", "https://api.bilibili.com/x/web-interface/view", params={"bvid": candidate.source_id})
        if view.get("code") != 0:
            raise SongSeekError("哔哩哔哩视频详情不可用", "provider_error")
        if len(view["data"].get("pages", [])) > 1:
            raise SongSeekError("该视频含多分集，请选择可确认的单曲视频", "provider_error")
        cid = view["data"]["cid"]
        data = self.request("GET", "https://api.bilibili.com/x/player/playurl", params={"bvid": candidate.source_id, "cid": cid, "fnval": 16, "qn": 80})
        return parse_audio(data, candidate.web_url)
