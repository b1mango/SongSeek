import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Pause, Play, X, LoaderCircle } from "lucide-react";
import { fmtDuration } from "./format";

export interface Playable {
  key: string; // e.g. "c:12" for a candidate preview, "t:5" for a downloaded file
  url: string;
  title: string;
  subtitle: string;
}

interface PlayerState {
  item: Playable | null;
  playing: boolean;
  loading: boolean;
  error: string | null;
  toggle: (p: Playable) => void;
  stop: () => void;
}

const Ctx = createContext<PlayerState | null>(null);

export function usePlayer() {
  const v = useContext(Ctx);
  if (!v) throw new Error("usePlayer outside PlayerProvider");
  return v;
}

export function PlayerProvider({ children }: { children: ReactNode }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [item, setItem] = useState<Playable | null>(null);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [time, setTime] = useState(0);
  const [length, setLength] = useState(0);

  if (!audio.current && typeof Audio !== "undefined") audio.current = new Audio();

  useEffect(() => {
    const a = audio.current!;
    const on = (type: string, fn: () => void) => a.addEventListener(type, fn);
    on("play", () => setPlaying(true));
    on("pause", () => setPlaying(false));
    on("ended", () => setPlaying(false));
    on("waiting", () => setLoading(true));
    on("loadstart", () => setLoading(true));
    on("canplay", () => setLoading(false));
    on("playing", () => setLoading(false));
    on("timeupdate", () => setTime(a.currentTime));
    on("durationchange", () => setLength(isFinite(a.duration) ? a.duration : 0));
    on("error", () => {
      setLoading(false);
      setPlaying(false);
      setError("无法播放：音源拒绝了试听请求或链接已失效");
    });
    return () => a.pause();
  }, []);

  const toggle = useCallback(
    (p: Playable) => {
      const a = audio.current!;
      if (item?.key === p.key) {
        if (a.paused) a.play().catch(() => {});
        else a.pause();
        return;
      }
      setError(null);
      setTime(0);
      setLength(0);
      setItem(p);
      a.src = p.url;
      a.play().catch(() => {});
    },
    [item],
  );

  const stop = useCallback(() => {
    const a = audio.current!;
    a.pause();
    a.removeAttribute("src");
    a.load();
    setItem(null);
    setError(null);
  }, []);

  const value = useMemo(() => ({ item, playing, loading, error, toggle, stop }), [item, playing, loading, error, toggle, stop]);

  return (
    <Ctx.Provider value={value}>
      {children}
      {item && (
        <div className="player" role="region" aria-label="播放器">
          <button className="icon-btn player-toggle" onClick={() => toggle(item)} aria-label={playing ? "暂停" : "播放"}>
            {loading ? <LoaderCircle className="spin" size={18} /> : playing ? <Pause size={18} /> : <Play size={18} />}
          </button>
          <div className="player-meta">
            <div className="player-title">{item.title}</div>
            <div className="player-sub">{error ?? item.subtitle}</div>
          </div>
          <span className="mono player-time">{fmtDuration(time)}</span>
          <input
            className="player-seek"
            type="range"
            min={0}
            max={length || 1}
            step={0.1}
            value={Math.min(time, length || 1)}
            onChange={(e) => {
              audio.current!.currentTime = Number(e.target.value);
            }}
            aria-label="进度"
          />
          <span className="mono player-time">{fmtDuration(length || null)}</span>
          <button className="icon-btn" onClick={stop} aria-label="关闭播放器">
            <X size={16} />
          </button>
        </div>
      )}
    </Ctx.Provider>
  );
}

/** Round play button used in tables and candidate rows. */
export function PlayButton({ playable, size = 32 }: { playable: Playable; size?: number }) {
  const { item, playing, loading, toggle } = usePlayer();
  const active = item?.key === playable.key;
  return (
    <button
      className={`play-btn${active ? " is-active" : ""}`}
      style={{ width: size, height: size }}
      onClick={(e) => {
        e.stopPropagation();
        toggle(playable);
      }}
      aria-label={active && playing ? "暂停试听" : "试听"}
      title={active && playing ? "暂停" : "试听"}
    >
      {active && loading ? <LoaderCircle className="spin" size={size * 0.45} /> : active && playing ? <Pause size={size * 0.42} /> : <Play size={size * 0.42} />}
    </button>
  );
}
