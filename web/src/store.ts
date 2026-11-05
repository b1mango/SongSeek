import { useEffect, useSyncExternalStore } from "react";
import { api } from "./api";
import type { Job, Playlist, Track, TrackProgress } from "./types";

type EventMap = { track: Track; job: Job; playlist: Playlist; track_progress: TrackProgress };
type Handler<K extends keyof EventMap> = (data: EventMap[K]) => void;

const handlers: { [K in keyof EventMap]: Set<Handler<K>> } = {
  track: new Set(),
  job: new Set(),
  playlist: new Set(),
  track_progress: new Set(),
};

export function onEvent<K extends keyof EventMap>(type: K, fn: Handler<K>) {
  handlers[type].add(fn);
  return () => {
    handlers[type].delete(fn);
  };
}

/* ---------- global store: playlists, jobs, connection ---------- */

interface State {
  playlists: Playlist[];
  jobs: Job[];
  connected: boolean;
  lyricsPref: boolean | null; // settings.lyrics: save .lrc alongside downloads (null until loaded)
}

let state: State = { playlists: [], jobs: [], connected: false, lyricsPref: null };
const listeners = new Set<() => void>();

function set(patch: Partial<State>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

export function useStore<T>(select: (s: State) => T): T {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => select(state),
  );
}

export function upsertPlaylist(p: Playlist) {
  const exists = state.playlists.some((x) => x.id === p.id);
  set({
    playlists: exists ? state.playlists.map((x) => (x.id === p.id ? p : x)) : [p, ...state.playlists],
  });
}

export function removePlaylist(id: number) {
  set({ playlists: state.playlists.filter((p) => p.id !== id) });
}

export function upsertJob(j: Job) {
  const others = state.jobs.filter((x) => x.id !== j.id);
  set({ jobs: [j, ...others].sort((a, b) => b.id - a.id).slice(0, 20) });
}

export function setLyricsPref(on: boolean) {
  set({ lyricsPref: on });
}

export async function refreshPlaylists() {
  set({ playlists: await api.playlists() });
}

let source: EventSource | null = null;

function connect() {
  source?.close();
  source = new EventSource("/api/events");
  source.onopen = () => {
    set({ connected: true });
    // Catch up on whatever changed while disconnected.
    refreshPlaylists().catch(() => {});
    api.jobs().then((jobs) => set({ jobs })).catch(() => {});
    api.settings().then((s) => set({ lyricsPref: s.lyrics ?? true })).catch(() => {});
  };
  source.onerror = () => set({ connected: false });
  for (const type of Object.keys(handlers) as (keyof EventMap)[]) {
    source.addEventListener(type, (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      if (type === "playlist") upsertPlaylist(data);
      if (type === "job") upsertJob(data);
      if (type === "track_progress") setProgress(data);
      if (type === "track" && data.status !== "downloading") clearProgress(data.id);
      (handlers[type] as Set<(d: unknown) => void>).forEach((fn) => fn(data));
    });
  }
}

export function useLiveConnection() {
  useEffect(() => {
    connect();
    return () => source?.close();
  }, []);
}

/* ---------- per-track download progress (kept apart so a tick only re-renders its own row) ---------- */

const progress = new Map<number, TrackProgress>();
const progressListeners = new Map<number, Set<() => void>>();

function setProgress(p: TrackProgress) {
  progress.set(p.track_id, p);
  progressListeners.get(p.track_id)?.forEach((l) => l());
}

function clearProgress(trackId: number) {
  if (progress.delete(trackId)) progressListeners.get(trackId)?.forEach((l) => l());
}

export function useTrackProgress(trackId: number): TrackProgress | null {
  return useSyncExternalStore(
    (l) => {
      const set = progressListeners.get(trackId) ?? new Set();
      progressListeners.set(trackId, set.add(l));
      return () => {
        set.delete(l);
        if (!set.size) progressListeners.delete(trackId);
      };
    },
    () => progress.get(trackId) ?? null,
  );
}
