# 觅音 SongSeek 本地 API 契约

后端与前端共同遵守本文件。改契约要先改这里。

- 服务只监听 `127.0.0.1`，默认端口 `8765`，前缀 `/api`。
- 前端构建产物 `web/dist` 由同一服务以静态文件提供：`/` 返回 `index.html`，非 `/api` 路径一律回退到 `index.html`。
- 请求体和响应体都是 JSON（UTF-8），文件上传除外。
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
}

interface Track {
  id: number;
  playlist_id: number;
  position: number;       // 清单内序号，从 1 开始
  title: string;
  artist: string;         // 可为空字符串，表示清单没给歌手
  album: string;
  duration: number | null;
  flags: VersionFlag[];   // 从清单歌名解析出的版本标记
  status: TrackStatus;
  chosen_candidate_id: number | null;
  chosen_by: "auto" | "user" | null;
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
  proxy: string;                 // 例如 "http://127.0.0.1:7897"，空字符串表示不用代理
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
| PATCH | `/api/tracks/{id}` | `{title?, artist?, album?, duration?, note?}`，返回 `Track` |
| DELETE | `/api/tracks/{id}` | 从清单中删除该曲目 |
| GET | `/api/tracks/{id}/candidates` | `Candidate[]`，按 rank 排序，包含 reject |
| POST | `/api/tracks/{id}/choose` | `{candidate_id}`，选定候选，status 改为 matched、chosen_by 改为 user，返回 `Track` |
| POST | `/api/tracks/{id}/search` | `{query?, providers?: string[]}`，同步重新搜索；新候选追加并重新排序，返回 `{track, candidates}`。超时上限 30s |
| POST | `/api/tracks/batch` | `{ids: number[], action}`，返回 `{updated: Track[]}`。action 取值：`accept_best`（选定每首的最佳候选，reject 不选）、`skip`、`unskip`（恢复到搜索前或搜索后的合理状态）、`retry`（失败的重新排队） |
| GET | `/api/tracks/{id}/audio` | 播放已下载的文件，支持 Range |
| POST | `/api/tracks/{id}/reveal` | 在系统文件管理器中显示该文件，返回 `{ok: true}` |

### 搜索与下载
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/search` | 单曲搜索：`{query, duration?}`。在名为「单曲搜索」的清单（source 为 `search`，不存在则自动创建）里新建一首曲目，同步搜索后返回 `{track, candidates}` |
| POST | `/api/playlists/{id}/search` | `{track_ids?}`，省略时搜索所有 pending 和 not_found 的曲目。返回 `Job` |
| POST | `/api/playlists/{id}/download` | `{track_ids?}`，省略时下载所有 matched 的曲目，只下载 matched。返回 `Job` |
| GET | `/api/jobs` | 进行中及最近 20 个 `Job[]` |
| POST | `/api/jobs/{id}/cancel` | 返回 `Job` |
| GET | `/api/candidates/{id}/preview` | 代理该候选的音频流，用于试听，支持 Range。只接受数据库里已有的候选 ID，不能当作通用代理 |

### 实时事件（SSE）
`GET /api/events` 返回 `text/event-stream`，每 15 秒发一次注释行 `: ping`。事件类型如下：

- `event: track`，data 为 `Track`：任何曲目状态或字段变化时发出。
- `event: job`，data 为 `Job`：进度变化时发出，同一任务每秒最多 4 次。
- `event: playlist`，data 为 `Playlist`：counts 变化时发出。

### 音源与设置
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/providers` | `Provider[]`，按 order 排序 |
| PUT | `/api/providers` | `[{id, enabled, order}]`，批量更新，返回 `Provider[]` |
| POST | `/api/providers/health` | 同步检查全部启用的音源，每个最多 10s，返回 `Provider[]` |
| GET | `/api/settings` | `Settings` |
| PUT | `/api/settings` | 部分更新 `Settings`。额外字段 `netease_cookie?: string`，传空字符串表示清除。返回 `Settings` |
