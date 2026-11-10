import { useEffect, useMemo, useRef, useState } from "react";
import {
  CircleAlert,
  Download,
  FileDown,
  FolderOpen,
  ListChecks,
  LoaderCircle,
  Pencil,
  Plus,
  ScrollText,
  RotateCcw,
  Search,
  SkipForward,
  Trash2,
  Undo2,
} from "lucide-react";
import { api, ApiError } from "../api";
import { Flags, isTyping, LrcSwitch, LyricsTag, StatusBadge, toneOfScore, useLyricsDefault, useToast } from "../components";
import { fmtDuration } from "../format";
import { PlayButton } from "../player";
import { href, navigate, subscribeJobFocus } from "../router";
import { FlowLabel, RowFlow } from "../RowFlow";
import { StatusBar, StatusFilters, matchesFilter, type Filter } from "../StatusStrip";
import { getPlaylistRevision, isPlaylistDeleted, isTrackDeleted, onEvent, removePlaylist, removeTrack, upsertJob, upsertPlaylist, useStore } from "../store";
import type { BatchAction, Playlist, Track, TrackStatus } from "../types";

export function PlaylistPage({ id }: { id: number }) {
  const toast = useToast();
  const [tracks, setTracks] = useState<Track[] | null>(null);
  const [fallback, setFallback] = useState<Playlist | null>(null);
  const playlist = useStore((s) => s.playlists.find((p) => p.id === id)) ?? (isPlaylistDeleted(id) ? null : fallback);
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [adding, setAdding] = useState(false);
  const anchor = useRef<number | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isTyping(e.target)) setSelected(new Set());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    let active = true;
    let request = 0;
    let trackEvents = 0;
    const load = () => {
      const currentRequest = ++request;
      const initialTrackEvents = trackEvents;
      const initialPlaylistRevision = getPlaylistRevision(id);
      api.playlist(id).then(({ playlist, tracks }) => {
        if (!active || currentRequest !== request || isPlaylistDeleted(id)) return;
        // A snapshot taken before a live track update must not overwrite it.
        if (trackEvents !== initialTrackEvents) return load();
        if (getPlaylistRevision(id) === initialPlaylistRevision) {
          setFallback(playlist);
          upsertPlaylist(playlist);
        }
        setTracks(tracks.filter((track) => !isTrackDeleted(track.id, id)));
      }).catch((e) => {
        if (active && currentRequest === request && !isPlaylistDeleted(id)) toast("bad", e.message);
      });
    };
    const offTrack = onEvent("track", (t) => {
      if (t.playlist_id !== id || isTrackDeleted(t.id, id)) return;
      trackEvents++;
      // Unknown ids are songs appended elsewhere (another window, or before our POST returned).
      setTracks((xs) =>
        !xs ? xs : xs.some((x) => x.id === t.id) ? xs.map((x) => (x.id === t.id ? t : x)) : [...xs, t].sort((a, b) => a.position - b.position),
      );
    });
    const offTrackDeleted = onEvent("track_deleted", (t) => {
      if (t.playlist_id !== id) return;
      trackEvents++;
      setTracks((xs) => xs?.filter((x) => x.id !== t.id) ?? null);
      setSelected((sel) => {
        if (!sel.has(t.id)) return sel;
        const next = new Set(sel);
        next.delete(t.id);
        return next;
      });
      if (anchor.current === t.id) anchor.current = null;
    });
    const offResync = onEvent("resync", load);
    load();
    return () => {
      active = false;
      offTrack();
      offTrackDeleted();
      offResync();
    };
  }, [id, toast]);

  // Sidebar job card clicked: show the track being worked on, even if the current filter hides it.
  const [flash, setFlash] = useState<number | null>(null);
  useEffect(() => {
    if (!tracks) return;
    return subscribeJobFocus(id, () => {
      const target = tracks.find((t) => t.status === "downloading" || t.status === "searching") ?? tracks.find((t) => t.status === "queued");
      if (!target) return;
      const needle = q.trim().toLowerCase();
      if (!matchesFilter(target.status, filter) || (needle && !`${target.title} ${target.artist} ${target.album}`.toLowerCase().includes(needle))) {
        setFilter("all");
        setQ("");
      }
      setFlash(target.id);
      requestAnimationFrame(() =>
        requestAnimationFrame(() => document.querySelector(`[data-track="${target.id}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" })),
      );
    });
  }, [id, tracks, filter, q]);
  useEffect(() => {
    if (flash === null) return;
    const timer = setTimeout(() => setFlash(null), 1800);
    return () => clearTimeout(timer);
  }, [flash]);

  const visible = useMemo(() => {
    if (!tracks) return [];
    const needle = q.trim().toLowerCase();
    return tracks.filter((t) => matchesFilter(t.status, filter) && (!needle || `${t.title} ${t.artist} ${t.album}`.toLowerCase().includes(needle)));
  }, [tracks, filter, q]);

  if (!playlist || !tracks) {
    return (
      <div className="page center-fill">
        <LoaderCircle className="spin" size={22} />
      </div>
    );
  }

  const c = playlist.counts;
  const selectedIds = [...selected].filter((sid) => tracks.some((t) => t.id === sid));

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    try {
      await fn();
    } catch (e) {
      // "nothing to do" is an explanation, not a failure
      toast(e instanceof ApiError && e.code === "no_eligible_tracks" ? "info" : "bad", (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const startSearch = (ids?: number[]) =>
    run("search", async () => {
      upsertJob(await api.searchPlaylist(id, ids));
      toast("info", "已开始搜索，结果会实时出现在下面");
    });
  const startDownload = (ids?: number[]) =>
    run("download", async () => {
      upsertJob(await api.downloadPlaylist(id, ids));
      toast("info", "已开始下载");
    });
  // One button for the whole list: reveals a selected downloaded song if any, else the first one (same folder).
  const openFolder = () => {
    const target = tracks.find((t) => t.file && !t.file_missing && selected.has(t.id)) ?? tracks.find((t) => t.file && !t.file_missing);
    if (target) api.reveal(target.id).catch((e) => toast("bad", e.message));
  };
  const fillLyrics = () =>
    run("lyrics", async () => {
      upsertJob(await api.playlistLyrics(id));
      toast("info", "正在给已下载的歌补歌词，已有歌词的会跳过");
    });
  const batch = (action: BatchAction, ids: number[]) =>
    run(action, async () => {
      const { updated, skipped = [] } = await api.batch(ids, action);
      const map = new Map(updated.map((t) => [t.id, t]));
      setTracks((xs) => xs!.map((x) => map.get(x.id) ?? x));
      setSelected(new Set());
      if (skipped.length) toast("info", `${skipped.length} 首未处理：${skipped[0].reason}`);
      return updated;
    });

  // Appends pasted songs, then searches just those so the rest of the list is left alone.
  const addSongs = (text: string) =>
    run("add", async () => {
      const { rows } = await api.parse({ kind: "text", text });
      if (!rows.length) return toast("info", "没认出歌曲，每行写一首，例如「晴天 - 周杰伦」");
      const { added, duplicates } = await api.addTracks(id, rows);
      const dup = duplicates ? `，${duplicates} 首清单里已经有了` : "";
      if (!added.length) return toast("info", `这 ${duplicates} 首都已在清单里，没有新增`);
      setTracks((xs) => {
        const known = new Set(xs!.map((x) => x.id));
        return [...xs!, ...added.filter((t) => !known.has(t.id) && !isTrackDeleted(t.id, id))].sort((a, b) => a.position - b.position);
      });
      setAdding(false);
      setFilter("all");
      setQ("");
      upsertJob(
        await api.searchPlaylist(
          id,
          added.map((t) => t.id),
        ),
      );
      toast("info", `已添加 ${added.length} 首${dup}，正在搜索`);
      requestAnimationFrame(() => document.querySelector(`[data-track="${added[0].id}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }));
    });

  // Deleting is per track on the server; busy tracks (searching / downloading) refuse and are reported.
  const removeTracks = (ids: number[]) => {
    const names = ids.map((x) => tracks.find((t) => t.id === x)?.title).filter(Boolean);
    const what = ids.length === 1 ? `「${names[0]}」` : `选中的 ${ids.length} 首歌`;
    if (!confirm(`从清单中删除${what}？已下载的文件不会被删除。`)) return;
    return run("delete", async () => {
      const results = await Promise.allSettled(ids.map((x) => api.deleteTrack(x)));
      const gone = new Set(ids.filter((_, i) => results[i].status === "fulfilled"));
      gone.forEach((trackId) => removeTrack({ id: trackId, playlist_id: id }));
      setTracks((xs) => xs!.filter((x) => !gone.has(x.id)));
      setSelected((sel) => new Set([...sel].filter((x) => !gone.has(x))));
      const failed = results.find((r): r is PromiseRejectedResult => r.status === "rejected");
      if (failed) toast("bad", `${ids.length - gone.size} 首没删掉：${(failed.reason as Error).message}`);
      else toast("ok", `已删除 ${gone.size} 首`);
    });
  };

  const allVisibleSelected = visible.length > 0 && visible.every((t) => selected.has(t.id));

  return (
    <div className="page">
      <div className="pl-top">
        <header className="page-head">
          <div className="grow">
            {renaming ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const name = new FormData(e.currentTarget).get("name")?.toString().trim();
                  setRenaming(false);
                  if (name && name !== playlist.name)
                    api
                      .renamePlaylist(id, name)
                      .then(upsertPlaylist)
                      .catch((er) => toast("bad", er.message));
                }}
              >
                <input
                  name="name"
                  className="input title-input"
                  defaultValue={playlist.name}
                  autoFocus
                  onBlur={(e) => e.currentTarget.form?.requestSubmit()}
                />
              </form>
            ) : (
              <h1 className="row gap-8">
                {playlist.name}
                <button className="icon-btn ghost" onClick={() => setRenaming(true)} aria-label="重命名">
                  <Pencil size={15} />
                </button>
              </h1>
            )}
            <p className="muted">
              共 {c.total} 首 · 已完成 {c.done}
              {c.total > 0 && <span className="mono"> · {Math.floor((c.done / c.total) * 100)}%</span>}
            </p>
          </div>
          <div className="head-actions">
            <button
              className={`btn${adding ? " is-on" : ""}`}
              onClick={() => setAdding((x) => !x)}
              aria-expanded={adding}
              title="往这份清单里追加歌曲"
            >
              <Plus size={16} /> 添加歌曲
            </button>
            {c.done > 0 && (
              <button className="btn" disabled={busy === "lyrics"} onClick={fillLyrics} title="给已下载但还没有歌词的歌补上 .lrc 歌词">
                {busy === "lyrics" ? <LoaderCircle className="spin" size={16} /> : <ScrollText size={16} />} 补歌词
              </button>
            )}
            {tracks.some((t) => t.file && !t.file_missing) && (
              <button className="btn" onClick={openFolder} title="打开下载文件夹（勾选了歌曲时定位到该首）">
                <FolderOpen size={16} /> 打开文件夹
              </button>
            )}
            <a className="btn" href={api.exportUrl(id)} download>
              <FileDown size={16} /> 导出结果
            </a>
            <button
              className="icon-btn danger"
              aria-label="删除清单"
              title="删除清单（已下载的文件保留）"
              onClick={() => {
                if (!confirm(`删除清单「${playlist.name}」？已下载的文件不会被删除。`)) return;
                run("delete", async () => {
                  await api.deletePlaylist(id);
                  removePlaylist(id);
                  navigate({ page: "home" });
                });
              }}
            >
              <Trash2 size={16} />
            </button>
          </div>
        </header>
        <StatusBar counts={c} />
      </div>

      {adding && <AddSongs busy={busy === "add"} onAdd={addSongs} onClose={() => setAdding(false)} />}

      <NextSteps
        counts={c}
        missing={tracks.filter((t) => t.file_missing && t.chosen_candidate_id != null).map((t) => t.id)}
        searchFailed={tracks.filter((t) => t.status === "not_found" && t.search_failed).length}
        onRedownload={(ids) => batch("redownload", ids)}
        busy={busy}
        onSearch={() => startSearch()}
        onDownload={() => startDownload()}
        onRetry={() => {
          const ids = tracks.filter((t) => t.status === "failed").map((t) => t.id);
          batch("retry", ids); // the server queues the download itself
        }}
        reviewHref={href({ page: "review", id })}
      />

      <div className="filter-bar">
        <StatusFilters counts={c} filter={filter} onFilter={setFilter} />
        <label className="search-field">
          <Search size={15} aria-hidden />
          <input className="input bare" type="search" placeholder="查找歌名、歌手或专辑" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
      </div>

      <div className="table-wrap card flush">
        <table className="table tracks">
          <thead>
            <tr>
              <th className="check-col">
                <input
                  type="checkbox"
                  aria-label="全选当前筛选结果"
                  checked={allVisibleSelected}
                  onChange={() =>
                    setSelected((s) => {
                      const n = new Set(s);
                      visible.forEach((t) => (allVisibleSelected ? n.delete(t.id) : n.add(t.id)));
                      return n;
                    })
                  }
                />
              </th>
              <th className="num">#</th>
              <th>歌曲</th>
              <th className="num">时长</th>
              <th>状态</th>
              <th>结果</th>
              <th aria-label="操作" />
            </tr>
          </thead>
          <tbody>
            {visible.map((t) => (
              <TrackRow
                key={t.id}
                t={t}
                flash={flash === t.id}
                checked={selected.has(t.id)}
                onCheck={(shift) => {
                  // Shift-click selects (or clears) everything between the last clicked row and this one.
                  const from = shift && anchor.current != null ? visible.findIndex((x) => x.id === anchor.current) : -1;
                  const to = visible.findIndex((x) => x.id === t.id);
                  const span = from < 0 ? [t] : visible.slice(Math.min(from, to), Math.max(from, to) + 1);
                  const on = !selected.has(t.id);
                  setSelected((s) => {
                    const n = new Set(s);
                    span.forEach((x) => (on ? n.add(x.id) : n.delete(x.id)));
                    return n;
                  });
                  anchor.current = t.id;
                }}
                busy={!!busy}
                onUpdate={(next) => setTracks((xs) => (xs ? xs.map((x) => (x.id === next.id ? next : x)) : xs))}
                onRedownload={() => batch("redownload", [t.id])}
                onDelete={() => removeTracks([t.id])}
              />
            ))}
          </tbody>
        </table>
        {visible.length === 0 && <div className="empty-row muted">没有符合条件的歌曲</div>}
      </div>

      {selectedIds.length > 0 && (
        <BulkBar
          picked={tracks.filter((t) => selected.has(t.id))}
          hidden={selectedIds.filter((x) => !visible.some((t) => t.id === x)).length}
          busy={busy}
          onBatch={batch}
          onSearch={startSearch}
          onDownload={startDownload}
          onDelete={removeTracks}
          onClear={() => setSelected(new Set())}
        />
      )}
    </div>
  );
}

const BUSY: TrackStatus[] = ["searching", "queued", "downloading"];

/**
 * Floating actions for the selection. Each button only sends the songs it applies to and shows how
 * many that is, so 下载 never re-queues finished songs and 通过最佳候选 never touches downloaded ones.
 */
function BulkBar({
  picked,
  hidden,
  busy,
  onBatch,
  onSearch,
  onDownload,
  onDelete,
  onClear,
}: {
  picked: Track[];
  hidden: number;
  busy: string | null;
  onBatch: (action: BatchAction, ids: number[]) => void;
  onSearch: (ids: number[]) => void;
  onDownload: (ids: number[]) => void;
  onDelete: (ids: number[]) => void;
  onClear: () => void;
}) {
  const ids = (ok: (t: Track) => boolean) => picked.filter(ok).map((t) => t.id);
  const idle = (t: Track) => !BUSY.includes(t.status);
  const actions = [
    {
      key: "accept",
      label: "通过最佳候选",
      Icon: ListChecks,
      ids: ids((t) => t.status === "needs_review"),
      run: (x: number[]) => onBatch("accept_best", x),
      why: "待审核",
    },
    {
      key: "search",
      label: "重新搜索",
      Icon: Search,
      ids: ids((t) => idle(t) && t.status !== "done" && t.status !== "skipped"),
      run: onSearch,
      why: "未完成",
    },
    { key: "download", label: "下载", Icon: Download, ids: ids((t) => t.status === "matched"), run: onDownload, why: "待下载" },
    {
      key: "retry",
      label: "重试",
      Icon: RotateCcw,
      ids: ids((t) => t.status === "failed" && t.chosen_candidate_id != null),
      run: (x: number[]) => onBatch("retry", x),
      why: "失败",
    },
    {
      key: "redownload",
      label: "重新下载",
      Icon: Download,
      ids: ids((t) => !!t.file_missing && t.chosen_candidate_id != null),
      run: (x: number[]) => onBatch("redownload", x),
      why: "文件丢失",
    },
    {
      key: "skip",
      label: "跳过",
      Icon: SkipForward,
      ids: ids((t) => idle(t) && t.status !== "done" && t.status !== "skipped"),
      run: (x: number[]) => onBatch("skip", x),
      why: "未完成",
    },
    {
      key: "unskip",
      label: "取消跳过",
      Icon: Undo2,
      ids: ids((t) => t.status === "skipped"),
      run: (x: number[]) => onBatch("unskip", x),
      why: "已跳过",
    },
  ].filter((a) => a.ids.length > 0);
  const deletable = ids(idle);
  return (
    <div className="bulk" role="toolbar" aria-label="批量操作">
      <span className="bulk-count" title={hidden ? `${hidden} 首不在当前筛选里，也会一起处理` : undefined}>
        已选 {picked.length} 首{hidden > 0 && <small> · {hidden} 首未显示</small>}
      </span>
      {actions.map(({ key, label, Icon, ids: x, run, why }) => (
        <button key={key} className="btn btn-sm" disabled={!!busy} onClick={() => run(x)} title={`对其中 ${x.length} 首${why}的歌生效`}>
          <Icon size={14} /> {label}
          {x.length < picked.length && <span className="bulk-n mono">{x.length}</span>}
        </button>
      ))}
      <button
        className="btn btn-sm"
        disabled={!!busy || !deletable.length}
        onClick={() => onDelete(deletable)}
        title={deletable.length < picked.length ? "正在处理的歌不能删除" : undefined}
      >
        <Trash2 size={14} /> 删除
        {deletable.length > 0 && deletable.length < picked.length && <span className="bulk-n mono">{deletable.length}</span>}
      </button>
      <button className="link-btn" onClick={onClear} title="Esc">
        清除选择
      </button>
    </div>
  );
}

function NextSteps({
  counts: c,
  missing,
  searchFailed,
  onRedownload,
  busy,
  onSearch,
  onDownload,
  onRetry,
  reviewHref,
}: {
  counts: Playlist["counts"];
  /** done tracks whose file vanished from disk and can be fetched again */
  missing: number[];
  searchFailed: number;
  onRedownload: (ids: number[]) => void;
  busy: string | null;
  onSearch: () => void;
  onDownload: () => void;
  onRetry: () => void;
  reviewHref: string;
}) {
  const searchable = c.pending + c.not_found;
  const steps = [
    c.pending > 0 && (
      <button key="s" className="step-btn" disabled={!!busy} onClick={onSearch}>
        {busy === "search" ? <LoaderCircle className="spin" size={18} /> : <Search size={18} />}
        <span>
          <b>搜索 {searchable} 首</b>
          <small>{c.not_found ? `含 ${c.not_found} 首上次未找到` : "查询全部启用的音源并自动匹配"}</small>
        </span>
      </button>
    ),
    c.needs_review > 0 && (
      <a key="r" className="step-btn tone-warn" href={reviewHref}>
        <ListChecks size={18} />
        <span>
          <b>审核 {c.needs_review} 首</b>
          <small>候选拿不准，需要你挑一下</small>
        </span>
      </a>
    ),
    c.matched > 0 && (
      <button key="d" className="step-btn tone-primary" disabled={!!busy} onClick={onDownload}>
        {busy === "download" ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />}
        <span>
          <b>下载 {c.matched} 首</b>
          <small>已确认版本，下载后逐个校验</small>
        </span>
      </button>
    ),
    c.failed > 0 && (
      <button key="f" className="step-btn tone-bad" disabled={!!busy} onClick={onRetry}>
        <RotateCcw size={18} />
        <span>
          <b>重试失败的 {c.failed} 首</b>
          <small>再试一次；仍失败可点该行「候选」换一个</small>
        </span>
      </button>
    ),
    missing.length > 0 && (
      <button key="m" className="step-btn tone-warn" disabled={!!busy} onClick={() => onRedownload(missing)}>
        <Download size={18} />
        <span>
          <b>重新下载丢失的 {missing.length} 首</b>
          <small>文件被移走或删除了，按原来选定的版本再下一次</small>
        </span>
      </button>
    ),
    c.pending === 0 && c.not_found > 0 && (
      <button key="n" className="step-btn tone-miss" disabled={!!busy} onClick={onSearch}>
        <Search size={18} />
        <span>
          <b>重新搜索未找到的 {c.not_found} 首</b>
          <small>
            {searchFailed === c.not_found
              ? "上次是音源连不上，检查网络或代理后再搜"
              : searchFailed > 0
                ? `其中 ${searchFailed} 首是音源连不上，不是没有这首歌`
                : "也可以进审核页换关键词手动搜"}
          </small>
        </span>
      </button>
    ),
  ].filter(Boolean);

  if (!steps.length) {
    const allDone = c.total > 0 && c.done + c.skipped === c.total;
    if (!c.total) return <div className="next-empty">清单还是空的，点右上角「添加歌曲」粘贴几首。</div>;
    return <div className={`next-empty${allDone ? " is-done" : ""}`}>{allDone ? "这份清单已经全部处理完。" : "正在处理中，进度会实时更新。"}</div>;
  }
  return <div className="next-steps">{steps}</div>;
}

function TrackRow({
  t,
  flash,
  checked,
  onCheck,
  busy,
  onUpdate,
  onRedownload,
  onDelete,
}: {
  t: Track;
  flash: boolean;
  checked: boolean;
  busy: boolean;
  onCheck: (shift: boolean) => void;
  onUpdate: (t: Track) => void;
  onRedownload: () => void;
  onDelete: () => void;
}) {
  const reviewable = t.candidate_count > 0 || t.status === "not_found";
  const go = () => reviewable && navigate({ page: "review", id: t.playlist_id, track: t.id });
  return (
    <tr
      data-track={t.id}
      className={`${checked ? "is-selected" : ""}${reviewable ? " is-clickable" : ""}${flash ? " is-flash" : ""}${t.status === "downloading" ? " is-flowing" : ""}`}
      onClick={go}
    >
      <td className="check-col" onClick={(e) => e.stopPropagation()}>
        {t.status === "downloading" && <RowFlow trackId={t.id} />}
        <input type="checkbox" checked={checked} onChange={(e) => onCheck((e.nativeEvent as MouseEvent).shiftKey)} aria-label={`选择 ${t.title}`} />
      </td>
      <td className="num mono muted">{t.position}</td>
      <td>
        <div className="song">
          <span className="song-title">
            {t.title} <Flags flags={t.flags} />
          </span>
          <span className="song-artist">{t.artist || "未写歌手"}</span>
          {t.file_missing ? (
            <span className="song-error tone-text-warn" title={t.file?.path}>
              <CircleAlert size={12} aria-hidden /> 文件已被移走或删除，可重新下载
            </span>
          ) : t.status === "not_found" && t.search_failed ? (
            <span className="song-error tone-text-warn" title={t.error ?? undefined}>
              <CircleAlert size={12} aria-hidden /> 音源都没连上，检查网络或代理后重新搜索
            </span>
          ) : (
            t.error &&
            (t.status === "failed" || t.status === "not_found" || t.status === "done") && (
              <span className={`song-error${t.status === "not_found" ? " tone-text-miss" : t.status === "done" ? " tone-text-warn" : ""}`}>
                <CircleAlert size={12} aria-hidden /> {t.error}
              </span>
            )
          )}
        </div>
      </td>
      <td className="num mono">{fmtDuration(t.duration)}</td>
      <td>
        <StatusBadge status={t.status} />
        {t.status === "searching" && <LoaderCircle className="spin inline-spin" size={13} />}
        {t.status === "downloading" && <FlowLabel trackId={t.id} />}
      </td>
      <td>
        {t.file ? (
          <div className="src">
            <span>{t.file.provider_label ?? "已下载"}</span>
            <span className="mono small muted">
              {t.file_missing ? (
                "文件不在原位置"
              ) : (
                <>
                  {t.file.format.toUpperCase()}
                  {t.file.bitrate ? ` · ${t.file.bitrate}k` : ""} · {fmtDuration(t.file.duration)}
                  <LyricsTag info={t.file.lyrics} />
                </>
              )}
            </span>
          </div>
        ) : t.chosen ? (
          <ChosenSummary t={t} />
        ) : t.best_score != null ? (
          <span className={`score tone-${toneOfScore(t.best_score)}`} title="最佳候选匹配分">
            {Math.round(t.best_score)}
            <small> 分{t.chosen_by === "user" ? " · 已手选" : t.chosen_by === "auto" ? " · 自动" : ""}</small>
          </span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td className="num row-actions" onClick={(e) => e.stopPropagation()}>
        {t.status !== "skipped" && !t.file_missing && <TrackLrc t={t} onUpdate={onUpdate} />}
        {t.file_missing && t.chosen_candidate_id != null && (
          <button className="link-btn" disabled={!!busy} onClick={onRedownload}>
            重新下载
          </button>
        )}
        {t.file && !t.file_missing && (
          <PlayButton
            playable={{
              key: `t:${t.id}`,
              url: api.audioUrl(t.id),
              title: t.title,
              subtitle: `${t.artist} · 已下载`,
              lyricsUrl: api.trackLyricsUrl(t.id),
              coverUrl: api.trackCoverUrl(t.id),
            }}
            size={28}
          />
        )}
        {reviewable && !t.file && (
          <a className="link-btn" href={href({ page: "review", id: t.playlist_id, track: t.id })}>
            {t.candidate_count ? `候选 ${t.candidate_count}` : "换关键词搜"}
          </a>
        )}
        {t.status !== "searching" && t.status !== "queued" && t.status !== "downloading" && (
          <button className="icon-btn danger row-del" title="从清单删除" aria-label={`删除 ${t.title}`} onClick={onDelete}>
            <Trash2 size={14} />
          </button>
        )}
      </td>
    </tr>
  );
}

/** The candidate that will be downloaded: source and quality, its own score, and whose pick it was. */
function ChosenSummary({ t }: { t: Track }) {
  const c = t.chosen!;
  const by = t.chosen_by === "user" ? "已手选" : t.chosen_by === "auto" ? "自动" : "";
  const who = [c.artist, c.album && `《${c.album}》`].filter(Boolean).join(" · ");
  return (
    <div className="pick" title={`${c.title}${who ? ` — ${who}` : ""}`}>
      <span className="pick-head">
        <span>{c.provider_label}</span>
        <span className={`score tone-${toneOfScore(c.score)}`}>
          {Math.round(c.score)}
          <small> 分{by && ` · ${by}`}</small>
        </span>
      </span>
      <span className="mono small muted">
        {c.quality} · {fmtDuration(c.duration)}
      </span>
      {who && <span className="pick-sub small muted">{who}</span>}
    </div>
  );
}

/**
 * Per-track LRC switch. Not downloaded yet: whether this song's download saves lyrics
 * (stored on the track, defaulting to the global setting). Downloaded: whether lyrics
 * exist; clicking an unlit one looks them up now.
 */
function TrackLrc({ t, onUpdate }: { t: Track; onUpdate: (t: Track) => void }) {
  const toast = useToast();
  const lyricsDefault = useLyricsDefault();
  const [busy, setBusy] = useState(false);
  const info = t.file?.lyrics;
  const saved = !!info && !info.error && !!(info.path || info.embedded);

  async function act() {
    setBusy(true);
    try {
      if (t.file) {
        const next = await api.trackLyrics(t.id, true);
        onUpdate(next);
        const got = next.file?.lyrics;
        if (got && !got.error && (got.path || got.embedded)) toast("ok", `「${t.title}」已保存歌词（${got.source_label ?? "歌词"}）`);
        else toast("info", got?.error ?? `没有找到「${t.title}」的歌词`);
      } else
        onUpdate(
          await api.updateTrack(t.id, {
            lyrics_wanted: !(t.lyrics_wanted ?? lyricsDefault),
          }),
        );
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (t.file)
    return (
      <LrcSwitch
        on={saved}
        busy={busy}
        onToggle={() => (saved ? toast("info", `歌词已保存（${info?.source_label ?? "LRC"}）`) : act())}
        title={saved ? "已保存歌词" : "还没有歌词，点击查找并保存"}
      />
    );
  const on = t.lyrics_wanted ?? lyricsDefault;
  return <LrcSwitch on={on} busy={busy} onToggle={act} title={on ? "下载时一起保存 .lrc 歌词（点击取消）" : "只下载音频（点击同时保存歌词）"} />;
}

/** Inline paste box for appending songs; same line format as the import page. Ctrl/⌘+Enter adds. */
function AddSongs({ busy, onAdd, onClose }: { busy: boolean; onAdd: (text: string) => void; onClose: () => void }) {
  const [text, setText] = useState("");
  const submit = () => text.trim() && !busy && onAdd(text);
  return (
    <div className="card add-songs fade-in">
      <textarea
        className="input textarea mono-ish"
        rows={4}
        autoFocus
        placeholder={"每行一首，例如：\n晴天 - 周杰伦\n后来 - 刘若英"}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
          if (e.key === "Escape") onClose();
        }}
      />
      <div className="row between">
        <span className="muted small">清单里已有的歌会自动略过；添加后只搜索新加的这几首。</span>
        <div className="row gap-8">
          <button className="btn btn-ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn btn-primary" disabled={!text.trim() || busy} onClick={submit} title="⌘/Ctrl + Enter">
            {busy ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} 添加并搜索
          </button>
        </div>
      </div>
    </div>
  );
}
