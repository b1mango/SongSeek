import hashlib
import math
import struct
import subprocess
import threading

from ..tools import require_tool
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
                partial = path.with_suffix(".part.mp3")
                try:
                    # PCM 输入让桌面版不必携带 lavfi/avdevice。
                    second = b"".join(struct.pack("<h", round(4095 * math.sin(2 * math.pi * 440 * i / 44100))) for i in range(44100))
                    subprocess.run([require_tool("ffmpeg"), "-v", "error", "-f", "s16le", "-ar", "44100", "-ac", "1", "-i", "pipe:0",
                                    "-t", str(candidate.duration), "-c:a", "libmp3lame", "-b:a", f"{candidate.bitrate}k", "-y", str(partial)],
                                   input=second * math.ceil(candidate.duration), check=True, capture_output=True, timeout=60)
                    partial.replace(path)
                finally:
                    partial.unlink(missing_ok=True)
        return AudioSource(url=path.as_uri(), ext="mp3", bitrate=candidate.bitrate, local_path=str(path))

    def health(self) -> Health:
        return Health(ok=True, latency_ms=0, message="离线演示可用", checked_at=now())
