# SongSeek

<p align="center">
  <img src="packaging/icon.svg" alt="觅音 SongSeek" width="112" height="112" />
</p>

<p align="center">觅音：导入歌单，多音源检索、审核，一键下载成带标签和歌词的本地曲库</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.0-2ea043" alt="version 0.1.0" />
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-1f6feb" alt="macOS and Windows" />
  <a href="#license"><img src="https://img.shields.io/badge/license-pending-f5c542" alt="license pending" /></a>
</p>

## 功能

- 导入 XLSX / CSV / Markdown 表格 / 纯文本歌单，支持列映射与导入预览
- 网易云、QQ 音乐、酷狗、zpod、哔哩哔哩、YouTube 多音源并行检索
- 按歌名、歌手、时长、版本（现场 / 伴奏等）、音质与热度打分，自动选出最佳候选
- 低把握的歌曲进入审核页，可试听候选、手动选择或换关键词重搜
- 批量下载并校验时长与完整解码，写入标签和专辑封面，同内容文件自动去重
- 逐首开关 LRC 歌词，已下载的歌曲可一键补歌词
- 单曲搜索与歌手模式、全屏播放页、导出结果 CSV
- 代理自动检测，国内音源始终直连
- macOS / Windows 桌面应用，内置 ffmpeg、ffprobe 和 yt-dlp；同时提供命令行

## 安装

尚未发布 GitHub Release，可按下方「开发」自行构建。

- **macOS（Apple Silicon）**：打开 `觅音-<版本>-mac-arm64.dmg`，将「觅音」拖入 Applications。应用未公证，首次启动在 Finder 中右键「觅音」→「打开」。
- **Windows**：解压 ZIP 后运行 `SongSeek.exe`，需要 Microsoft Edge WebView2 Runtime。

数据默认位于 macOS `~/Library/Application Support/SongSeek`、Windows `%APPDATA%\SongSeek`、Linux `~/.local/share/songseek`，可用 `SONGSEEK_HOME` 覆盖；日志在数据目录 `logs/app.log`。服务只监听本机回环地址。

## 开发

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22；源码运行需系统提供 `ffmpeg`、`ffprobe`。macOS 构建需 Apple Silicon 和 Xcode 命令行工具。

```bash
uv sync --extra desktop --group build
cd web && npm ci && npm run build && cd ..
uv run --extra desktop songseek app      # 桌面窗口
uv run songseek serve                    # 仅后端，127.0.0.1:8765
uv run pytest -q                         # 测试（不联网、不读个人凭据）
./scripts/build_macos.sh                 # 构建 dist/觅音.app 与 DMG
# Windows: uv run --extra desktop --group build python scripts/build_desktop.py
```

离线演示：`SONGSEEK_MOCK=1 SONGSEEK_HOME=/tmp/songseek-mock uv run songseek serve --no-open`。打包产物自检：`dist/觅音.app/Contents/MacOS/SongSeek --selftest`。

<details>
<summary>命令行</summary>

```bash
uv run songseek import songs.xlsx --name 我的歌单
uv run songseek playlists
uv run songseek search 1
uv run songseek review 1
uv run songseek download 1
uv run songseek search-one "晴天 周杰伦" --duration 269 --json
uv run songseek providers --health
uv run songseek verify /path/to/song.mp3
uv run songseek export 1 -o results.csv
```

文本 `歌名 - 歌手` 为默认方向；`歌手 - 歌名` 无法可靠自动判断，请用表格列映射或在导入预览中编辑。缺少歌手或时长的歌曲不会自动选择，需要人工审核。

</details>

<details>
<summary>匹配、下载与网络细节</summary>

- **匹配分数**：match 90–99、review 50–89、reject 0–49；同为 match 时依次按无损 / 码率、热度、音源顺序排序。失败回退只使用其他 match 候选。
- **下载**：视频音轨以 `-c:a copy` 换容器，再做时长与完整解码校验；目标文件同 SHA256 时复用，内容不同时加序号。
- **任务**：搜索与下载共享并发上限，每个音源最多两个并发；取消时保留已完成曲目，重启后恢复中断的曲目。
- **代理**：「自动 / 手动 / 不用」三种模式，默认自动检测环境变量与系统代理；只有 YouTube 与 LRCLIB 回退走代理，不支持 PAC。
- **网易云**：无 Cookie 时只尝试免费完整音频，会员或试听地址会明确拒绝。Cookie 保存在本地 0600 权限文件，只发送给网易云 API，接口不回传内容。
- **安全**：仅供本机单用户使用；上传上限 20 MB，XLSX 解压上限 100 MB；CSV 导出处理公式前缀。

</details>

## 代码结构

后端 FastAPI + SQLite，前端 React + TypeScript + Vite，桌面壳 pywebview + PyInstaller。

| 目录 | 职责 |
| --- | --- |
| `src/songseek/service.py` / `jobs.py` | 歌单、搜索、下载流程与后台任务 |
| `src/songseek/providers` | 各音源检索与取流 |
| `src/songseek/matching.py` / `normalize.py` | 打分与歌名归一化 |
| `src/songseek/downloader.py` / `lyrics.py` / `covers.py` | 下载校验、标签、歌词与封面 |
| `src/songseek/importers` | 表格与文本歌单解析 |
| `src/songseek/server.py` / `events.py` | HTTP API 与 SSE 推送 |
| `src/songseek/desktop.py` | 桌面窗口、单实例与退出 |
| `web/src` | 前端页面、播放器与状态 |
| `packaging` / `scripts` | 图标、PyInstaller 配置与构建脚本 |

前后端接口约定见 [`docs/API.md`](docs/API.md)，第三方工具来源与许可证见 [`packaging/THIRD_PARTY.md`](packaging/THIRD_PARTY.md)。

## License

<a id="license"></a>

许可证待定。
