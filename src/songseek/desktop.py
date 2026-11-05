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

    def titlebar_double_click(self):
        """Double-clicking the page's title strip does what the system setting says a title bar does."""
        if sys.platform != "darwin" or self.window is None:
            return
        import AppKit
        from PyObjCTools import AppHelper
        action = AppKit.NSUserDefaults.standardUserDefaults().stringForKey_("AppleActionOnDoubleClick") or "Maximize"
        native = self.window.native
        if action == "Minimize":
            AppHelper.callAfter(native.miniaturize_, None)
        elif action != "None":
            AppHelper.callAfter(native.zoom_, None)

    def apply_theme(self, theme: str):
        """Keep the native window (traffic lights, under-page colour, dialogs) in step with the page's theme."""
        if sys.platform != "darwin" or self.window is None or self.window.native is None:
            return
        from PyObjCTools import AppHelper
        AppHelper.callAfter(set_native_theme, self.window.native, theme)


THEMES = ("system", "light", "dark")
PAPER = {"light": "#f5f1e8", "dark": "#14120f"}


def stored_theme(home: Path) -> str:
    """The theme saved in settings.json, read without loading the backend (it is needed before the window exists)."""
    try:
        theme = json.loads((home / "settings.json").read_text("utf-8")).get("settings", {}).get("theme", "system")
    except (OSError, ValueError, AttributeError):
        return "system"
    return theme if theme in THEMES else "system"


def resolved_theme(theme: str) -> str:
    return theme if theme != "system" else "dark" if system_dark() else "light"


def set_native_theme(native, theme: str):
    import AppKit
    names = {"light": AppKit.NSAppearanceNameAqua, "dark": AppKit.NSAppearanceNameDarkAqua}
    native.setAppearance_(AppKit.NSAppearance.appearanceNamed_(names[theme]) if theme in names else None)
    colour = PAPER[resolved_theme(theme)]
    r, g, b = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    native.setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))


def unify_titlebar(window):
    """macOS: run the page under a transparent title bar so the window reads as one surface."""
    if sys.platform != "darwin":
        return
    import AppKit
    native = window.native
    # pywebview paints its title bar with the window colour; clear that so the page shows through.
    bar = native.contentView().superview().subviews().lastObject()
    if bar is not None and bar.respondsToSelector_("setBackgroundColor:"):
        bar.setBackgroundColor_(AppKit.NSColor.clearColor())
    native.setStyleMask_(native.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
    native.setTitlebarAppearsTransparent_(True)
    native.setTitleVisibility_(AppKit.NSWindowTitleHidden)


def keep_focus(window):
    """macOS: make sure a freshly launched window can actually be clicked.

    When the launch-time activation times out ("AppleEvent activation suspension timed out" in the
    system log) the window is drawn but the app never becomes active, and the page swallows clicks.
    main() avoids the cause by entering the run loop before any slow work; this is the safety net:
    for the first few seconds, until the app has been active once, keep asking for activation and
    make the window key with the web view as first responder. Once the user has had the app in front,
    leave focus alone, so switching away right after launch is respected.
    """
    if sys.platform != "darwin" or window.native is None:
        return
    import AppKit
    import WebKit
    from PyObjCTools import AppHelper
    app = AppKit.NSApplication.sharedApplication()
    native = window.native
    log = logging.getLogger("songseek.desktop")

    def check(attempt: int):
        if app.isActive():
            if not native.isKeyWindow():
                native.makeKeyAndOrderFront_(None)
            web = native.contentView()  # pywebview swaps the WKWebView in once the first page has loaded
            if web is not None and web.isKindOfClass_(WebKit.WKWebView) and native.firstResponder() is not web:
                native.makeFirstResponder_(web)
            if attempt:
                log.info("launch focus recovered after %d checks", attempt)
            return
        if attempt == 0 or attempt % 3 == 0:
            log.info("launch focus: app not active yet (check %d), re-activating", attempt)
        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
        if app.respondsToSelector_("activate"):
            app.activate()
        app.activateIgnoringOtherApps_(True)
        native.makeKeyAndOrderFront_(None)
        if attempt < 24:  # ~6 s
            AppHelper.callLater(0.25, check, attempt + 1)

    AppHelper.callAfter(check, 0)


def boot_page(theme: str) -> str:
    """What the window shows for the moment the local service takes to start: the page's own paper colour."""
    paper = PAPER[resolved_theme(theme)]
    ink = "#8b8273" if paper == PAPER["dark"] else "#8a8174"
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>html,body{{margin:0;height:100%;background:{paper};'
            f'color:{ink};font:13px -apple-system,"PingFang SC",sans-serif;-webkit-user-select:none;cursor:default}}'
            'body{display:grid;place-items:center}p{opacity:0;animation:in .3s ease 1.2s forwards}'
            '@keyframes in{to{opacity:1}}</style></head><body class="pywebview-drag-region"><p id="m">正在启动…</p></body></html>')


def quit_on_close(server: dict):
    """Close button: take the window off screen and exit in the same tick, before AppKit tears the page down."""
    def closing(window):
        if sys.platform == "darwin" and window.native is not None:
            window.native.orderOut_(None)
        if server.get("server") is not None:
            server["server"].quit_now()
        logging.shutdown()
        os._exit(0)
    return closing


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
        import uvicorn
        from .config import Config
        from .server import create_app
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", port))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.app = create_app(Config(home), port=self.port, desktop=bridge)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_config=None,
                                                   access_log=False, timeout_graceful_shutdown=2))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, name="songseek-server")

    def start(self):
        import httpx
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

    def quit_now(self):
        """Window closed: leave at once instead of waiting for open streams and running jobs.

        Waiting (even ~1s) keeps the menu bar and Dock icon alive with a blocked main thread, which reads as a
        stutter. A hard exit is safe: SQLite commits are atomic, interrupted tracks/jobs are reset on next launch
        (Service._recover) and files are published atomically, so nothing half-written reaches the music folder.
        """
        service = getattr(self.app.state, "service", None)
        if service is not None:
            for event in list(service.jobs.cancellations.values()):
                event.set()
        if sys.platform != "win32":
            # ffmpeg / yt-dlp children would otherwise keep running after we are gone.
            subprocess.run(["/usr/bin/pkill", "-TERM", "-P", str(os.getpid())], capture_output=True, timeout=2)
        logging.shutdown()
        os._exit(0)


