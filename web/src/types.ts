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
  kind: "search" | "download";
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
  search_concurrency: number;
  download_concurrency: number;
  auto_approve: boolean;
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
