import { useEffect, useMemo, useState } from "react";
import {
  CircleAlert,
  Download,
  FileDown,
  FolderOpen,
  ListChecks,
  LoaderCircle,
  Pencil,
  RotateCcw,
  Search,
  SkipForward,
  Trash2,
  Undo2,
} from "lucide-react";
import { api } from "../api";
import { Flags, StatusBadge, toneOfScore, useToast } from "../components";
import { fmtDuration } from "../format";
import { PlayButton } from "../player";
import { href, navigate } from "../router";
import { StatusStrip, matchesFilter, type Filter } from "../StatusStrip";
import { onEvent, removePlaylist, upsertJob, upsertPlaylist, useStore } from "../store";
import type { BatchAction, Playlist, Track } from "../types";

export function PlaylistPage({ id }: { id: number }) {
  const toast = useToast();
  const [tracks, setTracks] = useState<Track[] | null>(null);
  const [fallback, setFallback] = useState<Playlist | null>(null);
  const playlist = useStore((s) => s.playlists.find((p) => p.id === id)) ?? fallback;
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);

  useEffect(() => {
    api
      .playlist(id)
      .then(({ playlist, tracks }) => {
        setFallback(playlist);
        upsertPlaylist(playlist);
        setTracks(tracks);
      })
      .catch((e) => toast("bad", e.message));
    return onEvent("track", (t) => {
      if (t.playlist_id !== id) return;
      setTracks((xs) => (xs ? xs.map((x) => (x.id === t.id ? t : x)) : xs));
    });
  }, [id, toast]);

  const visible = useMemo(() => {
    if (!tracks) return [];
    const needle = q.trim().toLowerCase();
    return tracks.filter(
      (t) => matchesFilter(t.status, filter) && (!needle || `${t.title} ${t.artist} ${t.album}`.toLowerCase().includes(needle)),
    );
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
      toast("bad", (e as Error).message);
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
  const batch = (action: BatchAction, ids: number[]) =>
    run(action, async () => {
      const { updated } = await api.batch(ids, action);
      const map = new Map(updated.map((t) => [t.id, t]));
      setTracks((xs) => xs!.map((x) => map.get(x.id) ?? x));
      setSelected(new Set());
      return updated;
    });

  const allVisibleSelected = visible.length > 0 && visible.every((t) => selected.has(t.id));

  return (
    <div className="page">
      <header className="page-head">
        <div className="grow">
          {renaming ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const name = new FormData(e.currentTarget).get("name")?.toString().trim();
                setRenaming(false);
                if (name && name !== playlist.name) api.renamePlaylist(id, name).then(upsertPlaylist).catch((er) => toast("bad", er.message));
              }}
            >
              <input name="name" className="input title-input" defaultValue={playlist.name} autoFocus onBlur={(e) => e.currentTarget.form?.requestSubmit()} />
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
          </p>
        </div>
        <div className="row gap-8">
          <a className="btn" href={api.exportUrl(id)} download>
            <FileDown size={16} /> 导出结果
          </a>
          <button
            className="btn btn-ghost danger"
            onClick={() => {
              if (!confirm(`删除清单「${playlist.name}」？已下载的文件不会被删除。`)) return;
              run("delete", async () => {
                await api.deletePlaylist(id);
                removePlaylist(id);
                navigate({ page: "home" });
              });
            }}
          >
            <Trash2 size={16} /> 删除
          </button>
        </div>
      </header>

      <NextSteps
        counts={c}
        busy={busy}
        onSearch={() => startSearch()}
        onDownload={() => startDownload()}
        onRetry={() => {
          const ids = tracks.filter((t) => t.status === "failed").map((t) => t.id);
          batch("retry", ids).then(() => startDownload(ids));
        }}
        reviewHref={href({ page: "review", id })}
      />

      <StatusStrip counts={c} filter={filter} onFilter={setFilter} />

      <div className="toolbar">
        <label className="search-field">
          <Search size={15} aria-hidden />
          <input className="input bare" placeholder="在清单里查找" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        {selectedIds.length > 0 && (
          <div className="bulk" role="toolbar" aria-label="批量操作">
            <span className="bulk-count">已选 {selectedIds.length} 首</span>
            <button className="btn btn-sm" onClick={() => batch("accept_best", selectedIds)}>
              <ListChecks size={14} /> 通过最佳候选
            </button>
            <button className="btn btn-sm" onClick={() => startSearch(selectedIds)}>
              <Search size={14} /> 重新搜索
            </button>
            <button className="btn btn-sm" onClick={() => startDownload(selectedIds)}>
              <Download size={14} /> 下载
            </button>
            <button className="btn btn-sm" onClick={() => batch("skip", selectedIds)}>
              <SkipForward size={14} /> 跳过
            </button>
            <button className="btn btn-sm" onClick={() => batch("unskip", selectedIds)}>
              <Undo2 size={14} /> 取消跳过
            </button>
            <button className="link-btn" onClick={() => setSelected(new Set())}>
              清除选择
            </button>
          </div>
        )}
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
                checked={selected.has(t.id)}
                onCheck={() =>
                  setSelected((s) => {
                    const n = new Set(s);
                    if (n.has(t.id)) n.delete(t.id);
                    else n.add(t.id);
                    return n;
                  })
                }
                onReveal={() => api.reveal(t.id).catch((e) => toast("bad", e.message))}
              />
            ))}
          </tbody>
        </table>
        {visible.length === 0 && <div className="empty-row muted">没有符合条件的歌曲</div>}
      </div>
    </div>
  );
}

