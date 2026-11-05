# 觅音 SongSeek 本地 API 契约

后端与前端共同遵守本文件。改契约要先改这里。

- 服务只监听 `127.0.0.1`，默认端口 `8765`，前缀 `/api`。
- 前端构建产物 `web/dist` 由同一服务以静态文件提供：`/` 返回 `index.html`，非 `/api` 路径一律回退到 `index.html`。
- 请求体和响应体默认使用 JSON（UTF-8）；文件上传、音频、封面、导出和 SSE 使用各自的内容类型。
- 时间用 ISO 8601 字符串，时长单位是秒（float），码率单位是 kbps（int）。
- 出错时返回 `{"error": {"code": "snake_case", "message": "给用户看的中文说明"}}`，状态码使用 4xx/5xx，不返回堆栈。

## 数据类型

```ts
type TrackStatus =
  | "pending"       // 已导入，未搜索
  | "searching"
  | "matched"       // 已选定候选（自动或手动），等待下载
  | "needs_review"  // 有候选，但需人工确认
  | "not_found"     // 没有可用候选
  | "queued"        // 已加入下载队列
  | "downloading"
  | "done"
  | "failed"
  | "skipped";      // 用户跳过

type VersionFlag = "live" | "dj" | "remix" | "instrumental" | "cover" | "speed" | "clip";
// live 现场；dj DJ 版；remix 混音；instrumental 伴奏、纯音乐、Off Vocal；
// cover 翻唱；speed 加速、降调、0.8x；clip 片段、试听

interface Playlist {
  id: number;
  name: string;
  source: "xlsx" | "csv" | "md" | "text" | "netease" | "search";
  created_at: string;
  counts: Record<TrackStatus, number> & { total: number };
}

interface DownloadedFile {
  path: string;           // 绝对路径
  size: number;           // 字节数
  format: string;         // mp3 / m4a / flac / opus ...
  bitrate: number | null;
  duration: number;       // ffprobe 实测
  sha256: string;
  provider: string | null;        // 实际下载所用音源（可能是回退后的音源）
  provider_label: string | null;
}

interface Track {
  id: number;
  playlist_id: number;
  position: number;       // 清单内序号，从 1 开始
  title: string;
  artist: string;         // 可为空字符串，表示清单没给歌手
  album: string;
  duration: number | null;
  lyrics_wanted: boolean | null; // null 跟随 settings.lyrics，true/false 覆盖全局
  flags: VersionFlag[];   // 从清单歌名解析出的版本标记
  status: TrackStatus;
  chosen_candidate_id: number | null;
  chosen_by: "auto" | "user" | null;
  chosen: {                      // 选定候选的快照，随 chosen_candidate_id 自动刷新；未选定时为 null
    id: number; provider_label: string; title: string; artist: string; album: string;
    duration: number | null; quality: string; score: number;
  } | null;
  best_score: number | null;     // 最佳候选得分，0~100
  candidate_count: number;
  file: DownloadedFile | null;
  error: string | null;          // 最近一次失败原因（中文）
  note: string;
  updated_at: string;
}

interface CheckItem {
  key: "title" | "artist" | "duration" | "version" | "quality" | "popularity";
  ok: boolean | null;     // true 通过；false 不符；null 无法判断（例如清单没给时长）
  label: string;          // 简短中文，例如「时长差 +1.2s」「歌手一致」「多了 Live 标记」
}

interface Candidate {
  id: number;
  track_id: number;
  provider: string;        // provider id，例如 "netease"
  provider_label: string;  // 例如「网易云」
  source_id: string;       // 平台内的歌曲 ID 或 hash
  title: string;
  artist: string;
  album: string;
  duration: number | null;
  quality: { format: string; bitrate: number | null; label: string }; // 例如 {mp3, 320, "320k"}
  popularity: number | null; // 0~1，平台内热度归一化后的值
  score: number;             // 0~100
  verdict: "match" | "review" | "reject";
  checks: CheckItem[];
  flags: VersionFlag[];
  preview_url: string;       // 固定为 /api/candidates/{id}/preview
  web_url: string | null;    // 平台网页链接，可为空
  rank: number;              // 1 为最佳
}

interface Job {
  id: number;
  kind: "search" | "download";
  playlist_id: number | null;
  status: "running" | "done" | "cancelled" | "error";
  total: number;
  completed: number;
  failed: number;
  current: string | null;    // 当前处理的「歌名 - 歌手」
  started_at: string;
  finished_at: string | null;
}

interface Provider {
  id: string;
  label: string;
  max_quality: string;           // 例如 "320k"、"128k"、"无损"
  needs: "cookie" | "proxy" | null;
  enabled: boolean;
  order: number;                 // 下载优先级，越小越优先
  health: { ok: boolean; latency_ms: number | null; message: string; checked_at: string } | null;
}

interface Settings {
  output_dir: string;
  filename_template: string;     // 默认 "{artist} - {title}"，扩展名由程序补
  group_by_playlist: boolean;    // 为 true 时按清单名建子目录
  proxy: string;                 // 仅 YouTube 使用，例如 "http://127.0.0.1:7897"；其他音源搜索、取流、下载均直连，空字符串表示不用代理
  search_concurrency: number;
  download_concurrency: number;
  auto_approve: boolean;         // 为 true 时，最佳候选判定为 match 就自动选定
  netease_cookie_set: boolean;   // 只读，Cookie 本身不会经 API 返回
}
```

