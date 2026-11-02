import type { TrackStatus, VersionFlag } from "./types";

export function fmtDuration(s: number | null | undefined): string {
  if (s == null || !isFinite(s)) return "—";
  const total = Math.round(s);
  const m = Math.floor(total / 60);
  const sec = total % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function fmtDelta(d: number): string {
  const r = Math.round(d * 10) / 10;
  if (r === 0) return "±0s";
  return `${r > 0 ? "+" : "−"}${Math.abs(r)}s`;
}

export function fmtSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Same tolerance rule as the backend: ±3s, ±2s for tracks of 60s or less. */
export function tolerance(target: number): number {
  return target <= 60 ? 2 : 3;
}

export type Tone = "ok" | "warn" | "bad" | "info" | "idle" | "busy";

export const STATUS: Record<TrackStatus, { label: string; tone: Tone }> = {
  pending: { label: "未搜索", tone: "idle" },
  searching: { label: "搜索中", tone: "busy" },
  matched: { label: "待下载", tone: "info" },
  needs_review: { label: "待审核", tone: "warn" },
  not_found: { label: "未找到", tone: "bad" },
  queued: { label: "排队中", tone: "busy" },
  downloading: { label: "下载中", tone: "busy" },
  done: { label: "已完成", tone: "ok" },
  failed: { label: "下载失败", tone: "bad" },
  skipped: { label: "已跳过", tone: "idle" },
};

/** Order used for the status strip and filters: what needs attention first. */
export const STATUS_ORDER: TrackStatus[] = [
  "needs_review",
  "not_found",
  "failed",
  "matched",
  "done",
  "pending",
  "skipped",
];

export const FLAG_LABEL: Record<VersionFlag, string> = {
  live: "Live",
  dj: "DJ",
  remix: "Remix",
  instrumental: "伴奏",
  cover: "翻唱",
  speed: "变速",
  clip: "片段",
};

export const SOURCE_LABEL: Record<string, string> = {
  xlsx: "Excel",
  csv: "CSV",
  md: "Markdown",
  text: "文本",
  netease: "网易云歌单",
  search: "单曲搜索",
};
