import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, LoaderCircle, Pause, Play, RotateCcw, RotateCw, Volume1, Volume2, VolumeX } from "lucide-react";
import { fmtDuration } from "./format";
import { activeIndex, type Lyrics, type Playable } from "./player";

/**
 * Full-screen "now playing" page: big cover and transport on the left, synced lyrics on
 * the right, the whole thing washed in a blurred copy of the cover.
 */
export function NowPlaying({
  audio,
  item,
  playing,
  loading,
  error,
  time,
  length,
  lyrics,
  cover,
  toggle,
  seek,
  onClose,
}: {
  audio: HTMLAudioElement;
  item: Playable;
  playing: boolean;
  loading: boolean;
  error: string | null;
  time: number;
  length: number;
  lyrics: Lyrics | null;
  cover: string | null;
  toggle: () => void;
  seek: (t: number) => void;
  onClose: () => void;
}) {
  const [leaving, setLeaving] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  const seekRef = useRef<HTMLInputElement | null>(null);

  const close = () => {
    if (leaving) return;
    setLeaving(true);
    window.setTimeout(onClose, 380);
  };

  // Make #root inert, lock scrolling underneath, and take keyboard focus while open.
  useEffect(() => {
    const appRoot = document.getElementById("root");
    appRoot?.setAttribute("inert", "");
    document.documentElement.classList.add("np-open");
    const previous = document.activeElement as HTMLElement | null;
    root.current?.focus();
    return () => {
      appRoot?.removeAttribute("inert");
      document.documentElement.classList.remove("np-open");
      previous?.focus?.();
    };
  }, []);

  useEffect(() => {
    if (seekRef.current && document.activeElement !== seekRef.current) {
      const p = length ? Math.min(100, (time / length) * 100) : 0;
      seekRef.current.style.setProperty("--p", `${p}%`);
      seekRef.current.value = String(Math.min(time, length || 1));
    }
  }, [time, length]);

  useEffect(() => {
    if (!playing || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let frame = 0;
    const tick = () => {
      const t = audio.currentTime;
      const dur = isFinite(audio.duration) ? audio.duration : 0;
      if (seekRef.current) {
        const p = dur ? Math.min(100, (t / dur) * 100) : 0;
        seekRef.current.style.setProperty("--p", `${p}%`);
        if (document.activeElement !== seekRef.current) {
          seekRef.current.value = String(Math.min(t, dur || 1));
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [audio, playing]);

  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (e.key === "Escape") return closeRef.current();
      if (target.tagName === "INPUT" && (target as HTMLInputElement).type !== "range") return;
      if (e.key === " " && target.tagName !== "BUTTON") {
        e.preventDefault();
        toggle();
      } else if (e.key === "ArrowLeft") seek(Math.max(0, audio.currentTime - 5));
      else if (e.key === "ArrowRight") seek(Math.min(audio.duration || 0, audio.currentTime + 5));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [audio, toggle, seek]);

  const pct = length ? Math.min(100, (time / length) * 100) : 0;
  const hasLyrics = !!lyrics?.lines.some((l) => l.text);

  return createPortal(
    <div
      ref={root}
      className={`np${leaving ? " is-leaving" : ""}${hasLyrics ? "" : " no-lyrics"}${playing ? " is-playing" : ""}`}
      role="dialog"
      aria-modal="true"
      aria-label={`正在播放：${item.title}`}
      tabIndex={-1}
    >
      <div className="np-bg" aria-hidden>
        {cover ? (
          <>
            <span className="np-bg-art a" style={{ backgroundImage: `url("${cover}")` }} />
            <span className="np-bg-art b" style={{ backgroundImage: `url("${cover}")` }} />
          </>
        ) : (
          <>
            <span className="np-blob a" />
            <span className="np-blob b" />
            <span className="np-blob c" />
          </>
        )}
        <span className="np-bg-shade" />
      </div>

      <header className="np-top pywebview-drag-region">
        <button className="np-icon" onClick={close} aria-label="收起播放页" title="收起（Esc）">
          <ChevronDown size={22} />
        </button>
        <Volume audio={audio} />
      </header>

      <main className="np-main">
        <section className="np-left">
          <div className={`np-cover${cover ? "" : " is-vinyl"}`}>
            {cover ? <img src={cover} alt="" draggable={false} /> : <span className={`np-vinyl${playing ? " is-spinning" : ""}`} />}
          </div>

          <div className="np-meta">
            <h2 title={item.title}>{item.title}</h2>
            <p title={error ?? item.subtitle} className={error ? "is-error" : undefined}>
              {error ?? item.subtitle}
            </p>
          </div>

          <div className="np-progress">
            <input
              ref={seekRef}
              className="np-seek"
              type="range"
              min={0}
              max={length || 1}
              step={0.1}
              defaultValue={Math.min(time, length || 1)}
              style={{ "--p": `${pct}%` } as CSSProperties}
              onChange={(e) => {
                const next = Number(e.target.value);
                const p = length ? Math.min(100, (next / length) * 100) : 0;
                e.currentTarget.style.setProperty("--p", `${p}%`);
                seek(next);
              }}
              aria-label="进度"
              disabled={!!error}
            />
            <div className="np-times mono">
              <span>{fmtDuration(time)}</span>
              <span>{length ? `-${fmtDuration(Math.max(0, length - time))}` : "--:--"}</span>
            </div>
          </div>

          <div className="np-controls">
            <button className="np-icon np-skip" onClick={() => seek(Math.max(0, time - 10))} aria-label="后退 10 秒" title="后退 10 秒" disabled={!!error}>
              <RotateCcw size={22} />
              <small>10</small>
            </button>
            <button className="np-play" onClick={toggle} aria-label={playing ? "暂停" : "播放"}>
              {loading && !error ? <LoaderCircle className="spin" size={30} /> : playing ? <Pause size={34} fill="currentColor" strokeWidth={0} /> : <Play size={34} fill="currentColor" strokeWidth={0} className="nudge" />}
            </button>
            <button className="np-icon np-skip" onClick={() => seek(Math.min(length || 0, time + 10))} aria-label="前进 10 秒" title="前进 10 秒" disabled={!!error}>
              <RotateCw size={22} />
              <small>10</small>
            </button>
          </div>
        </section>

        {hasLyrics && <BigLyrics audio={audio} playing={playing} lyrics={lyrics!} time={time} length={length} onSeek={seek} />}
      </main>

      {lyrics?.source && <div className="np-source">歌词 · {lyrics.source}</div>}
    </div>,
    document.body,
  );
}

function Volume({ audio }: { audio: HTMLAudioElement }) {
  const [volume, setVolume] = useState(audio.volume);
  const [muted, setMuted] = useState(audio.muted);
  useEffect(() => {
    const sync = () => {
      setVolume(audio.volume);
      setMuted(audio.muted);
    };
    audio.addEventListener("volumechange", sync);
    return () => audio.removeEventListener("volumechange", sync);
  }, [audio]);
  const level = muted ? 0 : volume;
  const Icon = level === 0 ? VolumeX : level < 0.5 ? Volume1 : Volume2;
  return (
    <div className="np-volume">
      <button className="np-icon" onClick={() => (audio.muted = !audio.muted)} aria-label={muted ? "取消静音" : "静音"} title={muted ? "取消静音" : "静音"}>
        <Icon size={18} />
      </button>
      <input
        className="np-seek np-volume-range"
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={level}
        style={{ "--p": `${level * 100}%` } as CSSProperties}
        onChange={(e) => {
          const v = Number(e.target.value);
          audio.volume = v;
          audio.muted = v === 0;
          if (v > 0) localStorage.setItem("songseek.volume", String(v));
        }}
        aria-label="音量"
      />
    </div>
  );
}

function BigLyrics({
  audio,
  playing,
  lyrics,
  time,
  length,
  onSeek,
}: {
  audio: HTMLAudioElement;
  playing: boolean;
  lyrics: Lyrics;
  time: number;
  length: number;
  onSeek: (t: number) => void;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const touched = useRef(0);
  const [browsing, setBrowsing] = useState(false);
  const browseTimer = useRef(0);
  const synced = lyrics.synced;

  const active = synced ? activeIndex(lyrics.lines, time) : -1;

  const computeSweep = (t: number, idx: number) => {
    if (idx < 0) return 0;
    const start = lyrics.lines[idx].t;
    const next = lyrics.lines.slice(idx + 1).find((l) => l.t > start)?.t ?? Math.max(start + 4, length || start + 4);
    return Math.min(1, Math.max(0, (t + 0.15 - start) / Math.max(0.6, Math.min(next - start, 12))));
  };

  const sweep = computeSweep(time, active);

  // Update active line --sweep directly via DOM ref at 60fps while playing, zero React re-renders.
  useEffect(() => {
    if (!playing || !synced || active < 0 || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let frame = 0;
    const tick = () => {
      const span = box.current?.querySelector<HTMLElement>(".np-line.is-active .np-line-text");
      if (span) {
        const s = computeSweep(audio.currentTime, active);
        span.style.setProperty("--sweep", `${(s * 100).toFixed(1)}%`);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  });

  useEffect(() => {
    const el = box.current;
    if (!el || active < 0 || performance.now() - touched.current < 3000) return;
    const row = el.querySelector<HTMLElement>(`[data-i="${active}"]`);
    if (!row) return;
    // Keep the sung line a little above centre, like a lyric sheet you read downwards.
    el.scrollTo({ top: row.offsetTop - el.clientHeight * 0.38 + row.clientHeight / 2, behavior: "smooth" });
  }, [active]);

  const markTouched = () => {
    touched.current = performance.now();
    setBrowsing(true);
    window.clearTimeout(browseTimer.current);
    browseTimer.current = window.setTimeout(() => setBrowsing(false), 3000);
  };
  useEffect(() => () => window.clearTimeout(browseTimer.current), []);

  return (
    <section className={`np-lyrics${synced ? "" : " is-plain"}${browsing ? " is-browsing" : ""}`} ref={box} onWheel={markTouched} onTouchMove={markTouched} aria-label="歌词">
      <div className="np-lyrics-pad top" />
      {lyrics.lines.map((l, i) => {
        if (!l.text) return null;
        if (!synced)
          return (
            <p key={i} className="np-line">
              <span className="np-line-text">{l.text}</span>
              {l.trans && <small>{l.trans}</small>}
            </p>
          );
        const distance = active < 0 ? Math.min(i + 1, 4) : Math.min(Math.abs(i - active), 4);
        const state = i === active ? " is-active" : i < active ? " is-past" : "";
        return (
          <button
            key={i}
            data-i={i}
            className={`np-line${state}`}
            style={{ "--d": distance } as CSSProperties}
            onClick={() => onSeek(l.t)}
          >
            <span className="np-line-text" style={i === active ? ({ "--sweep": `${(sweep * 100).toFixed(1)}%` } as CSSProperties) : undefined}>
              {l.text}
            </span>
            {l.trans && <small>{l.trans}</small>}
          </button>
        );
      })}
      <div className="np-lyrics-pad" />
    </section>
  );
}