## 接口

### 导入
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/import/parse` | 解析但不保存。三种输入二选一：multipart 字段 `file`（xlsx/csv/md/txt），或 JSON `{text}`，或 JSON `{url}`（网易云歌单链接）。可选 `mapping`（multipart 中为 JSON 字符串），用来覆盖自动识别的列。返回 `ParseResult` |
| POST | `/api/playlists` | `{name, source, rows: ParsedRow[]}`，创建清单，返回 `Playlist` |

```ts
interface ParsedRow { title: string; artist: string; album: string; duration: number | null }
interface ParseResult {
  name: string;               // 建议的清单名
  source: Playlist["source"];
  columns: string[];          // 表格类输入的原始列名，文本和链接输入为空数组
  mapping: { title: string | null; artist: string | null; album: string | null; duration: string | null };
  rows: ParsedRow[];
  warnings: string[];         // 例如「第 12 行缺少歌名，已忽略」
}
```

### 清单与曲目
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/playlists` | `Playlist[]`，按创建时间倒序 |
| GET | `/api/playlists/{id}` | `{playlist: Playlist, tracks: Track[]}` |
| PATCH | `/api/playlists/{id}` | `{name}` |
| DELETE | `/api/playlists/{id}` | 删除清单记录，不删除已下载的文件 |
| GET | `/api/playlists/{id}/export.csv` | 导出结果 CSV，列为：序号、歌名、歌手、状态、来源、音质、时长、文件路径、备注 |
| PATCH | `/api/tracks/{id}` | `{title?, artist?, album?, duration?, note?, lyrics_wanted?}`，返回 `Track` |
| DELETE | `/api/tracks/{id}` | 从清单中删除该曲目及其候选，不删除已下载的文件；搜索或下载中返回 409 `track_busy` |
| GET | `/api/tracks/{id}/candidates` | `Candidate[]`，按 rank 排序，包含 reject |
| POST | `/api/tracks/{id}/choose` | `{candidate_id}`，选定候选，status 改为 matched、chosen_by 改为 user，返回 `Track` |
| POST | `/api/tracks/{id}/search` | `{query?, providers?: string[]}`，同步重新搜索；新候选追加并重新排序，返回 `{track, candidates}`。超时上限 30s |
| POST | `/api/tracks/batch` | `{ids: number[], action}`，返回 `{updated: Track[]}`。action 取值：`accept_best`（选定每首的最佳候选，reject 不选）、`skip`、`unskip`（恢复到搜索前或搜索后的合理状态）、`retry`（失败的重新排队） |
| GET | `/api/tracks/{id}/audio` | 播放已下载的文件，支持 Range |
| POST | `/api/tracks/{id}/reveal` | 在系统文件管理器中显示该文件，返回 `{ok: true}` |