function NextSteps({
  counts: c,
  busy,
  onSearch,
  onDownload,
  onRetry,
  reviewHref,
}: {
  counts: Playlist["counts"];
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
    c.pending === 0 && c.not_found > 0 && (
      <button key="n" className="step-btn" disabled={!!busy} onClick={onSearch}>
        <Search size={18} />
        <span>
          <b>重新搜索未找到的 {c.not_found} 首</b>
          <small>也可以进审核页换关键词手动搜</small>
        </span>
      </button>
    ),
  ].filter(Boolean);

  if (!steps.length) {
    const allDone = c.total > 0 && c.done + c.skipped === c.total;
    return <div className={`next-empty${allDone ? " is-done" : ""}`}>{allDone ? "这份清单已经全部处理完。" : "正在处理中，进度会实时更新。"}</div>;
  }
  return <div className="next-steps">{steps}</div>;
}

function TrackRow({ t, checked, onCheck, onReveal }: { t: Track; checked: boolean; onCheck: () => void; onReveal: () => void }) {
  const reviewable = t.candidate_count > 0 || t.status === "not_found";
  const go = () => reviewable && navigate({ page: "review", id: t.playlist_id, track: t.id });
  return (
    <tr className={`${checked ? "is-selected" : ""}${reviewable ? " is-clickable" : ""}`} onClick={go}>
      <td className="check-col" onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" checked={checked} onChange={onCheck} aria-label={`选择 ${t.title}`} />
      </td>
      <td className="num mono muted">{t.position}</td>
      <td>
        <div className="song">
          <span className="song-title">
            {t.title} <Flags flags={t.flags} />
          </span>
          <span className="song-artist">{t.artist || "未写歌手"}</span>
          {t.error && (t.status === "failed" || t.status === "not_found") && (
            <span className="song-error">
              <CircleAlert size={12} aria-hidden /> {t.error}
            </span>
          )}
        </div>
      </td>
      <td className="num mono">{fmtDuration(t.duration)}</td>
      <td>
        <StatusBadge status={t.status} />
        {t.status === "searching" || t.status === "downloading" ? <LoaderCircle className="spin inline-spin" size={13} /> : null}
      </td>
      <td>
        {t.file ? (
          <div className="src">
            <span>{t.file.provider_label ?? "已下载"}</span>
            <span className="mono small muted">
              {t.file.format.toUpperCase()}
              {t.file.bitrate ? ` · ${t.file.bitrate}k` : ""} · {fmtDuration(t.file.duration)}
            </span>
          </div>
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
        {t.file && (
          <>
            <PlayButton playable={{ key: `t:${t.id}`, url: api.audioUrl(t.id), title: t.title, subtitle: `${t.artist} · 已下载` }} size={28} />
            <button className="icon-btn" onClick={onReveal} aria-label="在文件夹中显示" title="在文件夹中显示">
              <FolderOpen size={15} />
            </button>
          </>
        )}
        {reviewable && !t.file && (
          <a className="link-btn" href={href({ page: "review", id: t.playlist_id, track: t.id })}>
            {t.candidate_count ? `候选 ${t.candidate_count}` : "换关键词搜"}
          </a>
        )}
      </td>
    </tr>
  );
}
