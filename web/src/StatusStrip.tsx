import { STATUS, STATUS_ORDER } from "./format";
import type { Playlist, TrackStatus } from "./types";

export type Filter = TrackStatus | "all" | "active";

const ACTIVE: TrackStatus[] = ["searching", "queued", "downloading"];

type Counts = Playlist["counts"];
type Seg = { key: Filter; label: string; tone: string; n: number };

function segments(counts: Counts): Seg[] {
  const active = ACTIVE.reduce((n, s) => n + (counts[s] ?? 0), 0);
  return [
    ...STATUS_ORDER.map((s): Seg => ({
      key: s,
      label: STATUS[s].label,
      tone: STATUS[s].tone,
      n: counts[s] ?? 0,
    })),
    { key: "active", label: "进行中", tone: "busy", n: active } satisfies Seg,
  ].filter((s) => s.n > 0);
}

/** Thin segmented bar of the status distribution. */
export function StatusBar({ counts, compact }: { counts: Counts; compact?: boolean }) {
  const total = counts.total || 1;
  return (
    <div className={`strip-bar${compact ? " is-compact" : ""}`} role="img" aria-label="状态分布">
      {segments(counts).map((s) => (
        <span key={s.key} className={`strip-seg tone-${s.tone}`} style={{ flexGrow: s.n / total }} />
      ))}
    </div>
  );
}

/** Status pills that double as filters. */
export function StatusFilters({ counts, filter, onFilter }: { counts: Counts; filter: Filter; onFilter: (f: Filter) => void }) {
  return (
    <div className="strip-legend" role="group" aria-label="按状态筛选">
      <button className={`legend-item${filter === "all" ? " is-on" : ""}`} onClick={() => onFilter("all")}>
        全部 <b className="mono">{counts.total}</b>
      </button>
      {segments(counts).map((s) => (
        <button key={s.key} className={`legend-item tone-${s.tone}${filter === s.key ? " is-on" : ""}`} onClick={() => onFilter(s.key)}>
          <span className="legend-dot" />
          {s.label} <b className="mono">{s.n}</b>
        </button>
      ))}
    </div>
  );
}

export function matchesFilter(status: TrackStatus, f: Filter) {
  if (f === "all") return true;
  if (f === "active") return ACTIVE.includes(status);
  return status === f;
}
