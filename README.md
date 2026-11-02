# 觅音 · SongSeek

Python 3.12 本地歌曲检索、审核、下载工具。后端和命令行共用 service 层，数据保存在 `$SONGSEEK_HOME`（默认 `~/.songseek`）。需要系统提供 `ffmpeg`、`ffprobe`、`yt-dlp`。

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

设置通过 `/api/settings` 更新；代理仅使用其中的 `proxy`，空字符串直连，不读取环境代理。Cookie 只能由设置提交到本地权限 0600 的 `netease_cookie`，API 仅返回是否已设置，不返回内容。

```sh
SONGSEEK_MOCK=1 SONGSEEK_HOME=/tmp/songseek-mock uv run songseek serve --no-open
uv run pytest -q
```

mock 候选包含正常、时长待审、歌手待审、现场版及伴奏版；含「不存在」的歌名无结果，含「失败」的候选取流失败。mock 音频由 ffmpeg 生成并缓存。只测试时请把输出目录设为 `/tmp` 下的目录，避免写入个人曲库。

导入预览支持 `mapping` 覆盖 `title/artist/album/duration` 的列名。Markdown 多表格默认选择含歌名列且列数最多的一张；可在 mapping 中加入 `"table": 0` 指定从 0 开始的表格序号。CSV/XLSX 无法识别歌名列时仍返回原始 columns，修正映射后重新解析。纯文本分隔方向无法自动确定，会返回中文警告。

匹配分数分为 match 90–99、review 50–89、reject 0–49；同为 match 时依次按无损/码率、热度、音源 order 排序。热度不可得时保留 null。缺少歌手或时长不会自动选择，因此不带 `--duration` 的单曲搜索可能是 needs_review；审核选定后可下载。失败回退仅使用其他 match 候选。目标文件同 SHA256 时复用，内容不同时添加序号。

网易云使用公开 API，无 Cookie 时仅尝试免费完整音频；返回会员限制或试听地址会明确拒绝。Cookie 仅发送给网易云 API，不跟随重定向、不发送给 CDN。YouTube 调用系统 yt-dlp，禁用其用户配置并显式传递代理。B站多分集视频不自动猜测分集，当前仅支持可确认的单视频音轨。视频音轨下载后以 `-c:a copy` 换容器，再做时长和完整解码校验。

后台任务共享搜索和下载并发上限，每个音源 HTTP/yt-dlp 操作最多两个并发。取消保留已完成曲目，运行中的曲目允许结束；重启会恢复中断曲目并将旧 running 任务标为 error。SSE 每 15 秒心跳，同一任务事件最多每 250ms 一次。试听视频音轨缓存最多 500MB，按最近访问淘汰。

后端仅供本机单用户使用；不将服务绑定到公网。请求异常使用中文错误结构，不返回上游请求头或堆栈。上传清单上限 20MB，XLSX 解压上限 100MB；CSV 导出处理公式前缀。测试使用合成 Cookie 和 MockTransport，不读取个人凭据、不联网。
