import argparse
import json
import logging
import os
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import uvicorn

from .config import Config
from .tools import require_tool, yt_dlp_command


def data_dir() -> Path:
    if override := os.environ.get("SONGSEEK_HOME"):
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        target = home / "Library" / "Application Support" / "SongSeek"
    elif sys.platform == "win32":
        target = Path(os.environ["APPDATA"]) / "SongSeek"
    else:
        target = home / ".local" / "share" / "songseek"
    legacy = home / ".songseek"
    return legacy if legacy.is_dir() and not target.exists() else target


class InstanceLock:
    def __init__(self, home: Path):
        self.path = home / "app.lock"
        self.stream = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        # Keep the inode: deleting a lock file creates a race between launchers.


class DesktopBridge:
    def __init__(self):
        self.window = None

    def pick_folder(self, initial: str | None) -> str | None:
        import webview
        result = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=initial or "")
        return result[0] if result else None


def system_dark() -> bool:
    """Whether the OS is in dark mode, so the window opens in the page's own colour (no flash)."""
    try:
        if sys.platform == "darwin":
            result = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=2)
            return result.stdout.strip().lower() == "dark"
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def configure_logging(home: Path):
    directory = home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "app.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    for name in ("songseek", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def already_running():
    if sys.platform == "darwin":
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("觅音已经在运行")
        alert.runModal()
    elif sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "觅音已经在运行", "觅音", 0x40)
    else:
        import tkinter.messagebox
        tkinter.messagebox.showinfo("觅音", "觅音已经在运行")


class LocalServer:
    def __init__(self, home: Path, bridge: DesktopBridge | None = None, port: int = 0):
        from .server import create_app
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", port))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        app = create_app(Config(home), port=self.port, desktop=bridge)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None,
                                                   access_log=False, timeout_graceful_shutdown=2))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, name="songseek-server")

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        with httpx.Client(trust_env=False, timeout=.5) as client:
            while time.monotonic() < deadline and self.thread.is_alive():
                try:
                    if client.get(self.url + "/api/health").is_success:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
        self.stop()
        raise RuntimeError("本地服务启动失败")

    def stop(self):
        self.server.should_exit = True
        self.thread.join()
        self.socket.close()


def selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="songseek-selftest-") as directory:
        server = LocalServer(Path(directory), DesktopBridge())
        server.start()
        try:
            with httpx.Client(trust_env=False, timeout=5) as client:
                health = client.get(server.url + "/api/health").json()
                system = client.get(server.url + "/api/system").json()
                static = client.get(server.url + "/").text
            checks = {}
            for name in ("ffmpeg", "ffprobe", "yt_dlp"):
                command = yt_dlp_command() + ["--version"] if name == "yt_dlp" else [require_tool(name), "-version"]
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                checks[name] = {"ok": result.returncode == 0, "version": result.stdout.splitlines()[0] if result.stdout else ""}
            ok = health["ok"] and all(item["ok"] for item in checks.values()) and "<html" in static.lower()
            print(json.dumps({"ok": ok, "health": health, "system": system, "tools": checks,
                              "frontend": "<html" in static.lower()}, ensure_ascii=False))
            return 0 if ok else 1
        finally:
            server.stop()


def main():
    if sys.argv[1:2] == ["--run-yt-dlp"]:
        import yt_dlp
        yt_dlp.main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(prog="songseek-desktop")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间")
    if args.selftest:
        raise SystemExit(selftest())
    home = data_dir()
    lock = InstanceLock(home)
    if not lock.acquire():
        if args.headless:
            print("觅音已经在运行", file=sys.stderr)
        else:
            already_running()
        raise SystemExit(1)
    try:
        configure_logging(home)
        bridge = DesktopBridge()
        server = LocalServer(home, bridge, args.port)
        server.start()
        try:
            if args.headless:
                print(server.url, flush=True)
                stopping = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stopping.set())
                signal.signal(signal.SIGINT, lambda *_: stopping.set())
                while server.thread.is_alive() and not stopping.wait(.2):
                    pass
            else:
                import webview
                bridge.window = webview.create_window("觅音", server.url, width=1280, height=840,
                                                      min_size=(980, 640), background_color="#14120f" if system_dark() else "#f5f1e8")
                webview.start(gui="cocoa" if sys.platform == "darwin" else "edgechromium" if sys.platform == "win32" else None)
        finally:
            server.stop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
