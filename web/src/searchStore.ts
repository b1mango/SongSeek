import { useSyncExternalStore } from "react";
import { api, ApiError, type SearchEvent } from "./api";
import { onEvent } from "./store";
import type { Candidate, Track } from "./types";

/*
 * Single-search state lives outside the page so it survives navigation: a search keeps
 * streaming while you look at another page, and the results are still there when you
 * come back. A copy goes to sessionStorage so a reload restores the last search too.
 */

export interface SourceState {
  id: string;
  label: string;
  state: "pending" | "ok" | "error";
  error?: string | null;
}

export interface SearchState {
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
}

const KEY = "songseek.search";
const empty: SearchState = {
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
};

function restore(): SearchState {
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) ?? "null");
    if (saved && typeof saved === "object") {
      // A reload cut off a running search: say so instead of spinning forever.
      const sources = (saved.sources ?? []).map((s: SourceState) => (s.state === "pending" ? { ...s, state: "error", error: "已中断" } : s));
      return { ...empty, ...saved, sources, loading: false, error: null };
    }
  } catch {
    /* ignore a corrupt copy */
  }
  return empty;
}

let state = restore();
const listeners = new Set<() => void>();
let saveTimer = 0;

function set(patch: Partial<SearchState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => {
    const { loading: _loading, error: _error, ...keep } = state;
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
  set({ loading: true, showRejected: false, session: state.session + 1, track: null, cands: [], sources: [], startedAt, elapsed: null, error: null });

  const handle = (event: SearchEvent) => {
    if (mine.signal.aborted) return;
    if (event.type === "start") {
      set({ sources: event.providers.map((p) => ({ ...p, state: "pending" as const })) });
    } else if (event.type === "progress") {
      const known = state.sources.some((s) => s.id === event.provider);
      const next: SourceState = { id: event.provider, label: event.label, state: event.ok ? "ok" : "error", error: event.error };
      set({ cands: event.candidates, sources: known ? state.sources.map((s) => (s.id === event.provider ? next : s)) : [...state.sources, next] });
    } else if (event.type === "done") {
      set({
        track: event.track,
        cands: event.candidates,
        sources: state.sources.map((s) => (s.state === "pending" ? { ...s, state: "error" as const, error: "搜索超时" } : s)),
      });
    } else {
      set({ error: event.message });
    }
  };

  try {
    try {
      await api.searchStream(query, duration, handle, mine.signal);
    } catch (error) {
      // Older server without the streaming endpoint: fall back to the one-shot search.
      if (!(error instanceof ApiError) || error.code !== "no_stream") throw error;
      const r = await api.searchOne(query, duration);
      handle({ type: "done", track: r.track, candidates: r.candidates });
    }
  } catch (e) {
    if ((e as Error).name !== "AbortError") set({ error: (e as Error).message });
  } finally {
    if (controller === mine) set({ loading: false, elapsed: (performance.now() - startedAt) / 1000 });
  }
}
