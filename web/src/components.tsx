import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { Check, CircleHelp, X, CircleAlert, CircleCheck, Info } from "lucide-react";
import { FLAG_LABEL, STATUS, fmtDelta, fmtDuration, tolerance, type Tone } from "./format";
import type { Candidate, CheckItem, TrackStatus, VersionFlag } from "./types";

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
  const withDur = candidates.filter((c) => c.duration != null);
  if (!withDur.length) return null;
  const sorted = withDur.map((c) => c.duration!).sort((a, b) => a - b);
  const center = target ?? sorted[Math.floor(sorted.length / 2)];
  const span = 15;
  const min = center - span;
  const max = center + span;
  const pos = (d: number) => ((Math.min(Math.max(d, min), max) - min) / (max - min)) * 100;
  const tol = tolerance(center);

  // Stack dots that would overlap.
  const lanes: number[][] = [];
  const placed = withDur
    .slice()
    .sort((a, b) => a.rank - b.rank)
    .map((c) => {
      const x = pos(c.duration!);
      let lane = 0;
      while (lanes[lane]?.some((px) => Math.abs(px - x) < 3.2)) lane++;
      (lanes[lane] ??= []).push(x);
      return { c, x, lane, clipped: c.duration! < min ? "left" : c.duration! > max ? "right" : null };
    });

  return (
    <div className="ruler" aria-label="候选时长对比">
      <div className="ruler-track" style={{ height: 34 + Math.max(0, lanes.length - 1) * 22 }}>
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
  if (score >= 80) return "ok";
  if (score >= 50) return "warn";
  return "bad";
}
