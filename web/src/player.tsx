import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { LoaderCircle, Maximize2, Pause, Play, ScrollText, X } from "lucide-react";
import { NowPlaying } from "./nowplaying";
import { audioError } from "./api";
import { fmtDuration } from "./format";

export interface Playable {
  key: string; // e.g. "c:12" for a candidate preview, "t:5" for a downloaded file
  url: string;
  title: string;
  subtitle: string;
  lyricsUrl?: string;
  coverUrl?: string;
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
  const current = useRef<string | null>(null);

  if (!audio.current && typeof Audio !== "undefined") {
    audio.current = new Audio();
    const saved = Number(localStorage.getItem("songseek.volume"));
    if (saved > 0 && saved <= 1) audio.current.volume = saved;
  }

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
    on("error", () => {
      const url = a.currentSrc || a.src;
      if (!url || !current.current) return;
      const key = current.current;
      setLoading(true);
      setPlaying(false);
      setError("试听失败，正在查原因…");
      audioError(url).then((message) => {
        if (current.current !== key) return;
        setLoading(false);
        setError(message ? `无法试听：${message}` : "无法试听：音源返回的音频无法播放，换一个候选试试");
      });
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
      setItem(p);
      current.current = p.key;
      a.src = p.url;
      a.play().catch(() => {});
    },
    [item],
  );

  const stop = useCallback(() => {
    const a = audio.current!;
    current.current = null;
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
      {item && audio.current && <PlayerBar audio={audio.current} item={item} playing={playing} loading={loading} error={error} toggle={toggle} stop={stop} />}
    </Ctx.Provider>
  );
}

/* ---------------- lyrics ---------------- */

export interface LyricLine {
  t: number;
  text: string;
  trans?: string;
}

export interface Lyrics {
  lines: LyricLine[];
  synced: boolean;
  source: string | null;
}

const TAG = /\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;

function parseLrc(text: string): { lines: LyricLine[]; synced: boolean } {
  const offsetTag = text.match(/\[offset:\s*([+-]?\d+)\]/i);
  const offset = offsetTag ? Number(offsetTag[1]) / 1000 : 0;
  const lines: LyricLine[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const stamps = [...raw.matchAll(TAG)];
    if (!stamps.length) continue;
    const body = raw.replace(TAG, "").trim();
    for (const m of stamps) {
      const frac = m[3] ? Number(m[3]) / 10 ** m[3].length : 0;
      lines.push({ t: Math.max(0, Number(m[1]) * 60 + Number(m[2]) + frac - offset), text: body });
    }
  }
  if (lines.length) {
    // Saved .lrc files carry translations as a second line with the same timestamp.
    const merged: LyricLine[] = [];
    for (const line of lines.sort((a, b) => a.t - b.t)) {
      const prev = merged[merged.length - 1];
      if (prev && Math.abs(prev.t - line.t) < 0.002 && prev.text && line.text && !prev.trans) prev.trans = line.text;
      else merged.push(line);
    }
    return { lines: merged, synced: true };
  }
  const plain = text
    .split(/\r?\n/)
    .map((l) => l.replace(/^\[[a-z]+:.*\]$/i, "").trim())
    .filter(Boolean);
  return { lines: plain.map((text) => ({ t: -1, text })), synced: false };
}

function attachTranslation(lines: LyricLine[], translation: string) {
  const { lines: trans, synced } = parseLrc(translation);
  if (!synced) return;
  const byTime = new Map(trans.filter((l) => l.text).map((l) => [Math.round(l.t * 10), l.text]));
  for (const line of lines) {
    const hit = byTime.get(Math.round(line.t * 10)) ?? byTime.get(Math.round(line.t * 10) - 1) ?? byTime.get(Math.round(line.t * 10) + 1);
    if (hit && hit !== line.text) line.trans = hit;
  }
}

function useLyrics(url: string | undefined): Lyrics | null {
  const [lyrics, setLyrics] = useState<Lyrics | null>(null);
  useEffect(() => {
    setLyrics(null);
    if (!url) return;
    const controller = new AbortController();
    fetch(url, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { lrc?: string | null; translation?: string | null; source_label?: string | null } | null) => {
        if (!data?.lrc) return;
        const parsed = parseLrc(data.lrc);
        if (!parsed.lines.some((l) => l.text)) return;
        if (parsed.synced && data.translation) attachTranslation(parsed.lines, data.translation);
        setLyrics({ ...parsed, source: data.source_label ?? null });
      })
      .catch(() => {});
    return () => controller.abort();
  }, [url]);
  return lyrics;
}

