import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import mutagen
from mutagen import MutagenError
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from .config import Config
from .errors import SongSeekError
from .models import AudioSource, DownloadedFile, RawCandidate, Track
from .normalize import tolerance
from .providers.base import BaseProvider, audio_url

_publish_lock = threading.Lock()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe(path: Path) -> dict:
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,bit_rate", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SongSeekError("未安装系统 ffprobe", "missing_tool") from None
    except subprocess.TimeoutExpired:
        raise SongSeekError("音频探测超时", "invalid_audio") from None
    if result.returncode:
        raise SongSeekError("无法读取音频，可能是错误页或损坏文件", "invalid_audio")
    data = json.loads(result.stdout)
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    duration = float(data.get("format", {}).get("duration", 0))
    if not streams or not math.isfinite(duration) or duration <= 0:
        raise SongSeekError("文件没有有效音频或时长", "invalid_audio")
    codec = streams[0]["codec_name"]
    formats = data["format"]["format_name"].split(",")
    ext = "m4a" if "mov" in formats else "opus" if codec == "opus" and "ogg" in formats else formats[0]
    return {"duration": duration, "bitrate": round(int(streams[0].get("bit_rate", 0)) / 1000) or None, "format": ext, "codec": codec}


def verify(path: Path, expected: float | None = None) -> dict:
    result = probe(path)
    duration = result["duration"]
    if expected is not None and abs(duration - expected) > tolerance(expected):
        raise SongSeekError(f"实际时长 {duration:.1f}s 与目标 {expected:.1f}s 不符，已拦截", "duration_mismatch")
    try:
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, timeout=max(90, duration * 2))
    except FileNotFoundError:
        raise SongSeekError("未安装系统 ffmpeg", "missing_tool") from None
    except subprocess.TimeoutExpired:
        raise SongSeekError("全曲解码超时", "invalid_audio") from None
    if decoded.returncode or decoded.stderr.strip():
        raise SongSeekError("全曲解码失败，文件可能损坏", "invalid_audio")
    return result


def transfer(source: AudioSource, target: Path, provider: BaseProvider, max_size: int = 1024 ** 3) -> None:
    if source.local_path:
        shutil.copyfile(source.local_path, target)
        return
    audio_url(source.url)
    started = time.monotonic()
    with provider.semaphore, provider.client(timeout=30) as client:
        with client.stream("GET", source.url, headers=provider.headers() | source.headers) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/" in content_type or "json" in content_type:
                raise SongSeekError("音频地址返回了网页或错误信息", "invalid_audio")
            size = response.headers.get("content-length")
            if response.status_code == 206:
                matched = re.fullmatch(r"bytes 0-(\d+)/(\d+)", response.headers.get("content-range", ""))
                if not matched or int(matched[1]) + 1 != int(matched[2]):
                    raise SongSeekError("音源只返回了部分文件", "incomplete_audio")
                size = matched[2]
            count = 0
            with target.open("wb") as stream:
                for chunk in response.iter_bytes(256 * 1024):
                    count += len(chunk)
                    if count > max_size or time.monotonic() - started > 600:
                        raise SongSeekError("音频超过大小或下载时间限制", "download_limit")
                    stream.write(chunk)
            if size is not None and count != int(size):
                raise SongSeekError("下载大小与音源声明不一致", "incomplete_audio")


def safe_name(value: str) -> str:
    value = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", value).strip(" .")
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}:
        value = "_" + value
    while len(value.encode("utf-8")) > 180:
        value = value[:-1]
    return value or "未命名"


def tag(path: Path, track: Track, raw: RawCandidate, cover: bytes | None = None) -> None:
    audio = mutagen.File(path)
    if audio is None:
        raise SongSeekError("无法识别音频标签格式", "invalid_audio")
    if not isinstance(audio, (MP4, MP3, FLAC, OggOpus, OggVorbis, AIFF, WAVE)):
        return
    if audio.tags is None:
        audio.add_tags()
    title, artist, album = track.title, track.artist or raw.artist, track.album or raw.album
    source = f"provider={raw.provider}; id={raw.source_id}"
    mime = "image/png" if cover and cover.startswith(b"\x89PNG") else "image/jpeg"
    if isinstance(audio, MP4):
        audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"], audio["\xa9cmt"] = [title], [artist], [album], [source]
        if cover:
            audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG)]
    elif isinstance(audio.tags, mutagen.id3.ID3):
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        audio.tags.add(TXXX(encoding=3, desc="SOURCE", text=source))
        if cover:
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, data=cover))
    elif isinstance(audio, (FLAC, OggOpus, OggVorbis)):
        audio["title"], audio["artist"], audio["album"], audio["source"] = [title], [artist], [album], [source]
        if cover and hasattr(audio, "add_picture"):
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, mime, cover
            audio.add_picture(picture)
    else:
        return
    try:
        audio.save()
    except MutagenError:
        raise SongSeekError("音频标签写入失败", "tag_failed") from None


