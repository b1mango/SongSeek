import type {
  BatchAction,
  Candidate,
  ColumnKey,
  Job,
  ParsedRow,
  ParseResult,
  Playlist,
  PlaylistSource,
  Provider,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError("offline", "连不上本地服务，请确认 songseek serve 正在运行");
  }
  if (!res.ok) {
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
    throw new ApiError(code, message);
  }
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

  updateTrack: (id: number, patch: Partial<Pick<Track, "title" | "artist" | "album" | "duration" | "note">>) =>
    request<Track>(`/tracks/${id}`, json("PATCH", patch)),
  deleteTrack: (id: number) => request<void>(`/tracks/${id}`, { method: "DELETE" }),
  candidates: (id: number) => request<Candidate[]>(`/tracks/${id}/candidates`),
  choose: (id: number, candidateId: number) => request<Track>(`/tracks/${id}/choose`, json("POST", { candidate_id: candidateId })),
  researchTrack: (id: number, query?: string, providers?: string[]) =>
    request<{ track: Track; candidates: Candidate[] }>(`/tracks/${id}/search`, json("POST", { query, providers })),
  batch: (ids: number[], action: BatchAction) => request<{ updated: Track[] }>("/tracks/batch", json("POST", { ids, action })),
  audioUrl: (id: number) => `/api/tracks/${id}/audio`,
  reveal: (id: number) => request<{ ok: true }>(`/tracks/${id}/reveal`, json("POST")),

  searchOne: (query: string, duration?: number) =>
    request<{ track: Track; candidates: Candidate[] }>("/search", json("POST", { query, duration })),
  searchPlaylist: (id: number, trackIds?: number[]) => request<Job>(`/playlists/${id}/search`, json("POST", { track_ids: trackIds })),
  downloadPlaylist: (id: number, trackIds?: number[]) => request<Job>(`/playlists/${id}/download`, json("POST", { track_ids: trackIds })),
  jobs: () => request<Job[]>("/jobs"),
  cancelJob: (id: number) => request<Job>(`/jobs/${id}/cancel`, json("POST")),

  providers: () => request<Provider[]>("/providers"),
  saveProviders: (items: { id: string; enabled: boolean; order: number }[]) => request<Provider[]>("/providers", json("PUT", items)),
  checkProviders: () => request<Provider[]>("/providers/health", json("POST")),
  settings: () => request<Settings>("/settings"),
  saveSettings: (patch: Partial<Omit<Settings, "netease_cookie_set">> & { netease_cookie?: string }) =>
    request<Settings>("/settings", json("PUT", patch)),
};
