"""Build on the target OS; downloads and intermediates stay in .build-cache."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".build-cache"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
MAC_SOURCE = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
WIN_SOURCE = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def run(*args, cwd=ROOT):
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def download(url, path):
    if path.exists():
        return
    temporary = path.with_suffix(path.suffix + ".part")
    run("curl.exe" if sys.platform == "win32" else "curl", "--fail", "--location", "--retry", "3", "--max-time", "600", "--silent", "--show-error", url, "-o", temporary)
    temporary.replace(path)


def archive(url, filename):
    path = CACHE / filename
    checksum = CACHE / (filename + ".sha256")
    download(url + ".sha256", checksum)
    download(url, path)
    expected = re.search(r"\b[0-9a-fA-F]{64}\b", checksum.read_text()).group().lower()
    actual = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
    if actual != expected:
        raise RuntimeError(f"SHA256 mismatch: {filename}; remove the cached download and retry")
    return path, actual


def prepare_tools():
    directory = CACHE / "bin"
    directory.mkdir(exist_ok=True)
    records = []
    if sys.platform == "darwin":
        if platform.machine() != "arm64":
            raise RuntimeError("macOS build requires Apple Silicon")
        for tool in ("ffmpeg", "ffprobe"):
            url = f"{MAC_SOURCE}/{tool}.zip"
            path, digest = archive(url, f"mac-arm64-{tool}-9.0.2.zip")
            with zipfile.ZipFile(path) as bundle:
                entry = next(item for item in bundle.namelist() if Path(item).name == tool)
                (directory / tool).write_bytes(bundle.read(entry))
            (directory / tool).chmod(0o755)
            run("codesign", "--verify", "--verbose", directory / tool)
            records.append({"url": url, "sha256": digest})
    elif sys.platform == "win32":
        path, digest = archive(WIN_SOURCE, "windows-ffmpeg-essentials.zip")
        with zipfile.ZipFile(path) as bundle:
            for tool in ("ffmpeg.exe", "ffprobe.exe"):
                entry = next(item for item in bundle.namelist() if Path(item).name == tool)
                (directory / tool).write_bytes(bundle.read(entry))
        records.append({"url": WIN_SOURCE, "sha256": digest})
    else:
        raise RuntimeError("Packaging supports macOS arm64 and Windows x64")
    versions = []
    for tool in ("ffmpeg", "ffprobe"):
        executable = directory / (tool + (".exe" if sys.platform == "win32" else ""))
        result = subprocess.run([str(executable), "-version"], check=True, capture_output=True, text=True)
        versions.append(result.stdout)
        if sys.platform == "darwin":
            linked = subprocess.check_output(["otool", "-L", str(executable)], text=True)
            if any(line.strip().startswith(("/opt/", "/usr/local/", "@rpath", "@loader_path")) for line in linked.splitlines()[1:]):
                raise RuntimeError("FFmpeg has non-system dynamic dependencies")
    notice = (ROOT / "packaging" / "THIRD_PARTY.md").read_text()
    notice += "\n## Build provenance\n\n```json\n" + json.dumps(records, indent=2) + "\n```\n\n```text\n" + "\n".join(versions) + "\n```\n"
    (CACHE / "THIRD_PARTY.md").write_text(notice)


def prepare_icons():
    from PIL import Image
    from resvg_py import svg_to_bytes
    svg = ROOT / "packaging" / "icon.svg"
    if not svg.exists():
        raise RuntimeError("packaging/icon.svg is required")
    png = CACHE / "icon.png"
    png.write_bytes(svg_to_bytes(svg_string=svg.read_text(), width=1024, height=1024))
    with Image.open(png) as art:
        art.save(CACHE / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        if sys.platform == "darwin":
            iconset = CACHE / "icon.iconset"
            iconset.mkdir(exist_ok=True)
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    art.resize((size * scale, size * scale), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png")
            run("iconutil", "-c", "icns", iconset, "-o", CACHE / "icon.icns")


def main():
    started = time.monotonic()
    CACHE.mkdir(exist_ok=True)
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    if not (ROOT / "web" / "node_modules").is_dir():
        run(npm, "ci", cwd=ROOT / "web")
    run(npm, "run", "build", cwd=ROOT / "web")
    prepare_tools()
    prepare_icons()
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", CACHE / "pyinstaller", ROOT / "packaging" / "songseek.spec")
    if sys.platform == "darwin":
        app = ROOT / "dist" / "觅音.app"
        run("codesign", "--force", "--deep", "-s", "-", app)
        run("codesign", "--verify", "--deep", "--strict", app)
        stage = CACHE / "dmg"
        stage.mkdir(exist_ok=True)
        if (stage / "觅音.app").exists():
            shutil.rmtree(stage / "觅音.app")
        run("ditto", app, stage / "觅音.app")
        if not (stage / "Applications").is_symlink():
            (stage / "Applications").symlink_to("/Applications")
        artifact = ROOT / "dist" / f"觅音-{VERSION}-mac-arm64.dmg"
        run("hdiutil", "create", "-volname", "觅音", "-srcfolder", stage, "-ov", "-format", "UDZO", artifact)
    else:
        artifact = Path(shutil.make_archive(str(ROOT / "dist" / f"SongSeek-{VERSION}-windows-x64"), "zip", ROOT / "dist", "SongSeek"))
    print(json.dumps({"artifact": str(artifact), "bytes": artifact.stat().st_size, "build_seconds": round(time.monotonic() - started, 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