def remux(path: Path, ext: str) -> None:
    # DASH AAC 是独立 m4s；WebM Opus 也需换成可写标签的音频容器。
    output = path.with_suffix(".remux.part")
    try:
        result = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "ipod" if ext == "m4a" else "opus", "-y", str(output)], capture_output=True, timeout=120)
        if result.returncode:
            raise SongSeekError("音频容器转换失败", "invalid_audio")
        os.replace(output, path)
    finally:
        output.unlink(missing_ok=True)


def publish(partial: Path, target: Path) -> Path:
    # 同一目录的锁让多进程也不会相互覆盖最终文件。
    with _publish_lock, (target.parent / ".songseek-publish.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            sha = digest(partial)
            chosen, index = target, 1
            while chosen.exists():
                if digest(chosen) == sha:
                    partial.unlink()
                    return chosen
                index += 1
                chosen = target.with_name(f"{target.stem} ({index}){target.suffix}")
            os.replace(partial, chosen)
            return chosen
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


class Downloader:
    def __init__(self, config: Config):
        self.config = config

    def download(self, track: Track, raw: RawCandidate, provider: BaseProvider, playlist_name: str) -> DownloadedFile:
        settings = self.config.settings
        directory = Path(settings.output_dir)
        if settings.group_by_playlist:
            directory /= safe_name(playlist_name)
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".songseek-", suffix=".part", dir=directory)
        os.close(fd)
        partial = Path(name)
        try:
            source = provider.resolve(raw)
            transfer(source, partial, provider)
            if raw.provider in ("bilibili", "youtube") and source.ext in ("m4a", "opus"):
                remux(partial, source.ext)
            info = verify(partial, track.duration if track.duration is not None else raw.duration)
            if raw.provider == "vkeys_qq" and (info["codec"] != "mp3" or info["bitrate"] != 320):
                raise SongSeekError("QQ 实际文件不是 320k MP3", "quality_mismatch")
            cover = None
            if raw.extra.get("cover_url"):
                with provider.semaphore, provider.client() as client:
                    with client.stream("GET", audio_url(raw.extra["cover_url"])) as response:
                        response.raise_for_status()
                        chunks = bytearray()
                        for chunk in response.iter_bytes(65536):
                            chunks.extend(chunk)
                            if len(chunks) > 10 * 1024 * 1024:
                                raise SongSeekError("封面文件过大", "invalid_cover")
                        if not chunks.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                            raise SongSeekError("封面不是有效的 JPEG 或 PNG", "invalid_cover")
                        cover = bytes(chunks)
            tag(partial, track, raw, cover)
            filename = safe_name(settings.filename_template.format(artist=track.artist or raw.artist, title=track.title, album=track.album or raw.album))
            target = publish(partial, directory / f"{filename}.{info['format']}")
            return DownloadedFile(path=str(target.resolve()), size=target.stat().st_size, format=info["format"], bitrate=info["bitrate"], duration=info["duration"], sha256=digest(target),
                                  provider=raw.provider, provider_label=next((p.label for p in self.config.providers if p.id == raw.provider), raw.provider))
        finally:
            partial.unlink(missing_ok=True)


class PreviewCache:
    def __init__(self, config: Config, limit: int = 500 * 1024 * 1024):
        self.directory = config.home / "cache" / "preview"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.lock = threading.Lock()

    def get(self, raw: RawCandidate, source: AudioSource, provider: BaseProvider) -> Path:
        key = hashlib.sha256((raw.provider + ":" + raw.source_id).encode()).hexdigest()
        with self.lock:
            files = list(self.directory.glob(key + ".*"))
            if files:
                os.utime(files[0], None)
                return files[0]
            fd, filename = tempfile.mkstemp(prefix=".preview-", suffix=".part", dir=self.directory)
            os.close(fd)
            partial = Path(filename)
            try:
                transfer(source, partial, provider, max_size=self.limit)
                if raw.provider in ("bilibili", "youtube") and source.ext in ("m4a", "opus"):
                    remux(partial, source.ext)
                if partial.stat().st_size > self.limit:
                    raise SongSeekError("试听文件超过缓存大小限制", "download_limit")
                info = probe(partial)
                target = self.directory / f"{key}.{info['format']}"
                os.replace(partial, target)
                files = sorted(self.directory.glob("*"), key=lambda p: p.stat().st_mtime)
                size = sum(p.stat().st_size for p in files)
                for path in files:
                    if size <= self.limit:
                        break
                    if path != target:
                        size -= path.stat().st_size
                        path.unlink()
                return target
            finally:
                partial.unlink(missing_ok=True)