### 搜索与下载
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/search` | `{query, duration?, providers?: string[]}`，自动识别歌曲或歌手；歌曲返回 `{mode:"song", track, candidates}`，歌手返回 `{mode:"artist", artist:{name,aliases}, songs:ArtistSong[]}` |
| POST | `/api/playlists/{id}/search` | `{track_ids?}`，省略时搜索所有 pending 和 not_found 的曲目。返回 `Job` |
| POST | `/api/playlists/{id}/download` | `{track_ids?, lyrics?: boolean\|null}`，省略 track_ids 时下载所有 matched 曲目。传 lyrics 时，在入队前为选中曲目设置 lyrics_wanted；省略保留原选择，null 恢复跟随设置。返回 `Job` |
| GET | `/api/jobs` | 进行中及最近 20 个 `Job[]` |
| POST | `/api/jobs/{id}/cancel` | 返回 `Job` |
| GET | `/api/candidates/{id}/preview` | 代理该候选的音频流，用于试听，支持 Range。只接受数据库里已有的候选 ID，不能当作通用代理 |

### 歌曲／歌手搜索及 NDJSON

`POST /api/search/stream` 接受与 `/api/search` 相同的请求体，返回 `application/x-ndjson`。`providers` 缺省或空数组表示全部启用来源；非空数组与已启用来源取交集，未知 ID 被忽略，交集为空时不请求任何来源，不修改全局设置。

查询拆分仅参考网易云、酷狗、QQ（VKeys）的曲名和歌手，支持“歌名 歌手”和“歌手 歌名”以及两种带 ` - ` 的顺序，多来源一致优先。同票按来源顺序；无曲库证据的显式分隔仍按“歌名 - 歌手”解释。歌手识别同时运行精确名称／别名查找与首轮歌曲查询；可信结果存在同名歌曲时优先歌曲模式。

歌曲模式才在「单曲搜索」（source=`search`）中新建 Track。歌手模式不创建 Track 或清单。事件顺序：

```ts
{type:"start", track_id:number|null, providers:[{id,label}]}
// 判定类型前发送；尚未创建曲目时 track_id 为 null。使用 done.track.id 获取最终曲目 ID。
// 歌曲模式：
{type:"progress", provider, label, ok, error, candidates:Candidate[]}
{type:"done", track:Track, candidates:Candidate[]}
// 歌手模式：
{type:"artist", name:string, aliases:string[], cover_url:string|null}
{type:"source", provider, label, ok:boolean, error:string|null, skipped?:boolean}  // skipped: 歌手模式下用不到该音源（不是失败）
{type:"artist_songs", songs:ArtistSong[]}
{type:"artist_done", songs:ArtistSong[]}
// 异常：
{type:"error", message:string}
```

`source` 可以在 `artist` 前出现，每个来源最终都有状态。`artist_songs` 是完整列表，可随来源完成多次发出；`artist_done` 为终止事件，不再发送 `done`。热门列表使用网易云和酷狗歌手热门接口，QQ（VKeys）未接入歌手歌曲接口；若艺人查找暂不可用而可信歌曲结果达成歌手共识，使用可信结果中的歌曲。过滤现场、DJ、混音、伴奏、翻唱、变速、片段及现场专辑，按规范化歌名、歌手和时长容差合并，以来源热门顺序和热度排序，最多 50 首。

```ts
interface ArtistSong {
  token: string;
  title: string;
  artist: string;
  album: string;
  duration: number | null;
  popularity: number | null; // 0..1
  flags: VersionFlag[];     // 已过滤版本，当前为 []
  providers: {id: string; label: string}[];
  quality_label: string;
  preview_url: string;      // /api/artist-songs/<token>/preview
  cover_url: string;        // /api/artist-songs/<token>/cover
  lyrics_url: string;       // /api/artist-songs/<token>/lyrics
}
```

Token 不透明，候选保存在进程内 LRU（4096 条，2 小时 TTL），重启或淘汰后失效。四个端点均对未知／过期 token 返回 HTTP 404 与 `error.code="not_found"`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/artist-songs/{token}/preview` | 支持 Range；缓存来源均失败后，以“歌名 歌手”搜索尚未尝试的已启用音源，可信曲库优先、组内按配置顺序，共享 5 秒额外搜索／取流预算。只尝试歌名、歌手、时长容差一致且无额外版本标记的录音，首个成功即返回；回退时返回 `X-Preview-Fallback: 1` 和 URL 编码的来源标签 `X-Preview-Source`。全部失败返回 502 和合并错误 |
| GET | `/api/artist-songs/{token}/cover` | 复用封面获取与缓存，尝试其他来源；没有可用封面返回 cover_not_found |
| GET | `/api/artist-songs/{token}/lyrics` | 返回 LyricsResponse，复用同版本来源、网易云、酷狗、LRCLIB 查找链 |
| POST | `/api/artist-songs/{token}/download` | `{lyrics?: boolean\|null, providers?: string[]}`，按“歌名 - 歌手”（只用所给音源）和已知时长重新执行完整单曲搜索；自动匹配时启动 download Job，否则交给人工审核。返回 `{track:Track, job:Job\|null}` |

