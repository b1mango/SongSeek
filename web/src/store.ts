import { useEffect, useSyncExternalStore } from "react";
import { api } from "./api";
import { navigate } from "./router";
import type { Job, Playlist, PlaylistDeleted, Track, TrackDeleted, TrackProgress } from "./types";

type EventMap = {
  track: Track;
  track_deleted: TrackDeleted;
  job: Job;
  playlist: Playlist;
  playlist_deleted: PlaylistDeleted;
  track_progress: TrackProgress;
  resync: null;
};
type Handler<K extends keyof EventMap> = (data: EventMap[K]) => void;

const handlers: { [K in keyof EventMap]: Set<Handler<K>> } = {
  track: new Set(),
  track_deleted: new Set(),
  job: new Set(),
  playlist: new Set(),
  playlist_deleted: new Set(),
  track_progress: new Set(),
  resync: new Set(),
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
export const getState = () => state;
const listeners = new Set<() => void>();
// IDs are AUTOINCREMENT on the server, so deleted IDs are never reused in this session.
const deletedTracks = new Set<number>();
const deletedPlaylists = new Set<number>();
let playlistRevision = 0;
let playlistRequest = 0;
const playlistRevisions = new Map<number, number>();

export const isPlaylistDeleted = (id: number) => deletedPlaylists.has(id);
export const isTrackDeleted = (id: number, playlistId: number) => deletedTracks.has(id) || isPlaylistDeleted(playlistId);
export const getPlaylistRevision = (id: number) => playlistRevisions.get(id) ?? 0;

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
  if (isPlaylistDeleted(p.id)) return;
  playlistRevisions.set(p.id, ++playlistRevision);
  const exists = state.playlists.some((x) => x.id === p.id);
  set({
    playlists: exists ? state.playlists.map((x) => (x.id === p.id ? p : x)) : [p, ...state.playlists],
  });
}

export function removePlaylist(id: number) {
  deletedPlaylists.add(id);
  playlistRevisions.set(id, ++playlistRevision);
  for (const p of progress.values()) if (p.playlist_id === id) clearProgress(p.track_id);
  set({ playlists: state.playlists.filter((p) => p.id !== id) });
}

export function removeTrack(data: TrackDeleted) {
  if (deletedTracks.has(data.id)) return;
  deletedTracks.add(data.id);
  playlistRevisions.set(data.playlist_id, ++playlistRevision);
  clearProgress(data.id);
  handlers.track_deleted.forEach((fn) => fn(data));
  const [path, query = ""] = location.hash.split("?");
  if (path === `#/playlist/${data.playlist_id}/review`) {
    if (new URLSearchParams(query).get("track") === String(data.id)) navigate({ page: "playlist", id: data.playlist_id }, true);
    else {
      // ReviewPage has its own queue and refreshes it through its existing resync handler.
      handlers.resync.forEach((fn) => fn(null));
    }
  }
}

export function upsertJob(j: Job) {
  const others = state.jobs.filter((x) => x.id !== j.id);
  set({ jobs: [j, ...others].sort((a, b) => b.id - a.id).slice(0, 20) });
}

export function setLyricsPref(on: boolean) {
  set({ lyricsPref: on });
}

export async function refreshPlaylists() {
  const request = ++playlistRequest;
  const revision = playlistRevision;
  const playlists = await api.playlists();
  if (request !== playlistRequest) return;
  const changed = new Map(state.playlists.filter((p) => getPlaylistRevision(p.id) > revision).map((p) => [p.id, p]));
  const result = playlists.filter((p) => !isPlaylistDeleted(p.id)).map((p) => {
    const latest = changed.get(p.id) ?? p;
    changed.delete(p.id);
    return latest;
  });
  set({ playlists: [...changed.values(), ...result] });
}

let source: EventSource | null = null;
let opened = false;

function connect() {
  source?.close();
  source = new EventSource("/api/events");
  source.onopen = () => {
    // Events sent while we were away are lost: pages holding track lists reload them on `resync`.
    if (opened) handlers.resync.forEach((fn) => (fn as (d: null) => void)(null));
    opened = true;
    set({ connected: true });
    // Catch up on whatever changed while disconnected.
    refreshPlaylists().catch(() => {});
    api
      .jobs()
      .then((jobs) => set({ jobs }))
      .catch(() => {});
    api
      .settings()
      .then((s) => set({ lyricsPref: s.lyrics ?? true }))
      .catch(() => {});
  };
  source.onerror = () => set({ connected: false });
  for (const type of (Object.keys(handlers) as (keyof EventMap)[]).filter((k) => k !== "resync")) {
    source.addEventListener(type, (e) => {
      const data = JSON.parse((e as MessageEvent).data);
      if (type === "track_deleted") {
        removeTrack(data);
        return;
      }
      if (type === "playlist_deleted") {
        removePlaylist(data.id);
        const path = location.hash.split("?")[0];
        if (path === `#/playlist/${data.id}` || path === `#/playlist/${data.id}/review`) navigate({ page: "home" }, true);
      }
      if (type === "playlist" && isPlaylistDeleted(data.id)) return;
      if (type === "track" && isTrackDeleted(data.id, data.playlist_id)) return;
      if (type === "track_progress" && isTrackDeleted(data.track_id, data.playlist_id)) return;
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
