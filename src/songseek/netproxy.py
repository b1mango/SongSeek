import errno
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit

import httpx

from .models import Settings

PORTS = (7890, 7897, 7891, 1080, 1082, 10808, 10809, 8889, 6152, 6153)
_lock = threading.Lock()
_cached = None
_expires = 0.0


def validate_proxy(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname
                or parsed.username or parsed.password or parsed.port == 0):
            raise ValueError
    except ValueError:
        raise ValueError("代理地址格式不正确") from None
    return value


def system_proxies() -> tuple[dict, bool]:
    if sys.platform == "darwin":
        # getproxies() skips sysconf even when the only environment entry is no_proxy.
        proxies = urllib.request.getproxies_macosx_sysconf()
        # _scproxy does not expose PAC; SystemConfiguration does.
        result = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=2)
        pac = "ProxyAutoConfigEnable : 1" in result.stdout or "ProxyAutoDiscoveryEnable : 1" in result.stdout
        return proxies, pac
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                pac = bool(winreg.QueryValueEx(key, "AutoConfigURL")[0])
        except OSError:
            pac = False
        return urllib.request.getproxies_registry(), pac
    return {}, False


def _usable(value):
    try:
        return validate_proxy(value)
    except ValueError:
        return None


def auto_proxies() -> dict:
    global _cached, _expires
    with _lock:
        if _cached is not None and time.monotonic() < _expires:
            return dict(_cached)
        env = next((value for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")
                    if (value := _usable(os.environ.get(key, "")))), None)
        try:
            proxies, pac = system_proxies()
        except (OSError, subprocess.TimeoutExpired):
            proxies, pac = {}, False
        system = None
        for scheme in ("https", "http", "socks"):
            value = proxies.get(scheme, "")
            if value and "://" not in value:
                value = ("socks5" if scheme == "socks" else "http") + "://" + value
            if scheme == "socks":
                value = "socks5://" + value.split("://", 1)[-1] if value else ""
            if system := _usable(value):
                break
        _cached = {"env": env, "system": system, "pac": pac}
        _expires = time.monotonic() + 15
        return dict(_cached)


def local_candidates() -> list[str]:
    found = []
    for port in PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.08):
                found.append(f"http://127.0.0.1:{port}")
        except OSError:
            pass
    return found


def effective_proxy(settings: Settings) -> str | None:
    if settings.proxy_mode == "off":
        return None
    if settings.proxy_mode == "manual":
        return validate_proxy(settings.proxy)
    detected = auto_proxies()
    return detected["env"] or detected["system"]


def status(settings: Settings) -> dict:
    detected = auto_proxies()
    proxy = effective_proxy(settings)
    source = ("manual" if settings.proxy_mode == "manual" else
              "env" if detected["env"] else "system") if proxy else None
    return {"mode": settings.proxy_mode, "effective": proxy, "source": source, **detected,
            "local_candidates": local_candidates() if settings.proxy_mode == "auto" and proxy is None else []}


def test_connection(settings: Settings) -> dict:
    proxy = None
    started = time.monotonic()
    try:
        validate_proxy(settings.proxy)
        proxy = effective_proxy(settings)
        with httpx.Client(proxy=proxy, trust_env=False, timeout=5) as client:
            response = client.get("https://www.youtube.com/generate_204")
            response.raise_for_status()
        return {"ok": True, "proxy": proxy, "latency_ms": round((time.monotonic() - started) * 1000), "error": None}
    except ValueError:
        error = "代理地址格式不正确"
    except httpx.TimeoutException:
        error = "超时"
    except httpx.HTTPStatusError as exc:
        error = f"连接返回 HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        cause = exc
        refused = False
        while cause is not None:
            refused |= isinstance(cause, ConnectionRefusedError) or getattr(cause, "errno", None) == errno.ECONNREFUSED or "Connection refused" in str(cause)
            cause = cause.__cause__
        error = "连接被拒绝" if refused else "无法连接，请检查代理或网络"
    return {"ok": False, "proxy": proxy, "latency_ms": None, "error": error}
