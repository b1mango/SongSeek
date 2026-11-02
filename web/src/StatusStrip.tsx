import { STATUS, STATUS_ORDER } from "./format";
import type { Playlist, TrackStatus } from "./types";

export type Filter = TrackStatus | "all" | "active";

const ACTIVE: TrackStatus[] = ["searching", "queued", "downloading"];

/** Segmented bar + legend. Legend items double as filters when onFilter is given. */
export function StatusStrip({
  counts,
  filter,
  onFilter,
  compact,
}: {
  counts: Playlist["counts"];
  filter?: Filter;
  onFilter?: (f: Filter) => void;
  compact?: boolean;
}) {
  const active = ACTIVE.reduce((n, s) => n + (counts[s] ?? 0), 0);
  type Seg = { key: Filter; label: string; tone: string; n: number };
  const segs: Seg[] = [
    ...STATUS_ORDER.map((s): Seg => ({ key: s, label: STATUS[s].label, tone: STATUS[s].tone, n: counts[s] ?? 0 })),
    { key: "active", label: "进行中", tone: "busy", n: active } satisfies Seg,
  ].filter((s) => s.n > 0);
  const total = counts.total || 1;

  return (
    <div className={`strip${compact ? " is-compact" : ""}`}>
      <div className="strip-bar" role="img" aria-label="状态分布">
        {segs.map((s) => (
          <span key={s.key} className={`strip-seg tone-${s.tone}`} style={{ flexGrow: s.n / total }} />
        ))}
      </div>
      {!compact && (
        <div className="strip-legend">
          {onFilter && (
            <button className={`legend-item${filter === "all" ? " is-on" : ""}`} onClick={() => onFilter("all")}>
              全部 <b className="mono">{counts.total}</b>
            </button>
          )}
          {segs.map((s) =>
            onFilter ? (
              <button key={s.key} className={`legend-item tone-${s.tone}${filter === s.key ? " is-on" : ""}`} onClick={() => onFilter(s.key)}>
                <span className="legend-dot" />
                {s.label} <b className="mono">{s.n}</b>
              </button>
            ) : (
              <span key={s.key} className={`legend-item tone-${s.tone}`}>
                <span className="legend-dot" />
                {s.label} <b className="mono">{s.n}</b>
              </span>
            ),
          )}
        </div>
      )}
    </div>
  );
}

export function matchesFilter(status: TrackStatus, f: Filter) {
  if (f === "all") return true;
  if (f === "active") return ACTIVE.includes(status);
  return status === f;
}