/** Index of the line being sung at `time` (lines are sorted); -1 before the first line. */
export function activeIndex(lines: LyricLine[], time: number) {
  let lo = 0;
  let hi = lines.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid].t <= time + 0.15) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/* ---------------- bar ---------------- */

const PANEL_KEY = "songseek.lyricsOpen";

/** Resolves to the cover URL once the image has actually loaded; null when there is none. */
export function useCover(url: string | undefined) {
  const [ready, setReady] = useState<string | null>(null);
  useEffect(() => {
    setReady(null);
    if (!url) return;
    const img = new Image();
    let live = true;
    img.onload = () => live && setReady(url);
    img.src = url;
    return () => {
      live = false;
      img.onload = null;
    };
  }, [url]);
  return ready;
}

function PlayerBar({
  audio,
  item,
  playing,
  loading,
  error,
  toggle,
  stop,
}: {
  audio: HTMLAudioElement;
  item: Playable;
  playing: boolean;
  loading: boolean;
  error: string | null;
} & Pick<PlayerState, "toggle" | "stop">) {
  const [time, setTime] = useState(0);
  const [length, setLength] = useState(0);
  const [closing, setClosing] = useState(false);
  const [open, setOpen] = useState(() => localStorage.getItem(PANEL_KEY) === "1");
  const lyrics = useLyrics(item.lyricsUrl);
  const cover = useCover(item.coverUrl);
  const [full, setFull] = useState(false);

  // Follow the clock every frame while playing so the progress fill and lyrics move smoothly.
  useEffect(() => {
    const sync = () => {
      setTime(audio.currentTime);
      setLength(isFinite(audio.duration) ? audio.duration : 0);
    };
    sync();
    const events = ["timeupdate", "durationchange", "seeked", "emptied", "loadedmetadata"];
    events.forEach((e) => audio.addEventListener(e, sync));
    let frame = 0;
    const tick = () => {
      sync();
      frame = requestAnimationFrame(tick);
    };
    if (playing) frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      events.forEach((e) => audio.removeEventListener(e, sync));
    };
  }, [audio, playing, item.key]);

  const latestKey = useRef(item.key);
  latestKey.current = item.key;
  useEffect(() => setClosing(false), [item.key]);

  const synced = !!lyrics?.synced;
  const active = synced ? activeIndex(lyrics!.lines, time) : -1;
  const line = active >= 0 ? lyrics!.lines[active] : null;
  const pct = length ? Math.min(100, (time / length) * 100) : 0;

  function close() {
    const key = item.key;
    setClosing(true);
    // Only stop if nothing new started playing during the exit animation.
    window.setTimeout(() => latestKey.current === key && stop(), 220);
  }

  function setPanel(next: boolean) {
    setOpen(next);
    localStorage.setItem(PANEL_KEY, next ? "1" : "0");
  }

  function seek(t: number) {
    audio.currentTime = t;
    setTime(t);
    if (audio.paused) audio.play().catch(() => {});
  }

  const expanded = !!(open && lyrics && !closing);
  // Leave room under the page content so the tall lyrics panel never hides the last rows.
  useEffect(() => {
    document.documentElement.classList.toggle("lyrics-open", expanded);
    return () => document.documentElement.classList.remove("lyrics-open");
  }, [expanded]);

  const cls = ["player", error && "has-error", closing && "is-closing", open && lyrics && "is-open"].filter(Boolean).join(" ");

  return (
    <>
    {full && (
      <NowPlaying
        audio={audio}
        item={item}
        playing={playing}
        loading={loading}
        error={error}
        time={time}
        length={length}
        lyrics={lyrics}
        cover={cover}
        toggle={() => toggle(item)}
        seek={seek}
        onClose={() => setFull(false)}
      />
    )}
    <div className={cls} role="region" aria-label="播放器">
      <div className="player-panel" aria-hidden={!(open && lyrics)}>
        <div className="player-panel-inner">
          {lyrics && <LyricsPanel lyrics={lyrics} active={active} visible={open} onSeek={seek} />}
          {lyrics?.source && <div className="lyrics-source">歌词 · {lyrics.source}</div>}
        </div>
      </div>

      <div className="player-bar">
        <button
          className={`disc${playing ? " is-spinning" : ""}${loading ? " is-loading" : ""}`}
          onClick={() => setFull(true)}
          aria-label="打开播放页"
          title="打开播放页"
        >
          <span className="disc-label" style={cover ? { backgroundImage: `url("${cover}")` } : undefined} data-cover={cover ? "" : undefined} />
          <Maximize2 className="disc-hint" size={14} aria-hidden />
        </button>

        <div className="player-meta">
          <div className="player-title" title={item.title}>
            {item.title}
          </div>
          <div className="player-sub" title={error ?? item.subtitle}>
            {error ?? item.subtitle}
          </div>
        </div>

        {!error && (
          <div className="player-center">
            <div className="player-line" aria-live="off">
              {line?.text ? (
                <span key={active} className="player-line-text">
                  {line.text}
                </span>
              ) : null}
            </div>
            <div className="player-progress">
              <span className="mono player-time">{fmtDuration(time)}</span>
              <input
                className="player-seek"
                type="range"
                min={0}
                max={length || 1}
                step={0.1}
                value={Math.min(time, length || 1)}
                style={{ "--p": `${pct}%` } as CSSProperties}
                onChange={(e) => {
                  audio.currentTime = Number(e.target.value);
                  setTime(audio.currentTime);
                }}
                aria-label="进度"
              />
              <span className="mono player-time">{fmtDuration(length || null)}</span>
            </div>
          </div>
        )}

        <div className="player-actions">
          {lyrics && (
            <button className={`icon-btn player-lyrics-btn${open ? " is-on" : ""}`} onClick={() => setPanel(!open)} aria-pressed={open} aria-label="歌词" title={open ? "收起歌词" : "展开歌词"}>
              <ScrollText size={16} />
            </button>
          )}
          <button className="player-toggle" onClick={() => toggle(item)} aria-label={playing ? "暂停" : "播放"}>
            {loading && !error ? <LoaderCircle className="spin" size={18} /> : playing ? <Pause size={18} fill="currentColor" strokeWidth={0} /> : <Play size={18} fill="currentColor" strokeWidth={0} className="nudge" />}
          </button>
          <button className="icon-btn player-close" onClick={close} aria-label="关闭播放器" title="关闭">
            <X size={16} />
          </button>
        </div>
      </div>
    </div>
    </>
  );
}

