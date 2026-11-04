import type {
  ArtistInfo,
  ArtistSong,
  BatchAction,
  Candidate,
  ColumnKey,
  Job,
  ParsedRow,
  ParseResult,
  Playlist,
  PlaylistSource,
  Provider,
  ProxyMode,
  ProxyStatus,
  ProxyTest,
  SystemInfo,
  Settings,
  Track,
} from "./types";

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

async function failure(res: Response): Promise<ApiError> {
  let code = `http_${res.status}`;
  let message = `请求失败（${res.status}）`;
  try {
    const body = await res.json();
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  } catch {
    /* non-JSON error body: keep the generic message */
  }
  return new ApiError(code, message);
}

/** Read a preview/audio URL's error message (the <audio> element can't see response bodies). */
export async function audioError(url: string): Promise<string | null> {
  try {
    const res = await fetch(url, { headers: { Range: "bytes=0-1" } });
    if (res.ok) {
      res.body?.cancel().catch(() => {});
      return null;
    }
    return (await failure(res)).message;
  } catch {
    return null;
  }
}

export type SearchEvent =
  | { type: "start"; track_id: number; providers: { id: string; label: string }[] }
  | { type: "progress"; provider: string; label: string; ok: boolean; error: string | null; candidates: Candidate[] }
  | { type: "done"; track: Track; candidates: Candidate[] }
  | ({ type: "artist" } & ArtistInfo)
  | { type: "source"; provider: string; label: string; ok: boolean; error: string | null; skipped?: boolean }
  | { type: "artist_songs"; songs: ArtistSong[] }
  | { type: "artist_done"; songs: ArtistSong[] }
  | { type: "error"; message: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError("offline", "连不上本地服务，请确认 songseek serve 正在运行");
  }
  if (!res.ok) throw await failure(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export type ParseInput =
  | { kind: "file"; file: File }
  | { kind: "text"; text: string }
  | { kind: "url"; url: string };

export const api = {
  parse(input: ParseInput, mapping?: Record<ColumnKey, string | null>) {
    if (input.kind === "file") {
      const form = new FormData();
      form.append("file", input.file);
      if (mapping) form.append("mapping", JSON.stringify(mapping));
      return request<ParseResult>("/import/parse", { method: "POST", body: form });
    }
    const body = input.kind === "text" ? { text: input.text } : { url: input.url };
    return request<ParseResult>("/import/parse", json("POST", mapping ? { ...body, mapping } : body));
  },
  createPlaylist: (name: string, source: PlaylistSource, rows: ParsedRow[]) =>
    request<Playlist>("/playlists", json("POST", { name, source, rows })),
  playlists: () => request<Playlist[]>("/playlists"),
  playlist: (id: number) => request<{ playlist: Playlist; tracks: Track[] }>(`/playlists/${id}`),
  renamePlaylist: (id: number, name: string) => request<Playlist>(`/playlists/${id}`, json("PATCH", { name })),
  deletePlaylist: (id: number) => request<void>(`/playlists/${id}`, { method: "DELETE" }),
  exportUrl: (id: number) => `/api/playlists/${id}/export.csv`,

  updateTrack: (id: number, patch: Partial<Pick<Track, "title" | "artist" | "album" | "duration" | "note" | "lyrics_wanted">>) =>
    request<Track>(`/tracks/${id}`, json("PATCH", patch)),
  deleteTrack: (id: number) => request<void>(`/tracks/${id}`, { method: "DELETE" }),
  candidates: (id: number) => request<Candidate[]>(`/tracks/${id}/candidates`),
  choose: (id: number, candidateId: number) => request<Track>(`/tracks/${id}/choose`, json("POST", { candidate_id: candidateId })),
  researchTrack: (id: number, query?: string, providers?: string[]) =>
    request<{ track: Track; candidates: Candidate[] }>(`/tracks/${id}/search`, json("POST", { query, providers })),
  batch: (ids: number[], action: BatchAction) => request<{ updated: Track[] }>("/tracks/batch", json("POST", { ids, action })),
  audioUrl: (id: number) => `/api/tracks/${id}/audio`,
  trackLyricsUrl: (id: number) => `/api/tracks/${id}/lyrics`,
  candidateLyricsUrl: (id: number) => `/api/candidates/${id}/lyrics`,
  trackCoverUrl: (id: number) => `/api/tracks/${id}/cover`,
  candidateCoverUrl: (id: number) => `/api/candidates/${id}/cover`,
  reveal: (id: number) => request<{ ok: true }>(`/tracks/${id}/reveal`, json("POST")),

  /** Streams per-provider results as NDJSON. Throws ApiError("http_404") on servers without streaming. */
  async searchStream(query: string, duration: number | undefined, providers: string[] | undefined, onEvent: (event: SearchEvent) => void, signal?: AbortSignal) {
    let res: Response;
    try {
      res = await fetch("/api/search/stream", { ...json("POST", { query, duration, providers }), signal });
    } catch (error) {
      if ((error as Error).name === "AbortError") throw error;
      throw new ApiError("offline", "连不上本地服务，请确认 songseek serve 正在运行");
    }
    if (res.status === 404 || res.status === 405) throw new ApiError("no_stream", "服务端不支持流式搜索");
    if (!res.ok || !res.body) throw await failure(res);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const flush = (final: boolean) => {
      let index: number;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) onEvent(JSON.parse(line) as SearchEvent);
      }
      if (final && buffer.trim()) onEvent(JSON.parse(buffer) as SearchEvent);
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      flush(false);
    }
    flush(true);
  },
  searchOne: (query: string, duration?: number, providers?: string[]) =>
    request<
      | { mode?: "song"; track: Track; candidates: Candidate[] }
      | { mode: "artist"; artist: ArtistInfo; songs: ArtistSong[] }
    >("/search", json("POST", { query, duration, providers })),
  downloadArtistSong: (token: string, lyrics?: boolean, providers?: string[]) =>
    request<{ track: Track; job: Job | null }>(`/artist-songs/${encodeURIComponent(token)}/download`, json("POST", { lyrics, providers })),
  searchPlaylist: (id: number, trackIds?: number[]) => request<Job>(`/playlists/${id}/search`, json("POST", { track_ids: trackIds })),
  downloadPlaylist: (id: number, trackIds?: number[], lyrics?: boolean) =>
    request<Job>(`/playlists/${id}/download`, json("POST", { track_ids: trackIds, lyrics })),
  playlistLyrics: (id: number) => request<Job>(`/playlists/${id}/lyrics`, json("POST")),
  trackLyrics: (id: number, force = false) => request<Track>(`/tracks/${id}/lyrics`, json("POST", { force })),
  jobs: () => request<Job[]>("/jobs"),
  cancelJob: (id: number) => request<Job>(`/jobs/${id}/cancel`, json("POST")),

  providers: () => request<Provider[]>("/providers"),
  saveProviders: (items: { id: string; enabled: boolean; order: number }[]) => request<Provider[]>("/providers", json("PUT", items)),
  checkProviders: () => request<Provider[]>("/providers/health", json("POST")),
  settings: () => request<Settings>("/settings"),
  saveSettings: (patch: Partial<Omit<Settings, "netease_cookie_set">> & { netease_cookie?: string }) =>
    request<Settings>("/settings", json("PUT", patch)),
  proxy: () => request<ProxyStatus>("/proxy"),
  testProxy: (mode?: ProxyMode, proxy?: string) => request<ProxyTest>("/proxy/test", json("POST", { mode, proxy })),
  system: () => request<SystemInfo>("/system"),
  pickFolder: (initial?: string) => request<{ path: string | null }>("/system/pick-folder", json("POST", { initial })),
  openUrl: (url: string) => request<{ ok: boolean }>("/system/open-url", json("POST", { url })),
};
