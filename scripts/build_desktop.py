"""Build on the target OS; downloads and intermediates stay in .build-cache."""
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".build-cache"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
MAC_VERSION = "9.0.2"
MAC_SOURCE = f"https://ffmpeg.org/releases/ffmpeg-{MAC_VERSION}.tar.xz"
MAC_SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
LAME_SOURCE = "https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz"
LAME_SHA256 = "ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e"
LAME_FLAGS = ["--disable-shared", "--enable-static", "--disable-frontend", "--disable-decoder",
              "--disable-dependency-tracking", "CFLAGS=-Os -mmacosx-version-min=12.0"]
MAC_FLAGS = [
    "--disable-everything", "--disable-autodetect", "--disable-network", "--disable-doc",
    "--disable-debug", "--disable-ffplay", "--disable-avdevice", "--disable-swscale",
    "--enable-small", "--enable-static", "--disable-shared", "--enable-stripping",
    "--cc=clang", "--arch=arm64", "--target-os=darwin",
    "--extra-cflags=-mmacosx-version-min=12.0 -I../include",
    "--extra-ldflags=-mmacosx-version-min=12.0 -L../lame-3.100/libmp3lame/.libs",
    "--enable-libmp3lame",
    "--enable-decoder=mp3float,aac,alac,flac,opus,vorbis,pcm_*",
    "--enable-encoder=pcm_s16le,libmp3lame",
    "--enable-demuxer=mp3,mov,flac,ogg,matroska,wav,aac,pcm_s16le",
    "--enable-muxer=ipod,mp4,opus,ogg,null,mp3",
    "--enable-parser=aac,flac,mpegaudio,opus,vorbis",
    "--enable-bsf=aac_adtstoasc", "--enable-protocol=file,pipe",
    "--enable-filter=aresample,anull",
]
WIN_SOURCE = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip"
WIN_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"


def run(*args, cwd=ROOT):
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def download(url, path):
    if path.exists():
        return
    temporary = path.with_suffix(path.suffix + ".part")
    run("curl.exe" if sys.platform == "win32" else "curl", "--fail", "--location", "--retry", "3", "--max-time", "600", "--silent", "--show-error", url, "-o", temporary)
    temporary.replace(path)


def source_archive(url, digest):
    path = CACHE / url.rsplit("/", 1)[1]
    download(url, path)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise RuntimeError(f"SHA256 mismatch: {path.name}; remove the cached download and retry")
    return path


def build_mac_tools(directory):
    recipe = {"version": MAC_VERSION, "sha256": MAC_SHA256, "configure": MAC_FLAGS,
              "lame_sha256": LAME_SHA256, "lame_configure": LAME_FLAGS}
    key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]
    build = CACHE / f"ffmpeg-{MAC_VERSION}-arm64-{key}"
    ready = build / "bin"
    if not all((ready / tool).is_file() for tool in ("ffmpeg", "ffprobe")):
        build.mkdir(exist_ok=True)
        for url, digest in ((MAC_SOURCE, MAC_SHA256), (LAME_SOURCE, LAME_SHA256)):
            with tarfile.open(source_archive(url, digest)) as bundle:
                bundle.extractall(build, filter="data")
        lame = build / "lame-3.100"
        run("./configure", *LAME_FLAGS, cwd=lame)
        run("make", "-j10", cwd=lame)
        headers = build / "include" / "lame"
        headers.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lame / "include" / "lame.h", headers / "lame.h")
        source = build / f"ffmpeg-{MAC_VERSION}"
        run("./configure", *MAC_FLAGS, cwd=source)
        run("make", "-j10", "ffmpeg", "ffprobe", cwd=source)
        ready.mkdir(exist_ok=True)
        for tool in ("ffmpeg", "ffprobe"):
            run("strip", "-S", source / tool)
            run("codesign", "--force", "-s", "-", source / tool)
            run("codesign", "--verify", "--verbose", source / tool)
        for tool in ("ffmpeg", "ffprobe"):
            shutil.copy2(source / tool, ready / tool)
    else:
        print(f"Using cached minimal FFmpeg: {build.name}", flush=True)
    for tool in ("ffmpeg", "ffprobe"):
        shutil.copy2(ready / tool, directory / tool)
        run("codesign", "--verify", "--verbose", directory / tool)
    return [{"url": MAC_SOURCE, "sha256": MAC_SHA256, "cache_key": key,
             "configure": shlex.join(["./configure", *MAC_FLAGS])},
            {"url": LAME_SOURCE, "sha256": LAME_SHA256,
             "configure": shlex.join(["./configure", *LAME_FLAGS])}]


def prepare_tools():
    directory = CACHE / "bin"
    directory.mkdir(exist_ok=True)
    records = []
    if sys.platform == "darwin":
        if platform.machine() != "arm64":
            raise RuntimeError("macOS build requires Apple Silicon")
        records = build_mac_tools(directory)
    elif sys.platform == "win32":
        path = source_archive(WIN_SOURCE, WIN_SHA256)
        with zipfile.ZipFile(path) as bundle:
            for tool in ("ffmpeg.exe", "ffprobe.exe"):
                entry = next(item for item in bundle.namelist() if Path(item).name == tool)
                (directory / tool).write_bytes(bundle.read(entry))
        records.append({"url": WIN_SOURCE, "sha256": WIN_SHA256})
    else:
        raise RuntimeError("Packaging supports macOS arm64 and Windows x64")
    versions = []
    for tool in ("ffmpeg", "ffprobe"):
        executable = directory / (tool + (".exe" if sys.platform == "win32" else ""))
        result = subprocess.run([str(executable), "-version"], check=True, capture_output=True, text=True)
        versions.append(result.stdout)
        if sys.platform == "darwin":
            linked = subprocess.check_output(["otool", "-L", str(executable)], text=True)
            if any(not line.strip().startswith(("/usr/lib/", "/System/Library/")) for line in linked.splitlines()[1:]):
                raise RuntimeError("FFmpeg has non-system dynamic dependencies")
    notice = (ROOT / "packaging" / "THIRD_PARTY.md").read_text(encoding="utf-8")
    notice += "\n## Build provenance\n\n```json\n" + json.dumps(records, indent=2) + "\n```\n\n```text\n" + "\n".join(versions) + "\n```\n"
    (CACHE / "THIRD_PARTY.md").write_text(notice, encoding="utf-8")


def prepare_icons():
    from PIL import Image
    from resvg_py import svg_to_bytes
    svg = ROOT / "packaging" / "icon.svg"
    if not svg.exists():
        raise RuntimeError("packaging/icon.svg is required")
    png = CACHE / "icon.png"
    png.write_bytes(svg_to_bytes(svg_string=svg.read_text(encoding="utf-8"), width=1024, height=1024))
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
        run("hdiutil", "create", "-volname", "觅音", "-srcfolder", stage, "-ov", "-format", "ULMO", artifact)
    else:
        artifact = Path(shutil.make_archive(str(ROOT / "dist" / f"SongSeek-{VERSION}-windows-x64"), "zip", ROOT / "dist", "SongSeek"))
    print(json.dumps({"artifact": str(artifact), "bytes": artifact.stat().st_size, "build_seconds": round(time.monotonic() - started, 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
