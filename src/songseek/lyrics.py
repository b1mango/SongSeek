import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import SYLT, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .downloader import digest
from .lrclib import LRCLib
from .netproxy import effective_proxy
from .matching import same_recording
from .models import LyricsInfo, LyricsResponse, ParsedRow
from .normalize import artists_match, title_key, tolerance, version_flags

LYRICS_TIMEOUT = 8
CACHE_TTL = 600
# Misses are often transient rate limits, so forget them sooner.
MISS_TTL = 60
TIMESTAMP = re.compile(r"\[(\d+):([0-5]\d)(?:[.:](\d{1,3}))?\]")
HEADER = re.compile(r"^\[([a-z]+):(.*)\]$", re.I)
PLACEHOLDER = re.compile(r"纯音乐|純音樂|请欣赏|請欣賞|暂无歌词|暫無歌詞|没有歌词|無歌詞|^instrumental$|^no lyrics", re.I)


def parse_lrc(text: str):
    headers, timed, plain = {}, [], []
    offset = 0
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or re.match(r'^\{\s*"t"\s*:', line):
            continue
        header = HEADER.fullmatch(line)
        if header:
            name, value = header[1].lower(), header[2].strip()
            if name in ("ti", "ar", "al"):
                headers[name] = value
            elif name == "offset" and re.fullmatch(r"[+-]?\d+", value):
                offset = int(value)
            continue
        stamps = list(TIMESTAMP.finditer(line))
        content = TIMESTAMP.sub("", line).strip()
        if PLACEHOLDER.search(content):
            continue
        if stamps:
            for stamp in stamps:
                milliseconds = (int(stamp[1]) * 60 + int(stamp[2])) * 1000 + int((stamp[3] or "0").ljust(3, "0"))
                timed.append((milliseconds, content))
        elif content:
            plain.append(content)
    return headers, sorted(((max(0, stamp - offset), line) for stamp, line in timed), key=lambda item: item[0]), plain


def timestamp(milliseconds: int) -> str:
    minutes, remainder = divmod(milliseconds, 60000)
    seconds, fraction = divmod(remainder, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{fraction:03d}]"


def normalize_lrc(text: str, track, duration: float | None = None) -> str | None:
    headers, timed, plain = parse_lrc(text)
    if not plain and not any(line for _, line in timed):
        return None
    if duration is not None and timed and timed[-1][0] > (duration + 15) * 1000:
        return None
    for key, value in (("ti", track.title), ("ar", track.artist), ("al", track.album)):
        if not headers.get(key):
            headers[key] = " ".join(value.splitlines())
    lines = [f"[{key}:{headers[key]}]" for key in ("ti", "ar", "al")]
    lines.extend(plain)
    lines.extend(timestamp(stamp) + line for stamp, line in timed)
    return "\n".join(lines) + "\n"


def merge_translation(lrc: str, translation: str | None) -> tuple[str, bool]:
    if not translation:
        return lrc, False
    headers, timed, plain = parse_lrc(lrc)
    _, translated, translated_plain = parse_lrc(translation)
    lines = [f"[{key}:{value}]" for key, value in headers.items()] + plain
    by_time = {}
    for stamp, line in translated:
        if line:
            by_time.setdefault(stamp, []).append(line)
    merged = False
    for stamp, line in timed:
        lines.append(timestamp(stamp) + line)
        for second in by_time.pop(stamp, []):
            lines.append(timestamp(stamp) + second)
            merged = True
    if not timed and translated_plain:
        lines.extend(translated_plain)
        merged = True
    return "\n".join(lines) + "\n", merged


