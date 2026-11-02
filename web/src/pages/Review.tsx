import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  Crown,
  ExternalLink,
  Keyboard,
  ListChecks,
  LoaderCircle,
  Pencil,
  Search,
  SkipForward,
  Undo2,
} from "lucide-react";
import { api } from "../api";
import { CheckChips, DurationRuler, Flags, StatusBadge, problemsOf, toneOfScore, useToast } from "../components";
import { fmtDelta, fmtDuration, tolerance } from "../format";
import { useFlipList } from "../motion";
import { PlayButton } from "../player";
import { href } from "../router";
import { onEvent, upsertPlaylist, useStore } from "../store";
import type { Candidate, Track } from "../types";

type QueueMode = "review" | "all";

export function ReviewPage({ id, focus }: { id: number; focus?: number }) {
  const toast = useToast();
  const playlist = useStore((s) => s.playlists.find((p) => p.id === id));
  const [tracks, setTracks] = useState<Track[] | null>(null);
  // Tracks that needed review when the page opened stay in the queue after being resolved,
  // so the reviewer can see their progress and go back.
  const [sessionIds, setSessionIds] = useState<Set<number>>(new Set());
  const [mode, setMode] = useState<QueueMode>("review");
  const [currentId, setCurrentId] = useState<number | null>(focus ?? null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [cands, setCands] = useState<Record<number, Candidate[]>>({});

  useEffect(() => {
    api
      .playlist(id)
      .then(({ playlist, tracks }) => {
        upsertPlaylist(playlist);
        setTracks(tracks);
        const ids = new Set(tracks.filter((t) => t.status === "needs_review").map((t) => t.id));
        if (focus) ids.add(focus);
        setSessionIds(ids);
        if (ids.size === 0) setMode("all");
        setCurrentId((cur) => cur ?? tracks.find((t) => ids.has(t.id))?.id ?? tracks.find((t) => t.candidate_count > 0)?.id ?? null);
      })
      .catch((e) => toast("bad", e.message));
    return onEvent("track", (t) => {
      if (t.playlist_id !== id) return;
      setTracks((xs) => (xs ? xs.map((x) => (x.id === t.id ? t : x)) : xs));
      if (t.status === "needs_review") setSessionIds((s) => (s.has(t.id) ? s : new Set(s).add(t.id)));
    });
  }, [id, focus, toast]);

  const queue = useMemo(() => {
    if (!tracks) return [];
    return mode === "review" ? tracks.filter((t) => sessionIds.has(t.id)) : tracks.filter((t) => t.candidate_count > 0 || t.status === "not_found");
  }, [tracks, mode, sessionIds]);

  const current = tracks?.find((t) => t.id === currentId) ?? null;
  const remaining = tracks?.filter((t) => t.status === "needs_review").length ?? 0;

  const loadCandidates = useCallback(
    (trackId: number, force = false) => {
      if (!force && cands[trackId]) return;
      api
        .candidates(trackId)
        .then((list) => setCands((m) => ({ ...m, [trackId]: list })))
        .catch((e) => toast("bad", e.message));
    },
    [cands, toast],
  );

  useEffect(() => {
    if (currentId != null) loadCandidates(currentId);
  }, [currentId, loadCandidates]);

  const select = useCallback(
    (tid: number) => {
      setCurrentId(tid);
      history.replaceState(null, "", href({ page: "review", id, track: tid }));
    },
    [id],
  );

  const step = useCallback(
    (dir: 1 | -1, onlyUnresolved = false) => {
      if (!queue.length) return;
      const idx = queue.findIndex((t) => t.id === currentId);
      for (let i = 1; i <= queue.length; i++) {
        const t = queue[(idx + dir * i + queue.length) % queue.length];
        if (!onlyUnresolved || t.status === "needs_review") return select(t.id);
      }
    },
    [queue, currentId, select],
  );

  const replaceTrack = (t: Track) => setTracks((xs) => xs!.map((x) => (x.id === t.id ? t : x)));

  async function choose(c: Candidate, advance = true) {
    try {
      replaceTrack(await api.choose(c.track_id, c.id));
      if (advance) step(1, true);
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  async function skip(t: Track) {
    try {
      const { updated } = await api.batch([t.id], t.status === "skipped" ? "unskip" : "skip");
      updated.forEach(replaceTrack);
      if (t.status !== "skipped") step(1, true);
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  async function batch(action: "accept_best" | "skip") {
    const ids = [...checked];
    try {
      const { updated } = await api.batch(ids, action);
      updated.forEach(replaceTrack);
      setChecked(new Set());
      toast("ok", action === "accept_best" ? `已通过 ${updated.length} 首的最佳候选` : `已跳过 ${updated.length} 首`);
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  // Keyboard: J/K next/prev, 1-9 choose, Enter choose best, S skip, Space play best.
  const list = current ? cands[current.id] : undefined;
  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyRef.current = (e: KeyboardEvent) => {
    const tag = (e.target as HTMLElement).tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "j" || e.key === "ArrowDown") (e.preventDefault(), step(1));
    else if (e.key === "k" || e.key === "ArrowUp") (e.preventDefault(), step(-1));
    else if (e.key === "s" && current) skip(current);
    else if (e.key === "Enter" && list) {
      const best = list.find((c) => c.verdict !== "reject");
      if (best) choose(best);
    } else if (/^[1-9]$/.test(e.key) && list) {
      const c = list.find((x) => x.rank === Number(e.key));
      if (c) choose(c);
    }
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => keyRef.current(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  if (!tracks) {
    return (
      <div className="page center-fill">
        <LoaderCircle className="spin" size={22} />
      </div>
    );
  }

  const allChecked = queue.length > 0 && queue.every((t) => checked.has(t.id));

  return (
    <div className="review">
      <aside className="review-queue">
        <a className="back" href={href({ page: "playlist", id })}>
          <ArrowLeft size={15} /> {playlist?.name ?? "返回清单"}
        </a>
        <div className="queue-head">
          <div>
            <h2>审核</h2>
            <p className={remaining ? "tone-text-warn" : "tone-text-ok"}>{remaining ? `还有 ${remaining} 首待审核` : "全部审核完成"}</p>
          </div>
          <div className="seg" role="radiogroup" aria-label="显示范围">
            <button role="radio" aria-checked={mode === "review"} className={mode === "review" ? "is-on" : ""} onClick={() => setMode("review")}>
              待审核
            </button>
            <button role="radio" aria-checked={mode === "all"} className={mode === "all" ? "is-on" : ""} onClick={() => setMode("all")}>
              全部
            </button>
          </div>
        </div>

        <div className="queue-batch">
          <label className="row gap-8 small">
            <input
              type="checkbox"
              checked={allChecked}
              onChange={() => setChecked(allChecked ? new Set() : new Set(queue.map((t) => t.id)))}
              aria-label="全选"
            />
            {checked.size ? `已选 ${checked.size}` : "全选"}
          </label>
          <div className="row gap-6">
            <button className="btn btn-sm" disabled={!checked.size} onClick={() => batch("accept_best")} title="把所选歌曲的最佳候选全部选定">
              <ListChecks size={14} /> 通过最佳
            </button>
            <button className="btn btn-sm" disabled={!checked.size} onClick={() => batch("skip")}>
              <SkipForward size={14} /> 跳过
            </button>
          </div>
        </div>

        <ul className="queue-list">
          {queue.map((t) => (
            <li key={t.id}>
              <div className={`queue-item${t.id === currentId ? " is-current" : ""} st-${t.status}`} onClick={() => select(t.id)}>
                <input
                  type="checkbox"
                  checked={checked.has(t.id)}
                  onClick={(e) => e.stopPropagation()}
                  onChange={() =>
                    setChecked((s) => {
                      const n = new Set(s);
                      if (n.has(t.id)) n.delete(t.id);
                      else n.add(t.id);
                      return n;
                    })
                  }
                  aria-label={`选择 ${t.title}`}
                />
                <div className="queue-text">
                  <span className="queue-title">{t.title}</span>
                  <span className="queue-artist">{t.artist || "未写歌手"}</span>
                </div>
                <QueueMark t={t} />
              </div>
            </li>
          ))}
          {queue.length === 0 && <li className="muted small pad">没有需要审核的歌曲</li>}
        </ul>
      </aside>

      <section className="review-main">
        {current ? (
          <Focus
            key={current.id}
            track={current}
            candidates={list}
            onChoose={choose}
            onSkip={() => skip(current)}
            onPrev={() => step(-1)}
            onNext={() => step(1)}
            onTrack={replaceTrack}
            onCandidates={(l) => setCands((m) => ({ ...m, [current.id]: l }))}
          />
        ) : (
          <div className="center-fill muted">从左侧选一首歌开始</div>
        )}
      </section>
    </div>
  );
}

function QueueMark({ t }: { t: Track }) {
  if (t.status === "needs_review")
    return (
      <span className={`score tone-${toneOfScore(t.best_score)}`} title="最佳候选得分">
        {t.best_score != null ? Math.round(t.best_score) : "—"}
      </span>
    );
  if (t.status === "matched" || t.status === "done" || t.status === "queued" || t.status === "downloading")
    return <CircleCheck className="tone-text-ok" size={16} aria-label="已选定" />;
  if (t.status === "skipped") return <span className="muted small">已跳过</span>;
  return <StatusBadge status={t.status} />;
}

/* ------------------------------------------------------------------ */

function Focus({
  track: t,
  candidates,
  onChoose,
  onSkip,
  onPrev,
  onNext,
  onTrack,
  onCandidates,
}: {
  track: Track;
  candidates: Candidate[] | undefined;
  onChoose: (c: Candidate, advance?: boolean) => void;
  onSkip: () => void;
  onPrev: () => void;
  onNext: () => void;
  onTrack: (t: Track) => void;
  onCandidates: (l: Candidate[]) => void;
}) {
  const toast = useToast();
  const [hl, setHl] = useState<number | null>(null);
  const [showRejected, setShowRejected] = useState(false);
  const [query, setQuery] = useState(`${t.title} ${t.artist}`.trim());
  const [searching, setSearching] = useState(false);
  const [editing, setEditing] = useState(false);
  const rowRefs = useRef<Record<number, HTMLTableRowElement | null>>({});
  const body = useRef<HTMLTableSectionElement | null>(null);

  const viable = candidates?.filter((c) => c.verdict !== "reject") ?? [];
  const rejected = candidates?.filter((c) => c.verdict === "reject") ?? [];
  useFlipList(body, (candidates ?? []).map((c) => c.id).join(",") + (showRejected ? "+r" : ""), t.id);
  const best = viable[0] ?? null;
  const chosen = candidates?.find((c) => c.id === t.chosen_candidate_id) ?? null;

  async function research(q = query) {
    setSearching(true);
    try {
      const r = await api.researchTrack(t.id, q);
      onTrack(r.track);
      onCandidates(r.candidates);
      const fresh = r.candidates.filter((c) => c.verdict !== "reject").length;
      toast(fresh ? "ok" : "info", fresh ? `找到 ${fresh} 个可用候选` : "没有新的可用候选，看看被排除的或换个关键词");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setSearching(false);
    }
  }

  const pick = (cid: number) => {
    setHl(cid);
    const row = rowRefs.current[cid];
    if (!row) setShowRejected(true);
    requestAnimationFrame(() => rowRefs.current[cid]?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
  };

  return (
    <div className="focus">
      {/* What the list asks for */}
      <div className="want">
        <div className="want-label">清单要求</div>
        {editing ? (
          <EditWant
            t={t}
            onCancel={() => setEditing(false)}
            onSave={async (patch) => {
              try {
                const nt = await api.updateTrack(t.id, patch);
                onTrack(nt);
                setEditing(false);
                const q = `${nt.title} ${nt.artist}`.trim();
                setQuery(q);
                research(q);
              } catch (e) {
                toast("bad", (e as Error).message);
              }
            }}
          />
        ) : (
          <div className="want-body">
            <div className="want-title">
              {t.title} <Flags flags={t.flags} />
            </div>
            <div className="want-meta">
              <span>{t.artist || <i className="muted">未写歌手</i>}</span>
              {t.album && <span className="muted">《{t.album}》</span>}
              <span className="mono">{t.duration != null ? fmtDuration(t.duration) : <i className="muted">未写时长</i>}</span>
              <button className="link-btn" onClick={() => setEditing(true)}>
                <Pencil size={12} /> 修改
              </button>
            </div>
          </div>
        )}
        <div className="want-nav">
          <span className="mono muted small">#{t.position}</span>
          <button className="icon-btn" onClick={onPrev} aria-label="上一首" title="上一首 (K)">
            <ChevronLeft size={18} />
          </button>
          <button className="icon-btn" onClick={onNext} aria-label="下一首" title="下一首 (J)">
            <ChevronRight size={18} />
          </button>
        </div>
      </div>

      <Verdict t={t} best={best} chosen={chosen} loaded={!!candidates} />

      {candidates && candidates.length > 0 && <DurationRuler target={t.duration} candidates={candidates} highlight={hl ?? chosen?.id ?? null} onPick={pick} />}

      {!candidates ? (
        <div className="center-fill pad">
          <LoaderCircle className="spin" size={20} />
        </div>
      ) : (
        <div className="cands card flush">
          <table className="table cand-table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th aria-label="试听" />
                <th>候选</th>
                <th>来源</th>
                <th className="num">时长</th>
                <th>检查</th>
                <th className="num">分数</th>
                <th aria-label="选择" />
              </tr>
            </thead>
            <tbody ref={body}>
              {viable.map((c) => (
                <CandRow key={c.id} c={c} t={t} chosen={c.id === t.chosen_candidate_id} hl={hl === c.id} rowRef={(el) => (rowRefs.current[c.id] = el)} onChoose={onChoose} onHover={setHl} />
              ))}
              {viable.length === 0 && (
                <tr>
                  <td colSpan={8} className="empty-row muted">
                    没有可用的候选。{rejected.length ? "下面是被排除的结果，必要时也可以手动选。" : "换个关键词再搜一次试试。"}
                  </td>
                </tr>
              )}
              {rejected.length > 0 && (
                <tr className="rejected-toggle">
                  <td colSpan={8}>
                    <button className="link-btn" onClick={() => setShowRejected((v) => !v)}>
                      <ChevronDown size={14} className={showRejected ? "rot-180" : ""} />
                      {showRejected ? "收起" : "展开"}已排除的 {rejected.length} 个（版本或时长明显不符）
                    </button>
                  </td>
                </tr>
              )}
              {showRejected &&
                rejected.map((c) => (
                  <CandRow key={c.id} c={c} t={t} chosen={c.id === t.chosen_candidate_id} hl={hl === c.id} rowRef={(el) => (rowRefs.current[c.id] = el)} onChoose={onChoose} onHover={setHl} />
                ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="focus-foot">
        <form
          className="research"
          onSubmit={(e) => {
            e.preventDefault();
            if (query.trim()) research();
          }}
        >
          <Search size={15} aria-hidden />
          <input className="input bare" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="搜索关键词" />
          <button className="btn btn-sm" disabled={searching || !query.trim()}>
            {searching && <LoaderCircle className="spin" size={14} />} 换关键词再搜
          </button>
        </form>
        <div className="row gap-8">
          <button className="btn" onClick={onSkip}>
            {t.status === "skipped" ? (
              <>
                <Undo2 size={15} /> 取消跳过
              </>
            ) : (
              <>
                <SkipForward size={15} /> 跳过这首 <kbd>S</kbd>
              </>
            )}
          </button>
          {best && (
            <button className="btn btn-primary" onClick={() => onChoose(best)}>
              <Check size={15} /> 选最佳并下一首 <kbd>↵</kbd>
            </button>
          )}
        </div>
      </div>
      <div className="kbd-hint muted small">
        <Keyboard size={13} aria-hidden /> <kbd>J</kbd>/<kbd>K</kbd> 切换歌曲 · <kbd>1</kbd>–<kbd>9</kbd> 选对应候选 · <kbd>↵</kbd> 选最佳 · <kbd>S</kbd> 跳过
      </div>
    </div>
  );
}

function Verdict({ t, best, chosen, loaded }: { t: Track; best: Candidate | null; chosen: Candidate | null; loaded: boolean }) {
  if (!loaded) return null;
  if (chosen) {
    return (
      <div className="verdict tone-ok">
        <CircleCheck size={18} aria-hidden />
        <div>
          <b>
            已选定 #{chosen.rank} {chosen.provider_label} · {chosen.quality.label}
          </b>
          <span>{t.chosen_by === "auto" ? "系统自动匹配，各项检查都通过" : "由你手动选定"}；下载时若失败，会自动改用其他匹配的候选。</span>
        </div>
      </div>
    );
  }
  if (t.status === "skipped") {
    return (
      <div className="verdict tone-idle">
        <SkipForward size={18} aria-hidden />
        <div>
          <b>已跳过</b>
          <span>这首不会被下载。</span>
        </div>
      </div>
    );
  }
  if (!best) {
    return (
      <div className="verdict tone-bad">
        <Search size={18} aria-hidden />
        <div>
          <b>没有找到可用的版本</b>
          <span>可以修改歌名或歌手后重新搜索，或者跳过。</span>
        </div>
      </div>
    );
  }
  const problems = problemsOf(best);
  return (
    <div className="verdict tone-warn">
      <ListChecks size={18} aria-hidden />
      <div>
        <b>为什么要你确认</b>
        <span>
          最佳候选 #{best.rank}（{best.provider_label}）：{problems.length ? problems.map((p) => p.label).join("；") : "多个候选分数接近"}
        </span>
      </div>
    </div>
  );
}

function CandRow({
  c,
  t,
  chosen,
  hl,
  rowRef,
  onChoose,
  onHover,
}: {
  c: Candidate;
  t: Track;
  chosen: boolean;
  hl: boolean;
  rowRef: (el: HTMLTableRowElement | null) => void;
  onChoose: (c: Candidate, advance?: boolean) => void;
  onHover: (id: number | null) => void;
}) {
  const delta = c.duration != null && t.duration != null ? c.duration - t.duration : null;
  const deltaTone = delta == null ? "idle" : Math.abs(delta) <= tolerance(t.duration!) ? "ok" : Math.abs(delta) <= 10 ? "warn" : "bad";
  return (
    <tr ref={rowRef} data-flip={c.id} className={`cand v-${c.verdict}${chosen ? " is-chosen" : ""}${hl ? " is-hl" : ""}`} onMouseEnter={() => onHover(c.id)} onMouseLeave={() => onHover(null)}>
      <td className="num">
        <span className={`rank v-${c.verdict}`}>{c.rank}</span>
      </td>
      <td>
        <PlayButton playable={{ key: `c:${c.id}`, url: c.preview_url, title: c.title, subtitle: c.needs_member ? `${c.provider_label}需会员 · 已换其他音源同版本试听` : `${c.artist} · ${c.provider_label} 试听`, lyricsUrl: api.candidateLyricsUrl(c.id), coverUrl: api.candidateCoverUrl(c.id) }} />
      </td>
      <td>
        <div className="song">
          <span className="song-title">
            {c.title} <Flags flags={c.flags} />
          </span>
          <span className="song-artist">
            {c.artist}
            {c.album && <span className="muted"> · 《{c.album}》</span>}
          </span>
        </div>
      </td>
      <td>
        <div className="src">
          <span>
            {c.provider_label}
            {c.needs_member && (
              <span className="member-tag" title="这个音源需要会员才能播放；试听和下载会自动改用其他音源的同一版本">
                <Crown size={11} aria-hidden /> 会员
              </span>
            )}
          </span>
          <span className="mono small muted">
            {c.quality.label}
            {c.web_url && (
              <a href={c.web_url} target="_blank" rel="noreferrer" className="ext" aria-label="在平台打开" title="在平台打开">
                <ExternalLink size={11} />
              </a>
            )}
          </span>
        </div>
      </td>
      <td className="num">
        <div className="dur">
          <span className="mono">{fmtDuration(c.duration)}</span>
          {delta != null && <span className={`mono small tone-text-${deltaTone}`}>{fmtDelta(delta)}</span>}
        </div>
      </td>
      <td>
        <CheckChips checks={c.checks} only={["title", "artist", "version"]} />
      </td>
      <td className="num">
        <span className={`score tone-${c.verdict === "match" ? "ok" : c.verdict === "review" ? "warn" : "bad"}`}>{Math.round(c.score)}</span>
      </td>
      <td className="num">
        {chosen ? (
          <span className="chosen-tag">
            <Check size={14} /> 已选
          </span>
        ) : (
          <button className={`btn btn-sm${c.verdict === "reject" ? "" : " btn-choose"}`} onClick={() => onChoose(c)} title={c.rank <= 9 ? `快捷键 ${c.rank}` : undefined}>
            选这个
          </button>
        )}
      </td>
    </tr>
  );
}

function EditWant({ t, onSave, onCancel }: { t: Track; onSave: (p: { title: string; artist: string; duration: number | null }) => void; onCancel: () => void }) {
  const [title, setTitle] = useState(t.title);
  const [artist, setArtist] = useState(t.artist);
  const [dur, setDur] = useState(t.duration != null ? fmtDuration(t.duration) : "");
  const parseDur = (s: string): number | null => {
    const v = s.trim();
    if (!v) return null;
    const m = v.match(/^(\d+):(\d{1,2})$/);
    if (m) return Number(m[1]) * 60 + Number(m[2]);
    const n = Number(v);
    return isFinite(n) ? n : null;
  };
  return (
    <form
      className="want-edit"
      onSubmit={(e) => {
        e.preventDefault();
        onSave({ title: title.trim(), artist: artist.trim(), duration: parseDur(dur) });
      }}
    >
      <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="歌名" placeholder="歌名" autoFocus />
      <input className="input" value={artist} onChange={(e) => setArtist(e.target.value)} aria-label="歌手" placeholder="歌手" />
      <input className="input mono w-90" value={dur} onChange={(e) => setDur(e.target.value)} aria-label="时长" placeholder="3:45" />
      <button className="btn btn-primary btn-sm" disabled={!title.trim()}>
        保存并重搜
      </button>
      <button type="button" className="btn btn-sm btn-ghost" onClick={onCancel}>
        取消
      </button>
    </form>
  );
}

