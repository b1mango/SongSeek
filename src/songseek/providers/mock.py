import hashlib
import subprocess
import threading

from ..errors import SongSeekError
from ..models import AudioSource, Health, RawCandidate, now
from ..query import parse_query
from .base import BaseProvider


def make_candidates(query: str) -> list[RawCandidate]:
    title, artist = parse_query(query)
    # 演示数据没有外部曲库，仅生成固定格式的候选。
    if not artist:
        title, _, artist = query.rpartition(" ") if " " in query else (query, "", "演示歌手")
    if "不存在" in title:
        return []
    prefix = hashlib.sha256(query.encode()).hexdigest()[:16]
    variants = [("正常", title, artist, 180.0, 192), ("时长", title, artist, 186.0, 128),
                ("歌手", title, "另一位歌手", 180.0, 128), ("现场", title + " (Live)", artist, 180.0, 128),
                ("伴奏", title + " (伴奏)", artist, 180.0, 128)]
    return [RawCandidate(provider="mock", source_id=prefix + "-" + kind, title=name, artist=singer,
                         album="离线演示", duration=duration, bitrate=bitrate, popularity=.9,
                         extra={"fail": "失败" in title}) for kind, name, singer, duration, bitrate in variants]


class MockProvider(BaseProvider):
    id = "mock"

    def __init__(self, config):
        super().__init__(config)
        self._file_lock = threading.Lock()

    def search(self, query: str, limit: int = 15) -> list[RawCandidate]:
        return make_candidates(query)[:limit]

    def resolve(self, candidate: RawCandidate) -> AudioSource:
        if candidate.extra["fail"]:
            raise SongSeekError("演示音源取流失败", "provider_error")
        directory = self.config.home / "mock"
        directory.mkdir(exist_ok=True)
        path = directory / f"{candidate.source_id}.mp3"
        with self._file_lock, self.semaphore:
            if not path.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                                "-t", str(candidate.duration), "-c:a", "libmp3lame", "-b:a", f"{candidate.bitrate}k", "-y", str(path)], check=True, capture_output=True, timeout=60)
        return AudioSource(url=path.as_uri(), ext="mp3", bitrate=candidate.bitrate, local_path=str(path))

    def health(self) -> Health:
        return Health(ok=True, latency_ms=0, message="离线演示可用", checked_at=now())
