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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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
from .covers import fetch_cover, image_type
from .tools import require_tool
from .errors import SongSeekError, storage_error
from .models import AudioSource, DownloadedFile, RawCandidate, Track
from .media_url import secure_media_client
from .normalize import tolerance
from .providers.base import BaseProvider, audio_url

_publish_lock = threading.Lock()


@dataclass(frozen=True)
class DownloadDestination:
    directory: Path
    filename_template: str


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe(path: Path) -> dict:
    try:
        result = subprocess.run([require_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,bit_rate", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
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
        decoded = subprocess.run([require_tool("ffmpeg"), "-v", "error", "-xerror", "-i", str(path), "-map", "0:a", "-f", "null", "-"], capture_output=True, text=True, timeout=max(90, duration * 2))
    except FileNotFoundError:
        raise SongSeekError("未安装系统 ffmpeg", "missing_tool") from None
    except subprocess.TimeoutExpired:
        raise SongSeekError("全曲解码超时", "invalid_audio") from None
    if decoded.returncode or decoded.stderr.strip():
        raise SongSeekError("全曲解码失败，文件可能损坏", "invalid_audio")
    return result


def transfer(source: AudioSource, target: Path, provider: BaseProvider, max_size: int = 1024 ** 3,
             on_progress: Callable[[int, int | None], None] | None = None) -> None:
    if source.local_path:
        shutil.copyfile(source.local_path, target)
        if on_progress:
            size = target.stat().st_size
            on_progress(size, size)
        return
    audio_url(source.url)
    started = time.monotonic()
    headers = provider.headers() | source.headers
    with provider.semaphore, provider.client(timeout=30) as client:
        secure_media_client(client)
        with client.stream("GET", source.url, headers=headers) as response:
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
            total = int(size) if size is not None else None
            count = 0
            with target.open("wb") as stream:
                for chunk in response.iter_bytes(256 * 1024):
                    count += len(chunk)
                    if count > max_size or time.monotonic() - started > 600:
                        raise SongSeekError("音频超过大小或下载时间限制", "download_limit")
                    stream.write(chunk)
                    if on_progress:
                        on_progress(count, total)
            if total is not None and count != total:
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
    mime = image_type(cover) if cover else "image/jpeg"
    if isinstance(audio, MP4):
        audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"], audio["\xa9cmt"] = [title], [artist], [album], [source]
        if cover and mime in ("image/jpeg", "image/png"):
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
    except MutagenError as error:
        if error.args and isinstance(error.args[0], OSError):
            raise storage_error(error.args[0]) from None
        raise SongSeekError("音频标签写入失败", "tag_failed") from None


def remux(path: Path, ext: str) -> None:
    # DASH AAC 是独立 m4s；WebM Opus 也需换成可写标签的音频容器。
    output = path.with_suffix(".remux.part")
    try:
        result = subprocess.run([require_tool("ffmpeg"), "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "ipod" if ext == "m4a" else "opus", "-y", str(output)], capture_output=True, timeout=120)
        if result.returncode:
            if any(message in result.stderr.lower() for message in
                   (b"no space left on device", b"disk quota exceeded", b"permission denied", b"read-only file system", b"not a directory", b"is a directory")):
                raise SongSeekError("音频保存失败，请清理磁盘空间或在设置中更换有写入权限的输出目录后重试", "storage_error")
            raise SongSeekError("音频容器转换失败", "invalid_audio")
        output.chmod(path.stat().st_mode & 0o777)
        os.replace(output, path)
    finally:
        output.unlink(missing_ok=True)


def publish(partial: Path, target: Path, prepare: Callable[[Path, str], None] | None = None) -> Path:
    # 同一目录的锁让多进程也不会相互覆盖最终文件。锁文件放在系统临时目录，
    # 不在用户的音乐文件夹里留下文件（Windows 不隐藏点开头的文件）。
    key = hashlib.sha1(str(target.parent.resolve()).encode()).hexdigest()[:16]
    with _publish_lock, (Path(tempfile.gettempdir()) / f"songseek-publish-{key}.lock").open("a+b") as lock:
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
            # A name is free only if no file of any extension shares its stem, so the .lrc next to it is ours alone.
            stems = {path.stem.casefold() for path in target.parent.iterdir()}
            sha = digest(partial)
            chosen, index = target, 1
            while chosen.stem.casefold() in stems:
                if chosen.exists() and digest(chosen) == sha:  # the very same audio is already here: reuse it
                    if prepare:
                        prepare(chosen, sha)
                    partial.unlink()
                    return chosen
                index += 1
                chosen = target.with_name(f"{target.stem} ({index}){target.suffix}")
            if prepare:
                prepare(chosen, sha)
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

    def destination(self, playlist_name: str) -> DownloadDestination:
        settings = self.config.settings
        directory = Path(settings.output_dir)
        if settings.group_by_playlist:
            directory /= safe_name(playlist_name)
        return DownloadDestination(directory, settings.filename_template)

    def cleanup_partials(self):
        directory = Path(self.config.settings.output_dir)
        try:
            if not directory.is_dir():
                return
            directories = [directory] + [path for path in directory.iterdir() if path.is_dir() and not path.is_symlink()]
        except OSError:
            return  # 输出目录不可用时仍允许启动，以便在设置中更换目录。
        for folder in directories:
            try:
                for path in folder.glob(".songseek-*.part"):
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
            except OSError:
                continue

    def recovered_file(self, path: Path, raw: RawCandidate) -> DownloadedFile:
        info = probe(path)
        return DownloadedFile(path=str(path.resolve()), size=path.stat().st_size, format=info["format"], bitrate=info["bitrate"], duration=info["duration"], sha256=digest(path),
                              provider=raw.provider, provider_label=next((p.label for p in self.config.providers if p.id == raw.provider), raw.provider))

    def download(self, track: Track, raw: RawCandidate, provider: BaseProvider, playlist_name: str,
                 on_progress: Callable[[str, int, int | None], None] | None = None, *,
                 destination: DownloadDestination | None = None,
                 on_publish: Callable[[Path, str], None] | None = None) -> DownloadedFile:
        destination = destination or self.destination(playlist_name)
        directory = destination.directory
        partial = directory / f".songseek-{uuid.uuid4().hex}.part"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            source = provider.resolve(raw)
            downloaded, total = 0, None
            def progress(count: int, size: int | None):
                nonlocal downloaded, total
                downloaded, total = count, size
                if on_progress:
                    on_progress("download", count, size)
            transfer(source, partial, provider, on_progress=progress if on_progress else None)
            if on_progress:
                on_progress("finishing", downloaded, total)
            if raw.provider in ("bilibili", "youtube") and source.ext in ("m4a", "opus"):
                remux(partial, source.ext)
            info = verify(partial, track.duration if track.duration is not None else raw.duration)
            cover = None
            if raw.extra.get("cover_url"):
                result = fetch_cover(raw.extra["cover_url"], provider)
                if result is not None:
                    cover = result[0]
            tag(partial, track, raw, cover)
            filename = safe_name(destination.filename_template.format(artist=track.artist or raw.artist, title=track.title, album=track.album or raw.album))
            target = publish(partial, directory / f"{filename}.{info['format']}", prepare=on_publish) if on_publish else publish(partial, directory / f"{filename}.{info['format']}")
            return DownloadedFile(path=str(target.resolve()), size=target.stat().st_size, format=info["format"], bitrate=info["bitrate"], duration=info["duration"], sha256=digest(target),
                                  provider=raw.provider, provider_label=next((p.label for p in self.config.providers if p.id == raw.provider), raw.provider))
        except OSError as error:
            raise storage_error(error) from None
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError as error:
                raise storage_error(error) from None


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