def atomic_lrc(path: Path, lrc: str):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".lyrics-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(lrc)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def embed_lyrics(path: Path, lrc: str):
    audio = mutagen.File(path)
    if not isinstance(audio, (MP3, FLAC, OggVorbis, OggOpus, MP4)):
        raise ValueError("unsupported lyrics tags")
    if audio.tags is None:
        audio.add_tags()
    _, timed, plain = parse_lrc(lrc)
    text = "\n".join(plain + [line for _, line in timed if line])
    if isinstance(audio, MP3):
        audio.tags.delall("USLT")
        audio.tags.delall("SYLT")
        audio.tags.add(USLT(encoding=3, lang="und", desc="", text=text))
        if timed:
            audio.tags.add(SYLT(encoding=3, lang="und", format=2, type=1, desc="", text=[(line, stamp) for stamp, line in timed]))
    elif isinstance(audio, MP4):
        audio.tags["©lyr"] = [text]
    else:
        audio.tags["LYRICS"] = [lrc]
    audio.save()


def embed_file(file, lrc: str):
    path = Path(file.path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".lyrics-", suffix=path.suffix)
    os.close(fd)
    temporary = Path(temporary)
    try:
        # 标签库可能在写入中途失败，完成后才替换已验证的音频。
        shutil.copy2(path, temporary)
        embed_lyrics(temporary, lrc)
        size, sha256 = temporary.stat().st_size, digest(temporary)
        os.replace(temporary, path)
        file.size, file.sha256 = size, sha256
    finally:
        temporary.unlink(missing_ok=True)


def has_lyrics_file(track) -> bool:
    return bool(track.file and track.file.lyrics and track.file.lyrics.path and Path(track.file.lyrics.path).is_file())