function LyricsPanel({ lyrics, active, visible, onSeek }: { lyrics: Lyrics; active: number; visible: boolean; onSeek: (t: number) => void }) {
  const box = useRef<HTMLDivElement | null>(null);
  const touched = useRef(0);

  // Keep the sung line centred, unless the listener has just scrolled by hand.
  useEffect(() => {
    const el = box.current;
    if (!el || !visible || active < 0 || performance.now() - touched.current < 2500) return;
    const row = el.querySelector<HTMLElement>(`[data-i="${active}"]`);
    if (!row) return;
    el.scrollTo({ top: row.offsetTop - el.clientHeight / 2 + row.clientHeight / 2, behavior: "smooth" });
  }, [active, visible]);

  const markTouched = () => {
    touched.current = performance.now();
  };

  return (
    <div className={`lyrics${lyrics.synced ? "" : " is-plain"}`} ref={box} onWheel={markTouched} onTouchMove={markTouched}>
      <div className="lyrics-pad" />
      {lyrics.lines.map((l, i) =>
        l.text ? (
          lyrics.synced ? (
            <button key={i} data-i={i} className={`lyric${i === active ? " is-active" : i < active ? " is-past" : ""}`} onClick={() => onSeek(l.t)} tabIndex={visible ? 0 : -1}>
              <span>{l.text}</span>
              {l.trans && <small>{l.trans}</small>}
            </button>
          ) : (
            <p key={i} className="lyric">
              {l.text}
            </p>
          )
        ) : null,
      )}
      <div className="lyrics-pad" />
    </div>
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
