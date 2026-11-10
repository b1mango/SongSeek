import { useSyncExternalStore } from "react";
import { api, ApiError, type SearchEvent } from "./api";
import { onEvent } from "./store";
import type { ArtistInfo, ArtistSong, Candidate, Track } from "./types";

/*
 * Single-search state lives outside the page so it survives navigation: a search keeps
 * streaming while you look at another page, and the results are still there when you
 * come back. A copy goes to sessionStorage so a reload restores the last search too.
 */

export interface SourceState {
  id: string;
  label: string;
  state: "pending" | "ok" | "error" | "skipped"; // skipped: not needed for this search (artist mode)
  error?: string | null;
}

export interface SearchState {
  mode: "song" | "artist";
  query: string;
  dur: string;
  loading: boolean;
  session: number;
  track: Track | null;
  cands: Candidate[];
  sources: SourceState[];
  startedAt: number; // performance.now() when the search started
  elapsed: number | null; // seconds, once finished
  showRejected: boolean;
  error: string | null; // last stream error, shown once by the page
  artist: ArtistInfo | null; // artist mode: who the query named
  songs: ArtistSong[]; // artist mode: their popular songs
  lrc: Record<string, boolean>; // per-row LRC choice (candidate id / song token) before downloading
  picked: Record<string, Track>; // artist mode: song token -> the track its download created
  catalog: { id: string; label: string }[]; // enabled providers, for the source switches
  off: string[]; // providers switched off for single search (remembered across sessions)
}

const KEY = "songseek.search";
const OFF_KEY = "songseek.searchSources.off";
const empty: SearchState = {
  mode: "song",
  query: "",
  dur: "",
  loading: false,
  session: 0,
  track: null,
  cands: [],
  sources: [],
  startedAt: 0,
  elapsed: null,
  showRejected: false,
  error: null,
  artist: null,
  songs: [],
  lrc: {},
  picked: {},
  catalog: [],
  off: [],
};

function restoreOff(): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(OFF_KEY) ?? "[]");
    return Array.isArray(saved) ? saved.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function restore(): SearchState {
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) ?? "null");
    if (saved && typeof saved === "object") {
      // A reload cut off a running search: say so instead of spinning forever.
      const sources = (saved.sources ?? []).map((s: SourceState) => (s.state === "pending" ? { ...s, state: "error", error: "已中断" } : s));
      return { ...empty, ...saved, sources, loading: false, error: null, off: restoreOff() };
    }
  } catch {
    /* ignore a corrupt copy */
  }
  return { ...empty, off: restoreOff() };
}

let state = restore();
const listeners = new Set<() => void>();
let saveTimer = 0;

function set(patch: Partial<SearchState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => {
    const { loading: _loading, error: _error, catalog: _catalog, off: _off, ...keep } = state;
    try {
      sessionStorage.setItem(KEY, JSON.stringify(keep));
    } catch {
      /* storage full or disabled: persistence is best-effort */
    }
  }, 150);
}

export function useSearch(): SearchState {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
  );
}

export const searchActions = {
  setQuery: (query: string) => set({ query }),
  setDur: (dur: string) => set({ dur }),
  toggleRejected: () => set({ showRejected: !state.showRejected }),
  setTrack: (track: Track) => set({ track }),
  setLrc: (key: string, on: boolean) => set({ lrc: { ...state.lrc, [key]: on } }),
  /** The sources the switches currently allow, or undefined before they are known (= all). */
  chosenProviders: () => (state.catalog.length ? state.catalog.filter((p) => !state.off.includes(p.id)).map((p) => p.id) : undefined),
  setPicked: (token: string, track: Track) => set({ picked: { ...state.picked, [token]: track } }),
  /** Fetch the enabled providers once, for the source switches. */
  async loadCatalog() {
    const all = await api.providers();
    set({
      catalog: all
        .filter((p) => p.enabled)
        .sort((a, b) => a.order - b.order)
        .map(({ id, label }) => ({ id, label })),
    });
  },
  /** Flip one source on or off for single search; refuses to switch off the last one. */
  toggleSource(id: string): boolean {
    const off = state.off.includes(id) ? state.off.filter((x) => x !== id) : [...state.off, id];
    if (state.catalog.length && state.catalog.every((p) => off.includes(p.id))) return false;
    set({ off });
    try {
      localStorage.setItem(OFF_KEY, JSON.stringify(off));
    } catch {
      /* best-effort */
    }
    return true;
  },
  clearError: () => set({ error: null }),
  /** After a reload the saved track may be out of date (a download finished meanwhile). */
  async refreshTrack() {
    const t = state.track;
    if (!t || state.loading) return;
    const { tracks } = await api.playlist(t.playlist_id);
    const fresh = tracks.find((x) => x.id === t.id);
    if (fresh && state.track?.id === fresh.id) set({ track: fresh });
  },
  run,
};

