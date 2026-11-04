import sys
import tomllib
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
cache = root / '.build-cache'
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
mac = sys.platform == 'darwin'
datas = [(str(root / 'web' / 'dist'), 'web/dist'), (str(cache / 'THIRD_PARTY.md'), '.')]
datas += copy_metadata('songseek')
web_data, web_bins, web_hidden = collect_all('webview')
datas += web_data
hidden = web_hidden + collect_submodules('uvicorn') + collect_submodules('yt_dlp')
binaries = web_bins + [(str(cache / 'bin' / (name + ('' if mac else '.exe'))), 'bin') for name in ('ffmpeg', 'ffprobe')]
a = Analysis([str(root / 'packaging' / 'entry.py')], pathex=[str(root / 'src')], binaries=binaries,
             datas=datas, hiddenimports=hidden, excludes=['pytest', 'tkinter', 'cairosvg', 'resvg_py'])
pyz = PYZ(a.pure)
# Console bootloader preserves stdout for yt-dlp/selftest; Finder launches still have no terminal.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SongSeek', debug=False,
          strip=False, upx=False, console=True, hide_console='hide-early' if not mac else None,
          icon=str(cache / ('icon.icns' if mac else 'icon.ico')), target_arch='arm64' if mac else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SongSeek')
if mac:
    app = BUNDLE(coll, name='觅音.app', icon=str(cache / 'icon.icns'),
                 bundle_identifier='app.songseek.desktop', version=version,
                 info_plist={'CFBundleName': '觅音', 'CFBundleDisplayName': '觅音',
                             'CFBundleShortVersionString': version, 'CFBundleVersion': version,
                             'NSHighResolutionCapable': True, 'NSRequiresAquaSystemAppearance': False,
                             'LSMinimumSystemVersion': '12.0'})
