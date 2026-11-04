import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { Check, CircleHelp, X, CircleAlert, CircleCheck, Info } from "lucide-react";
import { FLAG_LABEL, STATUS, fmtDelta, fmtDuration, tolerance, type Tone } from "./format";
import { useStore } from "./store";
import type { Candidate, CheckItem, LyricsInfo, TrackStatus, VersionFlag } from "./types";

/* ---------------- status ---------------- */

export function StatusBadge({ status }: { status: TrackStatus }) {
  const s = STATUS[status];
  return (
    <span className={`badge tone-${s.tone}`}>
      <span className="badge-dot" />
      {s.label}
    </span>
  );
}

export function Flags({ flags }: { flags: VersionFlag[] }) {
  if (!flags.length) return null;
  return (
    <>
      {flags.map((f) => (
        <span key={f} className="flag">
          {FLAG_LABEL[f]}
        </span>
      ))}
    </>
  );
}

/* ---------------- checks ---------------- */

const CHECK_NAME: Record<CheckItem["key"], string> = {
  title: "歌名",
  artist: "歌手",
  duration: "时长",
  version: "版本",
  quality: "音质",
  popularity: "热度",
};

export function CheckChips({ checks, only }: { checks: CheckItem[]; only?: CheckItem["key"][] }) {
  const list = only ? checks.filter((c) => only.includes(c.key)) : checks;
  return (
    <ul className="checks" aria-label="匹配检查">
      {list.map((c) => {
        const tone = c.ok === true ? "ok" : c.ok === false ? "bad" : "warn";
        const Icon = c.ok === true ? Check : c.ok === false ? X : CircleHelp;
        return (
          <li key={c.key} className={`check tone-${tone}`} title={`${CHECK_NAME[c.key]}：${c.label}`}>
            <Icon size={12} strokeWidth={2.5} aria-hidden />
            <span>{c.label}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** The blocking problems of a candidate, as one plain sentence for the review header. */
export function problemsOf(c: Candidate): CheckItem[] {
  return c.checks.filter((x) => x.ok !== true && x.key !== "quality" && x.key !== "popularity");
}

/* ---------------- duration ruler (signature element) ---------------- */

export function DurationRuler({
  target,
  candidates,
  highlight,
  onPick,
}: {
  target: number | null;
  candidates: Candidate[];
  highlight: number | null;
  onPick: (id: number) => void;
}) {
  const timed = candidates.filter((c) => c.duration != null);
  // Rejected candidates are collapsed in the table too; only plot them when nothing else is left.
  const viable = timed.filter((c) => c.verdict !== "reject");
  const withDur = viable.length ? viable : timed;
  if (!withDur.length) return null;
  const sorted = withDur.map((c) => c.duration!).sort((a, b) => a - b);
  const center = target ?? sorted[Math.floor(sorted.length / 2)];
  const span = 15;
  const min = center - span;
  const max = center + span;
  const pos = (d: number) => ((Math.min(Math.max(d, min), max) - min) / (max - min)) * 100;
  const tol = tolerance(center);

  // Stack overlapping dots, at most MAX_LANES high; the rest fold into a "+N" marker.
  const MAX_LANES = 3;
  const lanes: number[][] = [];
  const overflow = new Map<number, { x: number; n: number }>();
  const placed: { c: Candidate; x: number; lane: number; clipped: "left" | "right" | null }[] = [];
  for (const c of withDur.slice().sort((a, b) => a.rank - b.rank)) {
    const x = pos(c.duration!);
    let lane = 0;
    while (lane < MAX_LANES && lanes[lane]?.some((px) => Math.abs(px - x) < 3.2)) lane++;
    if (lane === MAX_LANES) {
      const anchor = lanes[MAX_LANES - 1].find((px) => Math.abs(px - x) < 3.2)!;
      const o = overflow.get(anchor) ?? { x: anchor, n: 0 };
      o.n++;
      overflow.set(anchor, o);
      continue;
    }
    (lanes[lane] ??= []).push(x);
    placed.push({ c, x, lane, clipped: c.duration! < min ? "left" : c.duration! > max ? "right" : null });
  }
  const height = 34 + Math.max(0, lanes.length - 1) * 22 + (overflow.size ? 16 : 0);

  return (
    <div className="ruler" aria-label="候选时长对比">
      <div className="ruler-track" style={{ height }}>
        {target != null && (
          <>
            <div className="ruler-band warn" style={{ left: `${pos(center - 10)}%`, width: `${pos(center + 10) - pos(center - 10)}%` }} />
            <div className="ruler-band ok" style={{ left: `${pos(center - tol)}%`, width: `${pos(center + tol) - pos(center - tol)}%` }} />
            <div className="ruler-target" style={{ left: "50%" }}>
              <span>{fmtDuration(target)}</span>
            </div>
          </>
        )}
        {placed.map(({ c, x, lane, clipped }) => (
          <button
            key={c.id}
            className={`ruler-dot v-${c.verdict}${highlight === c.id ? " is-hl" : ""}`}
            style={{ left: `${x}%`, bottom: 6 + lane * 22 }}
            onClick={() => onPick(c.id)}
            title={`#${c.rank} ${c.provider_label} ${fmtDuration(c.duration)}${target != null ? `（${fmtDelta(c.duration! - target)}）` : ""}`}
            aria-label={`候选 ${c.rank}，时长 ${fmtDuration(c.duration)}`}
          >
            {clipped === "left" ? "‹" : clipped === "right" ? "›" : c.rank}
          </button>
        ))}
        {[...overflow.values()].map((o) => (
          <span key={o.x} className="ruler-more mono" style={{ left: `${o.x}%`, bottom: 6 + MAX_LANES * 22 }}>
            +{o.n}
          </span>
        ))}
      </div>
      <div className="ruler-scale mono">
        <span>{fmtDelta(-span)}</span>
        <span>{target != null ? `清单时长 ${fmtDuration(target)} · 绿区 ±${tol}s 内算一致` : "清单未写时长，按候选中位数居中"}</span>
        <span>{fmtDelta(span)}</span>
      </div>
    </div>
  );
}

/* ---------------- toasts ---------------- */

interface Toast {
  id: number;
  tone: "ok" | "bad" | "info";
  text: string;
}
const ToastCtx = createContext<(tone: Toast["tone"], text: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs.slice(-3), { id, tone, text }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "bad" ? 6000 : 3200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => {
          const Icon = t.tone === "ok" ? CircleCheck : t.tone === "bad" ? CircleAlert : Info;
          return (
            <div key={t.id} className={`toast tone-${t.tone}`}>
              <Icon size={16} aria-hidden />
              <span>{t.text}</span>
            </div>
          );
        })}
      </div>
    </ToastCtx.Provider>
  );
}

export function toneOfScore(score: number | null): Tone {
  if (score == null) return "idle";
  if (score >= 90) return "ok";
  if (score >= 50) return "warn";
  return "bad";
}

/** Small tag telling whether a downloaded song has lyrics next to it. */
export function LyricsTag({ info }: { info: LyricsInfo | null | undefined }) {
  if (!info) return null;
  if (info.error || (!info.path && !info.embedded)) {
    return (
      <span className="lyr-tag is-missing" title={info.error ?? "没有找到歌词"}>
        无歌词
      </span>
    );
  }
  const kind = [info.synced ? "逐句同步" : "纯文本", info.translated ? "含翻译" : null, info.embedded ? "已内嵌" : null].filter(Boolean).join(" · ");
  return (
    <span className="lyr-tag" title={`歌词来自${info.source_label ?? "未知来源"}：${kind}${info.path ? `\n${info.path}` : ""}`}>
      {info.synced ? "LRC" : "词"}
      {info.translated ? <i>译</i> : null}
    </span>
  );
}

/** "LRC" switch: lit means downloads also save the matching .lrc lyrics (the global setting). */
/**
 * Per-song LRC switch: lit = this song's lyrics get saved as .lrc next to it.
 * Before download it is a choice; after download it shows whether lyrics exist and, when
 * they don't, clicking fetches them.
 */
export function LrcSwitch({ on, busy = false, onToggle, title }: { on: boolean; busy?: boolean; onToggle: () => void; title: string }) {
  return (
    <button
      type="button"
      className={`lrc-toggle is-compact${on ? " is-on" : ""}${busy ? " is-busy" : ""}`}
      aria-pressed={on}
      disabled={busy}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      title={title}
    >
      <span className="lrc-toggle-dot" aria-hidden />
      LRC
    </button>
  );
}

/** The global "save lyrics" default, used until a song gets its own choice. */
export function useLyricsDefault(): boolean {
  return useStore((s) => s.lyricsPref) ?? true;
}
