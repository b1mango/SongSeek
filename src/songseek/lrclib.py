import time

import httpx

from .models import Lyrics
from .normalize import artists_match, title_key, tolerance, version_flags
from .providers.base import USER_AGENT


class LRCLib:
    def __init__(self, proxy: str = ""):
        self.proxy = proxy

    def client(self, proxy, timeout):
        return httpx.Client(proxy=proxy, trust_env=False, timeout=timeout,
                            headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    def request(self, path, params, deadline):
        for proxy in [None] + ([self.proxy] if self.proxy else []):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("lyrics deadline")
            try:
                with self.client(proxy, min(3, remaining)) as client:
                    response = client.get(f"https://lrclib.net/api/{path}", params=params)
                    if response.status_code == 404:
                        return None
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPError:
                if proxy or not self.proxy:
                    raise

    def lyrics(self, track, duration: float, deadline: float) -> Lyrics | None:
        params = {"track_name": track.title, "artist_name": track.artist}
        result = self.request("get", {**params, "album_name": track.album, "duration": int(duration)}, deadline)
        if result is not None:
            return self.parse(result)
        fallback = None
        for candidate in self.request("search", params, deadline) or []:
            try:
                actual_duration = float(candidate.get("duration", 0))
            except (TypeError, ValueError):
                continue
            if (title_key(candidate.get("trackName", "")) != title_key(track.title)
                    or not artists_match(track.artist, candidate.get("artistName", ""))
                    or set(version_flags(candidate.get("trackName", ""))) != set(version_flags(track.title))
                    or not abs(actual_duration - duration) <= max(tolerance(duration), 6)):
                continue
            lyrics = self.parse(candidate)
            if lyrics and candidate.get("syncedLyrics"):
                return lyrics
            if fallback is None:
                fallback = lyrics
        return fallback

    @staticmethod
    def parse(result) -> Lyrics | None:
        if result.get("instrumental"):
            return None
        text = result.get("syncedLyrics") or result.get("plainLyrics")
        return Lyrics(text) if text else None