国内来源的歌曲、歌手、音频和封面请求均直连，不读取系统代理。

跨源试听成功的候选追加到原 token 的歌曲组，后续试听、封面和歌词请求直接复用；下载时也带入已启用且符合所选音源范围的缓存候选，同时保留完整单曲搜索和自动匹配流程。

### 封面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/candidates/{id}/cover` | 返回候选封面图片。顺序：自身封面 → 同曲目、同录音版本的非 reject 候选 → 同曲目其他非 reject 候选；获取失败时继续回退 |
| GET | `/api/tracks/{id}/cover` | 优先读取下载文件的内嵌封面（MP4 `covr`、ID3 `APIC`、FLAC pictures），没有或文件不可读时按选定候选走上述回退链 |

成功返回图片字节，`Content-Type` 为 `image/jpeg`、`image/png` 或 `image/webp`，并带 `Cache-Control: public, max-age=86400`。远程封面使用对应音源的直连/代理与 Referer 策略；每次获取超时约 5 秒，图片上限 5 MiB，内存 LRU 缓存最多 64 张。无可用封面返回 HTTP 404：`{"error":{"code":"cover_not_found","message":"没有找到可用封面"}}`；曲目或候选 ID 不存在使用原有 `not_found` 错误。接口只接受已有 ID。

### 歌词
`LyricsResponse = {lrc: string|null, synced: bool, source: string|null, source_label: string|null, translation: string|null}`；`DownloadedFile.lyrics = {path, source, source_label, synced, translated, embedded, error} | null`。
来源顺序：选定候选 → 同版本其他候选 → 网易云新搜索 → 酷狗歌词库关键词 → LRCLIB，同步歌词优先，总预算 8 秒；命中缓存 10 分钟，未命中 60 秒。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/candidates/{id}/lyrics` | 试听用歌词，返回 `LyricsResponse` |
| GET | `/api/tracks/{id}/lyrics` | 已下载曲目：读取旁边的 .lrc，没有时按选定候选查找 |
| POST | `/api/tracks/{id}/lyrics` | `{force?}`，显式为已下载曲目保存歌词，即使全局 lyrics 或该曲目 lyrics_wanted 为 false 也执行；已有 .lrc 默认跳过，返回 `Track` |
| POST | `/api/playlists/{id}/lyrics` | `{force?}`，批量补歌词（默认跳过已有 .lrc 的），返回 `Job`（kind `lyrics`） |

下载和清单批量补歌词遵守 Track.lyrics_wanted：true 强制获取，false 跳过，null 跟随全局。修改此字段不清空候选、不重置状态。CSV 列不变。

设置项：`lyrics`（默认开，下载时保存 .lrc）、`lyrics_translation`（附译文，同时间戳第二行）、`lyrics_embed`（写入音频标签）。

外观：`theme` 为 `system`（默认，跟随系统）/ `light` / `dark`。服务端返回 `index.html` 时写入 `<html data-theme-pref>`，页面首帧前据此确定主题；桌面端修改后同步窗口外观。

### 实时事件（SSE）
`GET /api/events` 返回 `text/event-stream`，每 15 秒发一次注释行 `: ping`。事件类型如下：

- `event: track`，data 为 `Track`：任何曲目状态或字段变化时发出。
- `event: job`，data 为 `Job`：进度变化时发出，同一任务每秒最多 4 次。
- `event: playlist`，data 为 `Playlist`：counts 变化时发出。
- `event: track_progress`，data 为 `{track_id, playlist_id, phase: "download" | "finishing", downloaded, total: number | null, fraction: number | null}`：单曲下载中按字节发出，同一首歌至少间隔 0.2 秒，传输完成（`fraction: 1`）必发；传输结束后、转封装 / 校验 / 写标签前发一次 `finishing`。总大小未知时 `total`、`fraction` 为 null。试听缓存不发。下载结束仍以 `track` 事件收尾。

### 音源与设置
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/providers` | `Provider[]`，按 order 排序 |
| PUT | `/api/providers` | `[{id, enabled, order}]`，批量更新，返回 `Provider[]` |
| POST | `/api/providers/health` | 同步检查全部启用的音源，每个最多 10s，返回 `Provider[]` |
| GET | `/api/settings` | `Settings` |
| PUT | `/api/settings` | 部分更新 `Settings`。额外字段 `netease_cookie?: string`，传空字符串表示清除。返回 `Settings` |

