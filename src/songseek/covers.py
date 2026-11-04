import threading
import time
from collections import OrderedDict

import httpx
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from .errors import SongSeekError
from .matching import same_recording
from .providers.base import BaseProvider, audio_url

MAX_SIZE = 5 * 1024 * 1024
TIMEOUT = 5


def image_type(data: bytes) -> str:
    if len(data) > MAX_SIZE:
        raise SongSeekError("封面文件过大", "invalid_cover")
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise SongSeekError("封面不是有效的 JPEG、PNG 或 WebP", "invalid_cover")


def fetch_cover(url: str, provider: BaseProvider) -> tuple[bytes, str] | None:
    if not provider.semaphore.acquire(timeout=TIMEOUT):
        return None
    try:
        started = time.monotonic()
        with provider.client(timeout=TIMEOUT) as client:
            with client.stream("GET", audio_url(url)) as response:
                response.raise_for_status()
                if int(response.headers.get("content-length", 0)) > MAX_SIZE:
                    return None
                chunks = bytearray()
                for chunk in response.iter_bytes(65536):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_SIZE or time.monotonic() - started > TIMEOUT:
                        return None
                data = bytes(chunks)
                return data, image_type(data)
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, SongSeekError):
        return None
    finally:
        provider.semaphore.release()


def embedded_cover(path: str) -> tuple[bytes, str] | None:
    try:
        audio = mutagen.File(path)
        if isinstance(audio, MP4):
            images = audio.get("covr", [])
        elif isinstance(audio, FLAC):
            images = [picture.data for picture in audio.pictures]
        elif audio is not None and isinstance(audio.tags, ID3):
            images = [picture.data for picture in audio.tags.getall("APIC")]
        else:
            return None
        for image in images:
            try:
                data = bytes(image)
                return data, image_type(data)
            except SongSeekError:
                continue
    except (OSError, mutagen.MutagenError, ValueError):
        return None
    return None


class CoverManager:
    def __init__(self, service):
        self.service = service
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def candidate(self, identifier: int) -> tuple[bytes, str]:
        chosen = self.service.candidate(identifier)
        track = self.service.track(chosen.track_id)
        alternatives = [candidate for candidate in self.service.candidates(track.id)
                        if candidate.id != chosen.id and candidate.verdict != "reject"]
        alternatives.sort(key=lambda candidate: not same_recording(track, chosen, candidate))
        return self.raws([self.service.raw_candidate(candidate.id) for candidate in [chosen] + alternatives])

    def raws(self, raws) -> tuple[bytes, str]:
        attempted = set()
        for raw in raws:
            url = raw.extra.get("cover_url")
            key = (raw.provider, url)
            if not url or key in attempted:
                continue
            attempted.add(key)
            with self.lock:
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return self.cache[key]
            result = fetch_cover(url, self.service.providers[raw.provider])
            if result is not None:
                with self.lock:
                    self.cache[key] = result
                    self.cache.move_to_end(key)
                    if len(self.cache) > 64:
                        self.cache.popitem(last=False)
                return result
        raise SongSeekError("没有找到可用封面", "cover_not_found", 404)

    def track(self, identifier: int) -> tuple[bytes, str]:
        track = self.service.track(identifier)
        if track.file:
            result = embedded_cover(track.file.path)
            if result is not None:
                return result
        if track.chosen_candidate_id:
            return self.candidate(track.chosen_candidate_id)
        raise SongSeekError("没有找到可用封面", "cover_not_found", 404)
