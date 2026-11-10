import json
import os
import re
import string
import tempfile
import threading
from pathlib import Path
from .netproxy import validate_proxy

from .errors import SongSeekError
from .models import ProviderInfo, Settings

PROVIDERS = [
    ProviderInfo(id="netease", label="网易云", max_quality="320k", needs="cookie", order=0),
    ProviderInfo(id="vkeys_qq", label="QQ 音乐（VKeys）", max_quality="320k", order=1),
    ProviderInfo(id="kugou", label="酷狗", max_quality="128k", order=2),
    ProviderInfo(id="zpod", label="zpod", max_quality="128k", order=3),
    ProviderInfo(id="bilibili", label="哔哩哔哩", max_quality="160k AAC", order=4),
    ProviderInfo(id="youtube", label="YouTube", max_quality="不定", needs="proxy", order=5),
]


def netease_cookie_header(cookie: str) -> str:
    """Extract the login and optional CSRF cookies without retaining unrelated account data."""
    if any(ord(char) < 32 or ord(char) == 127 for char in cookie):
        raise SongSeekError("Cookie 不能包含换行或控制字符，请粘贴单行内容", "invalid_settings")
    values: dict[str, str] = {}
    for item in cookie.split(";"):
        key, separator, value = item.strip().partition("=")
        key = re.sub(r"^cookie\s*:\s*", "", key.strip(), flags=re.IGNORECASE)
        if not separator or key not in ("MUSIC_U", "__csrf"):
            continue
        value = value.strip()
        if not re.fullmatch(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+", value):
            raise SongSeekError(f"{key} 的值为空或格式不正确，请重新复制 Cookie", "invalid_settings")
        if key in values and values[key] != value:
            raise SongSeekError(f"检测到多个不同的 {key}，请只粘贴同一条 Cookie", "invalid_settings")
        values[key] = value
    if "MUSIC_U" not in values:
        raise SongSeekError("没有找到 MUSIC_U，请复制网易云登录后的 Cookie", "invalid_settings")
    return "; ".join(f"{key}={values[key]}" for key in ("MUSIC_U", "__csrf") if key in values)


def atomic_json(path: Path, value: object) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Config:
    def __init__(self, home: Path | None = None):
        self.home = (home or Path(os.environ.get("SONGSEEK_HOME", "~/.songseek"))).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.mock = os.environ.get("SONGSEEK_MOCK") == "1"
        self._lock = threading.RLock()
        self.path = self.home / "settings.json"
        stored = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        values = stored.get("settings", {})
        if "proxy_mode" not in values:
            values["proxy_mode"] = "manual" if values.get("proxy") else "auto"
        self.settings = Settings(**values)
        self.settings.output_dir = str(Path(self.settings.output_dir).expanduser().resolve())
        self.settings.netease_cookie_set = self.cookie_path.is_file()
        self.providers = [p.model_copy(deep=True) for p in PROVIDERS]
        for provider in self.providers:
            saved = stored.get("providers", {}).get(provider.id, {})
            provider.enabled = saved.get("enabled", True)
            provider.order = saved.get("order", provider.order)
        if self.mock:
            self.providers = [ProviderInfo(id="mock", label="离线演示", max_quality="192k", order=0)]

    @property
    def cookie_path(self) -> Path:
        return self.home / "netease_cookie"

    def save(self) -> None:
        with self._lock:
            settings = self.settings.model_dump(exclude={"netease_cookie_set"})
            providers = {p.id: {"enabled": p.enabled, "order": p.order} for p in self.providers if p.id != "mock"}
            if self.mock and self.path.exists():
                providers = json.loads(self.path.read_text("utf-8")).get("providers", {})
            atomic_json(self.path, {"settings": settings, "providers": providers})

    def update(self, values: dict) -> Settings:
        with self._lock:
            values = dict(values)
            cookie = values.pop("netease_cookie", None)
            values.pop("netease_cookie_set", None)
            settings = Settings(**(self.settings.model_dump() | values))
            try:
                validate_proxy(settings.proxy)
            except ValueError:
                raise SongSeekError("代理地址格式不正确", "invalid_settings") from None
            try:
                fields = [name for _, name, _, _ in string.Formatter().parse(settings.filename_template) if name is not None]
                if any(name not in ("artist", "title", "album") for name in fields):
                    raise ValueError
                settings.filename_template.format(artist="歌手", title="歌名", album="专辑")
            except (ValueError, KeyError, IndexError):
                raise SongSeekError("文件名模板只支持 {artist}、{title}、{album}", "invalid_settings") from None
            if not settings.output_dir.strip():
                raise SongSeekError("输出目录不能为空", "invalid_settings")
            settings.output_dir = str(Path(settings.output_dir).expanduser().resolve())
            if cookie is not None:
                if not isinstance(cookie, str):
                    raise SongSeekError("Cookie 格式不正确", "invalid_settings")
                if cookie:
                    cookie = netease_cookie_header(cookie)
                    fd, temporary = tempfile.mkstemp(dir=self.home, prefix=".cookie-")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as stream:
                            os.fchmod(stream.fileno(), 0o600)
                            stream.write(cookie)
                        os.replace(temporary, self.cookie_path)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                else:
                    self.cookie_path.unlink(missing_ok=True)
            settings.netease_cookie_set = self.cookie_path.is_file()
            self.settings = settings
            self.save()
            return self.settings

    def update_providers(self, updates: list[dict]) -> list[ProviderInfo]:
        with self._lock:
            by_id = {p.id: p for p in self.providers}
            if any(value["id"] not in by_id for value in updates):
                raise SongSeekError("音源不存在", "unknown_provider")
            for value in updates:
                by_id[value["id"]].enabled = value["enabled"]
                by_id[value["id"]].order = value["order"]
            self.save()
            return sorted(self.providers, key=lambda p: p.order)