// Keep the shown track in step with downloads finishing in the background.
onEvent("track", (t) => {
  if (state.track && state.track.id === t.id) set({ track: t });
  const token = Object.keys(state.picked).find((k) => state.picked[k].id === t.id);
  if (token) set({ picked: { ...state.picked, [token]: t } });
});

let controller: AbortController | null = null;

async function run() {
  const query = state.query.trim();
  if (!query) return;
  const m = state.dur.trim().match(/^(\d+):(\d{1,2})$/);
  const seconds = m ? Number(m[1]) * 60 + Number(m[2]) : state.dur.trim() ? Number(state.dur) : undefined;
  const duration = seconds && isFinite(seconds) ? seconds : undefined;

  controller?.abort();
  const mine = new AbortController();
  controller = mine;
  const startedAt = performance.now();
  // Only switch-selected sources; before the catalog loads, let the server use all of them.
  const providers = searchActions.chosenProviders();
  set({
    mode: "song",
    loading: true,
    showRejected: false,
    session: state.session + 1,
    track: null,
    cands: [],
    sources: [],
    startedAt,
    elapsed: null,
    error: null,
    artist: null,
    songs: [],
    lrc: {},
    picked: {},
  });
  const settle = (s: SourceState) => (s.state === "pending" ? { ...s, state: "error" as const, error: "搜索超时" } : s);

  const handle = (event: SearchEvent) => {
    if (mine.signal.aborted) return;
    if (event.type === "start") {
      set({ sources: event.providers.map((p) => ({ ...p, state: "pending" as const })) });
    } else if (event.type === "progress" || event.type === "source") {
      const known = state.sources.some((s) => s.id === event.provider);
      const skipped = event.type === "source" && event.skipped;
      const next: SourceState = {
        id: event.provider,
        label: event.label,
        state: skipped ? "skipped" : event.ok ? "ok" : "error",
        error: event.error,
      };
      const sources = known ? state.sources.map((s) => (s.id === event.provider ? next : s)) : [...state.sources, next];
      set(event.type === "progress" ? { cands: event.candidates, sources } : { sources });
    } else if (event.type === "done") {
      set({ track: event.track, cands: event.candidates, sources: state.sources.map(settle) });
    } else if (event.type === "artist") {
      set({ mode: "artist", artist: { name: event.name, aliases: event.aliases ?? [], cover_url: event.cover_url ?? null } });
    } else if (event.type === "artist_songs") {
      set({ mode: "artist", songs: event.songs });
    } else if (event.type === "artist_done") {
      set({ mode: "artist", songs: event.songs, sources: state.sources.map(settle) });
    } else {
      set({ error: event.message });
    }
  };

  try {
    try {
      await api.searchStream(query, duration, providers, handle, mine.signal);
    } catch (error) {
      // Older server without the streaming endpoint: fall back to the one-shot search.
      if (!(error instanceof ApiError) || error.code !== "no_stream") throw error;
      const r = await api.searchOne(query, duration, providers);
      if (r.mode === "artist") {
        handle({ type: "artist", ...r.artist });
        handle({ type: "artist_done", songs: r.songs });
      } else handle({ type: "done", track: r.track, candidates: r.candidates });
    }
  } catch (e) {
    if ((e as Error).name !== "AbortError") set({ error: (e as Error).message });
  } finally {
    if (controller === mine) set({ loading: false, elapsed: (performance.now() - startedAt) / 1000 });
  }
}
