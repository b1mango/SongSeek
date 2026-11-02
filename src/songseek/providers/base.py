import threading
import time
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

import httpx

from ..config import Config
from ..errors import SongSeekError, public_error
from ..models import AudioSource, Health, RawCandidate, now

USER_AGENT = "Mozilla/5.0 (SongSeek local music tool)"


def audio_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise SongSeekError("音源未返回有效音频地址", "invalid_audio_url")
    return url


class BaseProvider(ABC):
    id: str
    referer = ""

    def __init__(self, config: Config):
        self.config = config
        self.semaphore = threading.BoundedSemaphore(2)

    def client(self, timeout: float = 8) -> httpx.Client:
        return httpx.Client(proxy=self.config.settings.proxy or None, trust_env=False,
                            timeout=timeout, follow_redirects=True,
                            headers={"User-Agent": USER_AGENT, "Referer": self.referer})

    def request(self, method: str, url: str, **kwargs) -> dict:
        with self.semaphore, self.client() as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()

    @abstractmethod
    def search(self, query: str, limit: int = 15) -> list[RawCandidate]: ...

    @abstractmethod
    def resolve(self, candidate: RawCandidate) -> AudioSource: ...

    def health(self) -> Health:
        started = time.monotonic()
        try:
            found = self.search("晴天 周杰伦", 1)
            return Health(ok=bool(found), latency_ms=round((time.monotonic() - started) * 1000),
                          message="搜索正常；取流以下载结果为准" if found else "接口可连接，未返回候选", checked_at=now())
        except Exception as error:
            return Health(ok=False, latency_ms=round((time.monotonic() - started) * 1000), message=public_error(error), checked_at=now())

    def headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Referer": self.referer, "Accept-Encoding": "identity"}