### 桌面、健康检查与代理

服务始终绑定 `127.0.0.1`。Host 必须为 `127.0.0.1:<实际端口>` 或 `localhost:<实际端口>`；写请求的 Origin（若有）也必须为同端口本机 HTTP 地址。

`Settings` 增加 `proxy_mode: "auto" | "manual" | "off"`，默认 `auto`，GET/PUT `/api/settings` 均支持。`proxy` 保留为手动地址。旧设置缺失模式时，非空 proxy 迁移为 manual，否则 auto。国内音源继续直连。

| 方法 | 路径 | 请求及响应 |
|---|---|---|
| GET | `/api/health` | `{ok:true}` |
| GET | `/api/proxy` | `{mode, effective:string|null, source:"manual"|"env"|"system"|null, system:string|null, env:string|null, pac:boolean, local_candidates:string[]}` |
| POST | `/api/proxy/test` | `{mode?:"auto"|"manual"|"off", proxy?:string}`，可省略请求体；省略字段使用当前设置，不保存试填值。返回 `{ok:boolean, proxy:string|null, latency_ms:number|null, error:string|null}`；连接错误以 HTTP 200 + ok:false 返回，字段类型或模式错误为 422 |
| GET | `/api/system` | `{desktop:boolean, platform:"darwin"|"win32"|"linux", version:string, data_dir:string, tools:{ffmpeg:boolean,ffprobe:boolean,yt_dlp:boolean}}` |
| POST | `/api/system/pick-folder` | `{initial?:string}`，返回 `{path:string|null}`，取消返回 null；非桌面或无窗口模式返回 409，error.code=`not_desktop` |
| POST | `/api/system/open-url` | `{url:string}`，仅接受 http/https，打开系统浏览器并返回 `{ok:true}`；非法地址为 400 |

自动代理优先级：`HTTPS_PROXY`、`https_proxy`、`ALL_PROXY`、`all_proxy`、`HTTP_PROXY`、`http_proxy`，再查询系统代理（HTTPS → HTTP → SOCKS）。检测缓存 15 秒，手动和不用模式立即生效。`pac:true` 表示发现 PAC/自动发现配置，应用不执行 PAC。无自动代理时探测常用回环端口并返回建议，仅开放 TCP 端口不代表已验证为代理，用户需手动选择并测试。代理地址不接受用户名或密码。

连接测试通过所解析的代理（null 表示强制直连）向 `https://www.youtube.com/generate_204` 发送 HTTPS GET，超时 5 秒。错误仅提供中文短句。LRCLIB 保留先直连、失败后使用有效代理的行为。

`POST /api/tracks/{id}/reveal` 通过系统参数列表调用 Finder / Explorer / xdg-open；Windows 文件路径作为独立参数传给 Explorer `/select,`，支持空格与中文。
