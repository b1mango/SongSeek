// Mirrors docs/API.md. Change the contract there first.

export type TrackStatus =
  | "pending"
  | "searching"
  | "matched"
  | "needs_review"
  | "not_found"
  | "queued"
  | "downloading"
  | "done"
  | "failed"
  | "skipped";

export type VersionFlag = "live" | "dj" | "remix" | "instrumental" | "cover" | "speed" | "clip";

export type PlaylistSource = "xlsx" | "csv" | "md" | "text" | "netease" | "search";

export interface Playlist {
  id: number;
  name: string;
  source: PlaylistSource;
  created_at: string;
  counts: Record<TrackStatus, number> & { total: number };
}

export interface DownloadedFile {
  path: string;
  size: number;
  format: string;
  bitrate: number | null;
  duration: number;
  sha256: string;
  provider: string | null;
  provider_label: string | null;
  lyrics?: LyricsInfo | null;
}

export interface LyricsInfo {
  path: string | null;
  source: string | null;
  source_label: string | null;
  synced: boolean;
  translated: boolean;
  embedded: boolean;
  error: string | null;
}

export interface Track {
  id: number;
  playlist_id: number;
  position: number;
  title: string;
  artist: string;
  album: string;
  duration: number | null;
  flags: VersionFlag[];
  status: TrackStatus;
  chosen_candidate_id: number | null;
  chosen_by: "auto" | "user" | null;
  /** Save lyrics with this track's download; null follows the global setting. */
  lyrics_wanted?: boolean | null;
  best_score: number | null;
  candidate_count: number;
  file: DownloadedFile | null;
  error: string | null;
  note: string;
  updated_at: string;
}

export interface CheckItem {
  key: "title" | "artist" | "duration" | "version" | "quality" | "popularity";
  ok: boolean | null;
  label: string;
}

export interface Candidate {
  id: number;
  track_id: number;
  provider: string;
  provider_label: string;
  source_id: string;
  title: string;
  artist: string;
  album: string;
  duration: number | null;
  quality: { format: string; bitrate: number | null; label: string };
  popularity: number | null;
  score: number;
  verdict: "match" | "review" | "reject";
  checks: CheckItem[];
  flags: VersionFlag[];
  preview_url: string;
  web_url: string | null;
  rank: number;
  needs_member?: boolean;
}

export interface Job {
  id: number;
  kind: "search" | "download" | "lyrics";
  playlist_id: number | null;
  status: "running" | "done" | "cancelled" | "error";
  total: number;
  completed: number;
  failed: number;
  current: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ProviderHealth {
  ok: boolean;
  latency_ms: number | null;
  message: string;
  checked_at: string;
}

export interface Provider {
  id: string;
  label: string;
  max_quality: string;
  needs: "cookie" | "proxy" | null;
  enabled: boolean;
  order: number;
  health: ProviderHealth | null;
}

export interface Settings {
  output_dir: string;
  filename_template: string;
  group_by_playlist: boolean;
  proxy: string;
  proxy_mode?: ProxyMode;
  search_concurrency: number;
  download_concurrency: number;
  auto_approve: boolean;
  lyrics: boolean;
  lyrics_embed: boolean;
  lyrics_translation: boolean;
  netease_cookie_set: boolean;
}

export interface ParsedRow {
  title: string;
  artist: string;
  album: string;
  duration: number | null;
}

export type ColumnKey = "title" | "artist" | "album" | "duration";

export interface ParseResult {
  name: string;
  source: PlaylistSource;
  columns: string[];
  mapping: Record<ColumnKey, string | null>;
  rows: ParsedRow[];
  warnings: string[];
}

export type BatchAction = "accept_best" | "skip" | "unskip" | "retry";

/** One song in an artist's popular list; the token addresses it until the server forgets it (~2h). */
export interface ArtistSong {
  token: string;
  title: string;
  artist: string;
  album: string;
  duration: number | null;
  popularity: number | null;
  flags: VersionFlag[];
  providers: { id: string; label: string }[];
  quality_label: string;
  preview_url: string;
  cover_url: string;
  lyrics_url: string;
}

export interface ArtistInfo {
  name: string;
  aliases: string[];
  cover_url?: string | null;
}

export type ProxyMode = "auto" | "manual" | "off";

export interface ProxyStatus {
  mode: ProxyMode;
  effective: string | null;
  source: "manual" | "env" | "system" | null;
  system: string | null;
  env: string | null;
  pac: boolean;
  local_candidates: string[];
}

export interface ProxyTest {
  ok: boolean;
  proxy: string | null;
  latency_ms: number | null;
  error: string | null;
}

export interface SystemInfo {
  desktop: boolean;
  platform: "darwin" | "win32" | "linux";
  version: string;
  data_dir: string;
  tools: { ffmpeg: boolean; ffprobe: boolean; yt_dlp: boolean };
}
