import importlib.util
import os
import shutil
import sys
from pathlib import Path

from .errors import SongSeekError


def resource_dir() -> Path:
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]


def resolve_tool(name: str) -> str | None:
    executable = name + (".exe" if sys.platform == "win32" else "")
    bundled = resource_dir() / "bin" / executable
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    if path := shutil.which(name):
        return path
    if sys.platform == "darwin":
        for directory in ("/opt/homebrew/bin", "/usr/local/bin"):
            path = Path(directory) / name
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None


def require_tool(name: str) -> str:
    if path := resolve_tool(name):
        return path
    raise SongSeekError(f"未安装系统 {name}", "missing_tool")


def yt_dlp_command() -> list[str]:
    return [sys.executable, "--run-yt-dlp"] if getattr(sys, "frozen", False) else [sys.executable, "-m", "yt_dlp"]


def availability() -> dict:
    return {"ffmpeg": resolve_tool("ffmpeg") is not None, "ffprobe": resolve_tool("ffprobe") is not None,
            "yt_dlp": importlib.util.find_spec("yt_dlp") is not None}