class LyricsManager:
    def __init__(self, service):
        self.service = service
        self.cache: dict[tuple[int, float | None], tuple[float, LyricsResponse]] = {}
        self.lock = threading.Lock()

    def _request(self, provider, operation, deadline):
        def run():
            if provider is None:
                return operation()
            with provider.search_budget(deadline):
                provider.remaining(8)
                return operation()
        future = self.service.provider_pool.submit(run)
        try:
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            future.cancel()
            return None

    def fetch(self, track, chosen) -> LyricsResponse:
        service = self.service
        duration = track.file.duration if track.file else chosen.duration or track.duration
        infos = {p.id: p for p in service.config.providers}
        alternatives = sorted((c for c in service.candidates(track.id) if c.id != chosen.id
                               and infos[c.provider].enabled and same_recording(track, chosen, c)),
                              key=lambda c: (infos[c.provider].order, c.rank))
        return self.fetch_raws(track, [service.raw_candidate(c.id) for c in [chosen] + alternatives], duration)

    def fetch_raws(self, track, raws, duration) -> LyricsResponse:
        service = self.service
        deadline = time.monotonic() + LYRICS_TIMEOUT
        infos = {p.id: p for p in service.config.providers}
        chosen = raws[0]
        flags = version_flags(chosen.title)
        fallback = LyricsResponse()
        seen = set()

        def accept(result, source, label):
            nonlocal fallback
            if result is None:
                return None
            try:
                lrc = normalize_lrc(result.lrc, track, duration)
                translation = normalize_lrc(result.translation, track, duration) if result.translation else None
            except (TypeError, ValueError, AttributeError):
                return None
            if lrc is None:
                return None
            response = LyricsResponse(lrc=lrc, synced=bool(TIMESTAMP.search(lrc)), source=source,
                                      source_label=label, translation=translation)
            if response.synced:
                return response
            if fallback.lrc is None:
                fallback = response
            return None

        def attempt(raw):
            identity = (raw.provider, raw.source_id)
            if identity in seen or time.monotonic() >= deadline:
                return None
            seen.add(identity)
            provider = service.providers[raw.provider]
            result = self._request(provider, lambda: provider.lyrics(raw), deadline)
            return accept(result, raw.provider, infos[raw.provider].label)

        for raw in raws:
            response = attempt(raw)
            if response:
                return response
        if "netease" in service.providers and infos["netease"].enabled and duration is not None and time.monotonic() < deadline:
            provider = service.providers["netease"]
            found = self._request(provider, lambda: provider.search(f"{track.title} {track.artist}".strip(), 15), deadline)
            for raw in found or []:
                if (title_key(raw.title) == title_key(chosen.title) == title_key(track.title)
                        and artists_match(track.artist, raw.artist)
                        and set(version_flags(raw.title)) == set(flags) == set(version_flags(track.title))
                        and raw.duration is not None and abs(raw.duration - duration) <= tolerance(duration)):
                    response = attempt(raw)
                    if response:
                        return response
        if "kugou" in service.providers and infos["kugou"].enabled and duration is not None and time.monotonic() < deadline:
            provider = service.providers["kugou"]
            result = self._request(provider, lambda: provider.lyrics_keyword(track, duration), deadline)
            response = accept(result, "kugou", infos["kugou"].label)
            if response:
                return response
        if duration is not None and time.monotonic() < deadline:
            source = LRCLib(effective_proxy(service.config.settings) or "")
            result = self._request(None, lambda: source.lyrics(track, duration, deadline), deadline)
            response = accept(result, "lrclib", "LRCLIB")
            if response:
                return response
        return fallback

    def artist_song(self, token: str) -> LyricsResponse:
        raws = self.service.artist_songs.get(token)
        raw = raws[0]
        track = ParsedRow(title=raw.title, artist=raw.artist, album=raw.album, duration=raw.duration)
        return self.fetch_raws(track, raws, raw.duration)

    def preview(self, candidate_id: int, *, downloaded_track=None) -> LyricsResponse:
        candidate = self.service.candidate(candidate_id)
        if downloaded_track is None and not self.service.config.settings.lyrics:
            return LyricsResponse()
        track = downloaded_track or self.service.track(candidate.track_id)
        # 预览其他候选时应按该候选的时长，而不是已下载文件的时长匹配。
        if downloaded_track is None:
            track.file = None
        duration = track.file.duration if track.file else candidate.duration or track.duration
        cache_key = (candidate_id, duration)
        with self.lock:
            cached = self.cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1].model_copy()
        try:
            result = self.fetch(track, candidate)
        except Exception:
            result = LyricsResponse()
        with self.lock:
            current = time.monotonic()
            self.cache = {key: value for key, value in self.cache.items() if value[0] > current}
            self.cache[cache_key] = (current + (CACHE_TTL if result.lrc else MISS_TTL), result)
        return result.model_copy()

    def save(self, track):
        info = LyricsInfo(source=track.file.provider or "", source_label=track.file.provider_label or "")
        try:
            chosen = self.service.candidate(track.chosen_candidate_id)
            info.source, info.source_label = chosen.provider, chosen.provider_label
            result = self.preview(chosen.id, downloaded_track=track)
            if result.lrc is None:
                info.error = "没有找到匹配的歌词"
                return info
            info.source, info.source_label, info.synced = result.source, result.source_label, result.synced
            lrc = result.lrc
            if self.service.config.settings.lyrics_translation:
                lrc, info.translated = merge_translation(lrc, result.translation)
            path = Path(track.file.path).with_suffix(".lrc")
            atomic_lrc(path, lrc)
            info.path = str(path)
            if self.service.config.settings.lyrics_embed:
                try:
                    embed_file(track.file, lrc)
                    info.embedded = True
                except Exception:
                    info.error = "歌词已保存，但嵌入音频失败"
            with self.lock:
                self.cache = {key: value for key, value in self.cache.items() if key[0] != chosen.id}
        except Exception:
            info.error = "歌词获取或保存失败"
        return info

    def read(self, track) -> LyricsResponse:
        if not self.service.config.settings.lyrics:
            return LyricsResponse()
        if has_lyrics_file(track):
            try:
                info = track.file.lyrics
                lrc = Path(info.path).read_text("utf-8")
                return LyricsResponse(lrc=lrc, synced=bool(TIMESTAMP.search(lrc)), source=info.source, source_label=info.source_label)
            except (OSError, UnicodeError):
                pass
        return self.preview(track.chosen_candidate_id, downloaded_track=track) if track.chosen_candidate_id else LyricsResponse()
