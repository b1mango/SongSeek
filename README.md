# 觅音 · SongSeek

Python 3.12 本地歌曲检索、审核、下载工具。后端和命令行共用 service 层，数据保存在 `$SONGSEEK_HOME`（默认 `~/.songseek`）。桌面安装包内置 `ffmpeg`、`ffprobe` 和 `yt-dlp`；源码运行需要系统提供 `ffmpeg`、`ffprobe`。

```sh
uv sync
uv run songseek serve
```

服务只监听 `127.0.0.1:8765`。前端尚未构建时，首页显示构建提示。API 见 `docs/API.md`。

```sh
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

清单缺少歌手或时长时必须人工审核。文本 `歌名 - 歌手` 为默认方向；`歌手 - 歌名` 无法可靠自动判断，请用表格列映射或导入预览后编辑。`search-one` 使用「歌名 歌手」；有空格的英文歌名建议使用「歌名 - 歌手」。

设置通过 `/api/settings` 更新；代理模式为「自动 / 手动 / 不用」，默认自动检测环境变量与系统代理，约 15 秒刷新。手动模式使用 `proxy` 地址；不用模式强制直连。国内音源始终直连，只有 YouTube 与 LRCLIB 回退使用代理。PAC 自动配置不受支持，检测不到代理时仅提示常用本地端口，不自动使用。Cookie 只能由设置提交到本地权限 0600 的 `netease_cookie`，API 仅返回是否已设置，不返回内容。

```sh
SONGSEEK_MOCK=1 SONGSEEK_HOME=/tmp/songseek-mock uv run songseek serve --no-open
uv run pytest -q
```

mock 候选包含正常、时长待审、歌手待审、现场版及伴奏版；含「不存在」的歌名无结果，含「失败」的候选取流失败。mock 音频由 ffmpeg 生成并缓存。只测试时请把输出目录设为 `/tmp` 下的目录，避免写入个人曲库。

导入预览支持 `mapping` 覆盖 `title/artist/album/duration` 的列名。Markdown 多表格默认选择含歌名列且列数最多的一张；可在 mapping 中加入 `"table": 0` 指定从 0 开始的表格序号。CSV/XLSX 无法识别歌名列时仍返回原始 columns，修正映射后重新解析。纯文本分隔方向无法自动确定，会返回中文警告。

匹配分数分为 match 90–99、review 50–89、reject 0–49；同为 match 时依次按无损/码率、热度、音源 order 排序。热度不可得时保留 null。缺少歌手或时长不会自动选择，因此不带 `--duration` 的单曲搜索可能是 needs_review；审核选定后可下载。失败回退仅使用其他 match 候选。目标文件同 SHA256 时复用，内容不同时添加序号。

网易云使用公开 API，无 Cookie 时仅尝试免费完整音频；返回会员限制或试听地址会明确拒绝。Cookie 仅发送给网易云 API，不跟随重定向、不发送给 CDN。YouTube 使用内置 Python yt-dlp，禁用用户配置并显式传递代理（不用模式强制直连）。B站多分集视频不自动猜测分集，当前仅支持可确认的单视频音轨。视频音轨下载后以 `-c:a copy` 换容器，再做时长和完整解码校验。

后台任务共享搜索和下载并发上限，每个音源 HTTP/yt-dlp 操作最多两个并发。取消保留已完成曲目，运行中的曲目允许结束；重启会恢复中断曲目并将旧 running 任务标为 error。SSE 每 15 秒心跳，同一任务事件最多每 250ms 一次。试听视频音轨缓存最多 500MB，按最近访问淘汰。

后端仅供本机单用户使用；不将服务绑定到公网。请求异常使用中文错误结构，不返回上游请求头或堆栈。上传清单上限 20MB，XLSX 解压上限 100MB；CSV 导出处理公式前缀。测试使用合成 Cookie 和 MockTransport，不读取个人凭据、不联网。

## 安装与运行

macOS Apple Silicon：打开 DMG，将「觅音」拖到 Applications。应用尚未公证，首次启动请在 Finder 中右键「觅音」→「打开」。Windows：解压 ZIP 后运行 `SongSeek.exe`，系统需要 Microsoft Edge WebView2 Runtime。应用只监听本机回环地址，不提供局域网模式；退出窗口会等待当前文件安全写入并停止服务。

桌面数据默认位于 macOS `~/Library/Application Support/SongSeek`、Windows `%APPDATA%\SongSeek`、Linux `~/.local/share/songseek`。`SONGSEEK_HOME` 优先；新目录不存在且旧 `~/.songseek` 存在时沿用旧目录。日志保存在数据目录的 `logs/app.log`，最多约 3 MiB。命令行 `serve` 的默认数据目录仍为 `~/.songseek`。

开发运行与构建（Python 3.12、uv、Node.js 22；macOS 构建需 Apple Silicon 和 Xcode 命令行工具）：

```sh
uv sync --extra desktop --group build
cd web && npm ci && npm run build && cd ..
uv run --extra desktop songseek app
./scripts/build_macos.sh
# Windows: uv run --extra desktop --group build python scripts/build_desktop.py
```

macOS 构建从官方 FFmpeg 9.0.2 和 LAME 3.100 源码编译 LGPL 最小音频静态版，源码 SHA256 固定在脚本中，最低支持 macOS 12；禁用网络、视频和设备，保留解码校验、音频换容器及 mock MP3 生成。首次使用 `make -j10` 编译，后续按版本和配置命中 `.build-cache/` 缓存；不链接或打包 Homebrew FFmpeg。Windows 继续下载并校验 Gyan essentials GPL 版，来源不变。

构建从 `packaging/icon.svg` 生成图标，产物写入 `dist/`。Pillow 仅用于构建图标，不随应用分发；歌单表格导入不需要图片处理。macOS 包做 ad-hoc 签名，生成 ULMO（LZMA，macOS 10.15+ 可打开）的 `觅音-<版本>-mac-arm64.dmg`；Windows 生成便携 ZIP。CI 在 tag push 或手动触发时分别构建，不自动发布。第三方工具来源、许可证、构建哈希和 configure 命令随应用附带在 `THIRD_PARTY.md`。

无窗口验收：`dist/觅音.app/Contents/MacOS/SongSeek --selftest`。临时运行后端：`SONGSEEK_HOME=/tmp/songseek-desktop dist/觅音.app/Contents/MacOS/SongSeek --headless --port 8765`。
