# Third-party software

The application includes FFmpeg and FFprobe static binaries. macOS arm64 builds
come from [Martin Riedl's build server](https://ffmpeg.martin-riedl.de/), version
9.0.2, build 1789931890. Windows x64 builds come from
[Gyan's FFmpeg essentials release builds](https://www.gyan.dev/ffmpeg/builds/).
The build verifies each archive against the publisher's SHA256 and runs both tools.
The macOS binaries are also checked against their upstream code signatures before
being signed with the application. Exact URLs, hashes and configuration flags are
appended to the copy of this document shipped with every build.

These builds enable GPL components and are distributed under **GPL v3 or later**,
not LGPL-only. FFmpeg source: https://ffmpeg.org/download.html ; license:
https://www.gnu.org/licenses/gpl-3.0.html . Build recipes and linked codec/library
versions are provided by the corresponding build publisher. Retain the matching
source and license notices when redistributing these binaries.

SongSeek runs these tools as separate command-line processes.

Other included components include Python (PSF), pywebview (BSD-3-Clause),
yt-dlp (Unlicense; bundled dependencies retain their licenses), FastAPI (MIT),
uvicorn (BSD-3-Clause), HTTPX (BSD-3-Clause), Mutagen (GPL-2.0-or-later),
OpenCC Python reimplementation (Apache-2.0), and the React UI (MIT).
PyInstaller's bootloader is GPL with its distribution exception.