def selftest() -> int:
    import httpx
    from .tools import require_tool, yt_dlp_command
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
        if not args.headless:
            run_window(home, bridge, args.port)
            return
        server = LocalServer(home, bridge, args.port)
        server.start()
        try:
            print(server.url, flush=True)
            stopping = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopping.set())
            signal.signal(signal.SIGINT, lambda *_: stopping.set())
            while server.thread.is_alive() and not stopping.wait(.2):
                pass
        finally:
            server.stop()
    finally:
        lock.close()


def run_window(home: Path, bridge: DesktopBridge, port: int):
    """Window first, backend second.

    macOS hands a launching app its activation only if the main thread reaches the AppKit run loop in
    time. Importing the backend and waiting for the local service used to happen before that, and on a
    cold first launch (fresh copy, Gatekeeper check, cold disk cache) it took long enough for the
    activation to time out: the window appeared but could not be clicked. So the window opens at once
    on a placeholder in the page's colour, and the service starts on a worker thread, then loads in.
    """
    import webview
    log = logging.getLogger("songseek.desktop")
    began = time.monotonic()
    theme = stored_theme(home)
    held: dict = {}
    # Only the page's own drag strips move the window, not the buttons inside them.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    window = bridge.window = webview.create_window("觅音", html=boot_page(theme), width=1280, height=840, min_size=(980, 640),
                                                   background_color=PAPER[resolved_theme(theme)])
    window.events.before_show += unify_titlebar
    if sys.platform == "darwin":
        window.events.before_show += lambda window: set_native_theme(window.native, theme)
    window.events.closing += quit_on_close(held)
    window.events.shown += keep_focus
    window.events.shown += lambda: log.info("window shown %.2fs after start", time.monotonic() - began)

    def boot():
        try:
            server = LocalServer(home, bridge, port)
            server.start()
        except Exception as error:
            log.exception("local service failed to start")
            window.evaluate_js(f"document.getElementById('m').textContent = {json.dumps('本地服务启动失败：' + str(error))}")
            return
        held["server"] = server
        log.info("local service ready %.2fs after start", time.monotonic() - began)
        window.load_url(server.url)

    webview.start(boot, gui="cocoa" if sys.platform == "darwin" else "edgechromium" if sys.platform == "win32" else None)
    if "server" in held:
        held["server"].quit_now()

if __name__ == "__main__":
    main()
