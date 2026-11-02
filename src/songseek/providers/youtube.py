import json
import os
import subprocess
import time

from ..errors import SongSeekError, public_error
from ..models import AudioSource, Health, RawCandidate, now
from .base import BaseProvider, audio_url
from .bilibili import video_title


def parse_search(data: dict) -> list[RawCandidate]:
    results = []
    for entry in data.get("entries", []):
        if not entry or not entry.get("id"):
            continue
        title, artist = video_title(entry.get("track") or entry["title"], entry.get("artist") or "")
        results.append(RawCandidate(provider="youtube", source_id=entry["id"], title=title, artist=artist,
                                   album=entry.get("album") or "", duration=entry.get("duration"), format="m4a",
                                   popularity=min(1, (entry.get("view_count") or 0) / 10000000),
                                   web_url="https://www.youtube.com/watch?v=" + entry["id"], extra={"version_evidence": entry["title"]}))
    return results


def parse_audio(data: dict) -> AudioSource:
    if data.get("url") and data.get("acodec") != "none":
        chosen = data
    else:
        audios = [f for f in data.get("formats", []) if f.get("acodec", "none") != "none" and f.get("vcodec") == "none" and f.get("url")]
        if not audios:
            raise SongSeekError("YouTube 未返回可用音轨", "provider_error")
        chosen = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    ext = chosen.get("ext", "m4a")
    if ext == "webm":
        ext = "opus" if chosen.get("acodec") == "opus" else "webm"
    return AudioSource(audio_url(chosen["url"]), chosen.get("http_headers", data.get("http_headers", {})), ext,
                       round(chosen.get("abr") or chosen.get("tbr") or 0) or None)


class YouTubeProvider(BaseProvider):
    id = "youtube"

    def command(self, arguments: list[str], timeout: int = 11) -> dict:
        command = ["yt-dlp", "--ignore-config", "--no-cache-dir", "--no-update", "--no-warnings", "--socket-timeout", "7", "--retries", "0", "--proxy", self.config.settings.proxy]
        try:
            with self.semaphore:
                result = subprocess.run(command + arguments, capture_output=True, text=True, timeout=timeout,
                                        env={k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")})
        except FileNotFoundError:
            raise SongSeekError("未安装系统 yt-dlp", "missing_tool") from None
        except subprocess.TimeoutExpired:
            raise SongSeekError("YouTube 请求超时，请检查代理", "provider_timeout") from None
        if result.returncode:
            raise SongSeekError("YouTube 无法访问或该视频不可用，请检查代理", "provider_error")
        return json.loads(result.stdout)

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        return parse_search(self.command(["--flat-playlist", "--skip-download", "-J", f"ytsearch{limit}:{query}"]))[:limit]

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        return parse_audio(self.command(["--no-playlist", "--skip-download", "-f", "bestaudio[ext=m4a]/bestaudio", "-J", candidate.web_url], timeout=25))

    def health(self) -> Health:
        started = time.monotonic()
        try:
            self.command(["--flat-playlist", "--skip-download", "-J", "ytsearch1:晴天 周杰伦"], timeout=8)
            return Health(ok=True, latency_ms=round((time.monotonic() - started) * 1000), message="搜索正常；取流以下载结果为准", checked_at=now())
        except Exception as error:
            return Health(ok=False, latency_ms=round((time.monotonic() - started) * 1000), message=public_error(error), checked_at=now())
