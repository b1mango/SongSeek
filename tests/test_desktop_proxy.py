import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from songseek import netproxy, tools
from songseek.config import Config
from songseek.desktop import InstanceLock, data_dir
from songseek.models import Settings
from songseek.server import create_app

# Saved before the global offline fixture patches sockets; only loopback is used below.
_connect = socket.socket.connect


@pytest.fixture
def detection(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(netproxy, "_cached", None)
    monkeypatch.setattr(netproxy, "system_proxies", lambda: ({"http": "http://127.0.0.1:7897"}, False))


def test_auto_env_system_cache(detection, monkeypatch):
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setattr(netproxy.urllib.request, "getproxies", lambda: pytest.fail("must not use merged getproxies"))
    assert netproxy.effective_proxy(Settings()) == "http://127.0.0.1:7897"
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    assert netproxy.effective_proxy(Settings()) == "http://127.0.0.1:7897"
    monkeypatch.setattr(netproxy, "_expires", 0)
    assert netproxy.status(Settings())["source"] == "env"
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8889")
    monkeypatch.setattr(netproxy, "_expires", 0)
    assert netproxy.effective_proxy(Settings()) == "http://127.0.0.1:8889"


def test_macos_sysconf_with_no_proxy(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(netproxy.urllib.request, "getproxies_macosx_sysconf", lambda: {"http": "http://localhost:7897"}, raising=False)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(stdout="ProxyAutoConfigEnable : 1"))
    assert netproxy.system_proxies() == ({"http": "http://localhost:7897"}, True)


def test_manual_off_socks_and_pac(detection, monkeypatch):
    assert netproxy.effective_proxy(Settings(proxy_mode="manual", proxy="")) is None
    assert netproxy.effective_proxy(Settings(proxy_mode="manual", proxy="http://localhost:7890")) == "http://localhost:7890"
    assert netproxy.effective_proxy(Settings(proxy_mode="off", proxy="http://localhost:7890")) is None
    monkeypatch.setattr(netproxy, "system_proxies", lambda: ({"socks": "localhost:1080"}, True))
    assert netproxy.status(Settings())["effective"] == "socks5://localhost:1080"
    assert netproxy.status(Settings())["pac"] is True


@pytest.mark.parametrize("proxy,mode", [("", "auto"), ("http://localhost:7890", "manual")])
def test_migration(tmp_path, proxy, mode):
    (tmp_path / "settings.json").write_text(json.dumps({"settings": {"proxy": proxy}}))
    config = Config(tmp_path)
    assert config.settings.proxy_mode == mode
    config.update({"proxy_mode": "off"})
    assert Config(tmp_path).settings.proxy_mode == "off"


def test_local_candidates_loopback(detection, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(netproxy, "PORTS", [port])
        def connect(address, timeout):
            assert address == ("127.0.0.1", port) and timeout == .08
            sock = socket.socket()
            sock.settimeout(timeout)
            _connect(sock, address)
            return sock
        monkeypatch.setattr(socket, "create_connection", connect)
        monkeypatch.setattr(netproxy, "system_proxies", lambda: ({}, False))
        result = netproxy.status(Settings())
        assert result["effective"] is None
        assert result["local_candidates"] == [f"http://127.0.0.1:{port}"]
        assert netproxy.status(Settings(proxy_mode="off"))["local_candidates"] == []


@pytest.mark.parametrize("failure,message", [(None, None), (httpx.ConnectError("Connection refused"), "连接被拒绝"), (httpx.ReadTimeout("secret"), "超时")])
def test_proxy_endpoint(config, detection, monkeypatch, failure, message):
    original = httpx.Client
    routes = []
    def handle(request):
        assert str(request.url) == "https://www.youtube.com/generate_204"
        if failure:
            raise failure
        return httpx.Response(204)
    with TestClient(create_app(config)) as client:
        def factory(**kwargs):
            routes.append(kwargs)
            return original(transport=httpx.MockTransport(handle))
        monkeypatch.setattr(netproxy.httpx, "Client", factory)
        result = client.post("/api/proxy/test", json={"mode": "manual", "proxy": "http://localhost:7890"}).json()
        assert result["ok"] == (failure is None)
        assert result["error"] == message
        assert routes[-1]["proxy"] == "http://localhost:7890" and routes[-1]["trust_env"] is False
        assert config.settings.proxy_mode == "auto" and config.settings.proxy == ""
        client.post("/api/proxy/test", json={"mode": "off"})
        assert routes[-1]["proxy"] is None
        assert client.post("/api/proxy/test", json={"proxy": "invalid"}).json()["error"] == "代理地址格式不正确"
        assert client.get("/api/proxy").json()["source"] == "system"


def test_system_and_hosts(config, monkeypatch):
    opened = []
    monkeypatch.setattr("songseek.server.webbrowser.open", opened.append)
    with TestClient(create_app(config, port=8765), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").json() == {"ok": True}
        system = client.get("/api/system").json()
        assert system["desktop"] is False and system["data_dir"] == str(config.home)
        assert client.post("/api/system/pick-folder", json={}).status_code == 409
        for host in ("evil.example:8765", "127.0.0.1:9999", "testserver", "localhost", "127.0.0.1:8765@evil.example"):
            assert client.get("/api/health", headers={"host": host}).status_code == 400
        assert client.get("/api/health", headers={"host": "localhost:8765"}).status_code == 200
        assert client.post("/api/system/open-url", json={"url": "https://example.com"}).json() == {"ok": True}
        assert opened == ["https://example.com"]
        assert client.post("/api/system/open-url", json={"url": "file:///tmp/foo"}).status_code == 400
        assert client.post("/api/system/open-url", json={"url": "https://example.com"}, headers={"origin": "http://localhost:9999"}).status_code == 403
    bridge = SimpleNamespace(window=object(), pick_folder=lambda initial: initial)
    with TestClient(create_app(config, desktop=bridge)) as client:
        assert client.get("/api/system").json()["desktop"] is True
        assert client.post("/api/system/pick-folder", json={"initial": "/tmp/music"}).json() == {"path": "/tmp/music"}
        assert client.post("/api/system/pick-folder").json() == {"path": None}
        assert client.put("/api/settings", json={"proxy_mode": "off"}).json()["proxy_mode"] == "off"


def test_tool_resolution_and_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "resource_dir", lambda: tmp_path)
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/path/" + name)
    assert tools.resolve_tool("ffmpeg") == "/path/ffmpeg"
    binary = tmp_path / "bin" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    binary.parent.mkdir()
    binary.write_text("binary")
    binary.chmod(0o755)
    assert tools.resolve_tool("ffmpeg") == str(binary)
    assert tools.yt_dlp_command() == [sys.executable, "-m", "yt_dlp"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert tools.yt_dlp_command() == [sys.executable, "--run-yt-dlp"]
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert tools.resolve_tool("ffprobe") is None


def test_data_dir_and_single_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("SONGSEEK_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_dir() == tmp_path / "Library/Application Support/SongSeek"
    (tmp_path / ".songseek").mkdir()
    assert data_dir() == tmp_path / ".songseek"
    monkeypatch.setenv("SONGSEEK_HOME", str(tmp_path / "override"))
    assert data_dir() == tmp_path / "override"
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.close()
    assert second.acquire()
    second.close()


def test_tool_homebrew_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(tools, "resource_dir", lambda: tmp_path)
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) == "/opt/homebrew/bin/ffmpeg")
    monkeypatch.setattr(tools.os, "access", lambda *args: True)
    assert tools.resolve_tool("ffmpeg") == "/opt/homebrew/bin/ffmpeg"


def test_windows_registry_proxy_and_home(tmp_path, monkeypatch):
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    registry = SimpleNamespace(HKEY_CURRENT_USER=0, OpenKey=lambda *args: Key(),
                               QueryValueEx=lambda *args: ("https://example.com/proxy.pac", 1))
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(netproxy.urllib.request, "getproxies_registry", lambda: {"https": "http://localhost:7890"}, raising=False)
    assert netproxy.system_proxies() == ({"https": "http://localhost:7890"}, True)
    monkeypatch.delenv("SONGSEEK_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert data_dir() == tmp_path / "SongSeek"


def test_shutdown_waits_for_current_download(service, monkeypatch):
    import threading
    from songseek.models import ParsedRow
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    partial = service.config.home / "test.part"
    target = service.config.home / "test.complete"
    playlist = service.create_playlist("shutdown", "text", [ParsedRow(title="晴天", artist="周杰伦", duration=180)])
    track = service.search_track(service.tracks(playlist.id)[0].id)["track"]
    def download(*args, **kwargs):
        partial.write_text("complete audio")
        entered.set()
        assert release.wait(3)
        partial.replace(target)
    monkeypatch.setattr(service, "download_track", download)
    job = service.jobs.start("download", playlist.id)
    assert entered.wait(3)
    def close():
        service.jobs.close()
        closed.set()
    thread = threading.Thread(target=close)
    thread.start()
    assert not closed.wait(.05)
    release.set()
    thread.join(3)
    assert closed.is_set() and target.read_text() == "complete audio" and not partial.exists()
    assert service.jobs.get(job.id).status in ("done", "cancelled")


def test_windows_reveal_argument_list(service, config, monkeypatch):
    import subprocess
    from songseek.models import DownloadedFile, ParsedRow
    playlist = service.create_playlist("reveal", "text", [ParsedRow(title="test")])
    track = service.tracks(playlist.id)[0]
    path = config.home / "中文 music file.mp3"
    path.write_bytes(b"audio")
    track.file = DownloadedFile(path=str(path), size=5, format="mp3", bitrate=None, duration=1, sha256="test", provider="mock", provider_label="mock")
    track.status = "done"
    service.save_track(track)
    calls = []
    monkeypatch.setattr("songseek.server.subprocess.Popen", lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(sys, "platform", "win32")
    with TestClient(create_app(config, service)) as client:
        assert client.post(f"/api/tracks/{track.id}/reveal").json() == {"ok": True}
    assert calls == [["explorer", "/select,", str(path)]]
    assert '"' in subprocess.list2cmdline(calls[0])
