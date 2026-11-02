import { useEffect, useRef, useState } from "react";
import { Check, CircleAlert, Crown, Download, FolderOpen, LoaderCircle, ScrollText, Search, X } from "lucide-react";
import { api } from "../api";
import { CheckChips, Flags, LrcToggle, LyricsTag, StatusBadge, useToast } from "../components";
import { fmtDuration } from "../format";
import { useFlipList } from "../motion";
import { PlayButton } from "../player";
import { href } from "../router";
import { searchActions, useSearch, type SourceState } from "../searchStore";
import { upsertJob } from "../store";
import type { Candidate } from "../types";

export function SearchPage() {
  const toast = useToast();
  const { query, dur, loading, session, track, cands, sources, startedAt, elapsed, showRejected, error } = useSearch();
  const [lyricsBusy, setLyricsBusy] = useState(false);
  const body = useRef<HTMLTableSectionElement | null>(null);
  const setTrack = searchActions.setTrack;

  useEffect(() => {
    if (track && ["queued", "downloading"].includes(track.status)) searchActions.refreshTrack().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!error) return;
    toast("bad", error);
    searchActions.clearError();
  }, [error, toast]);

  const search = searchActions.run;

  async function download(c: Candidate) {
    if (!track) return;
    try {
      setTrack(await api.choose(track.id, c.id));
      upsertJob(await api.downloadPlaylist(track.playlist_id, [track.id]));
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  async function fillLyrics() {
    if (!track) return;
    setLyricsBusy(true);
    try {
      const next = await api.trackLyrics(track.id, true);
      setTrack(next);
      const info = next.file?.lyrics;
      if (info && !info.error && (info.path || info.embedded)) toast("ok", `已保存歌词（${info.source_label ?? "歌词"}）`);
      else toast("info", info?.error ?? "没有找到这首歌的歌词");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setLyricsBusy(false);
    }
  }

  const viable = cands.filter((c) => c.verdict !== "reject");
  const rejected = cands.filter((c) => c.verdict === "reject");
  const shown = showRejected ? cands : viable;
  const busy = track && ["queued", "downloading"].includes(track.status);
  const searching = loading && !track;
  useFlipList(body, shown.map((c) => c.id).join(","), session, false);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>单曲搜索</h1>
          <p className="muted">输入「歌名 歌手」，同时查询所有启用的音源，先返回的先显示。下载过的歌会记录在「单曲搜索」清单里。</p>
        </div>
      </header>

      <form
        className={`hero-search${searching ? " is-searching" : ""}`}
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) search();
        }}
      >
        <Search size={20} aria-hidden />
        <input className="input bare grow" placeholder="例如：晴天 周杰伦" value={query} onChange={(e) => searchActions.setQuery(e.target.value)} autoFocus />
        <input className="input bare mono w-90" placeholder="时长 可选" value={dur} onChange={(e) => searchActions.setDur(e.target.value)} aria-label="时长（可选）" title="填了时长会更准，例如 4:29" />
        <button className="btn btn-primary" disabled={!query.trim()}>
          {loading ? <LoaderCircle className="spin" size={16} /> : null} 搜索
        </button>
      </form>

      {sources.length > 0 && <SourceStrip sources={sources} startedAt={startedAt} elapsed={elapsed} done={!loading} />}

      {(track || cands.length > 0 || searching) && (
        <section className="stack gap-12">
          {track?.file ? (
            <div className="verdict tone-ok fade-in">
              <Download size={18} aria-hidden />
              <div>
                <b>已下载：{track.file.path.split("/").pop()}</b>
                <span>
                  {track.file.provider_label ? `${track.file.provider_label} · ` : ""}
                  {track.file.format.toUpperCase()}
                  {track.file.bitrate ? ` · ${track.file.bitrate}k` : ""} · {fmtDuration(track.file.duration)} · 已通过完整解码校验
                  <LyricsTag info={track.file.lyrics} />
                </span>
              </div>
              <div className="row gap-8 end">
                <PlayButton playable={{ key: `t:${track.id}`, url: api.audioUrl(track.id), title: track.title, subtitle: `${track.artist} · 已下载`, lyricsUrl: api.trackLyricsUrl(track.id), coverUrl: api.trackCoverUrl(track.id) }} />
                {(!track.file.lyrics || track.file.lyrics.error || (!track.file.lyrics.path && !track.file.lyrics.embedded)) && (
                  <button className="btn btn-sm" disabled={lyricsBusy} onClick={fillLyrics} title="重新查找并保存这首歌的歌词">
                    {lyricsBusy ? <LoaderCircle className="spin" size={14} /> : <ScrollText size={14} />} 补歌词
                  </button>
                )}
                <button className="btn btn-sm" onClick={() => api.reveal(track.id).catch((e) => toast("bad", e.message))}>
                  <FolderOpen size={14} /> 打开位置
                </button>
              </div>
            </div>
          ) : busy ? (
            <div className="verdict tone-info fade-in">
              <LoaderCircle className="spin" size={18} aria-hidden />
              <div>
                <b>{track!.status === "queued" ? "排队中" : "下载并校验中"}</b>
                <span>完成后会在这里显示文件。</span>
              </div>
            </div>
          ) : track?.status === "failed" ? (
            <div className="verdict tone-bad fade-in">
              <CircleAlert size={18} aria-hidden />
              <div>
                <b>下载失败</b>
                <span>{track.error ?? "换一个候选试试。"}</span>
              </div>
            </div>
          ) : null}

          <div className="card flush">
            <table className="table cand-table">
              <thead>
                <tr>
                  <th aria-label="试听" />
                  <th>歌曲</th>
                  <th>来源</th>
                  <th className="num">时长</th>
                  <th>检查</th>
                  <th className="num">
                    <LrcToggle compact />
                  </th>
                </tr>
              </thead>
              <tbody ref={body}>
                {shown.map((c) => (
                  <tr key={c.id} data-flip={c.id} className={`cand v-${c.verdict}${track && c.id === track.chosen_candidate_id ? " is-chosen" : ""}`}>
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
                        <span className="mono small muted">{c.quality.label}</span>
                      </div>
                    </td>
                    <td className="num mono">{fmtDuration(c.duration)}</td>
                    <td>
                      <CheckChips checks={c.checks} only={["title", "artist", "version", "popularity"]} />
                    </td>
                    <td className="num">
                      {track && c.id === track.chosen_candidate_id && !["failed", "matched"].includes(track.status) ? (
                        <StatusBadge status={track.status} />
                      ) : (
                        <button
                          className="btn btn-sm btn-choose"
                          disabled={!track || !!busy}
                          title={track ? undefined : "等所有音源返回后再下载"}
                          onClick={() => download(c)}
                        >
                          <Download size={14} /> 下载
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {searching && shown.length === 0 && <SkeletonRows />}
            {!searching && track && cands.length === 0 && <div className="empty-row muted">没有搜到结果，换个写法试试。</div>}
            {!searching && cands.length > 0 && shown.length === 0 && !showRejected && (
              <div className="empty-row muted">搜到的结果都是其他版本，可以点下方查看。</div>
            )}
            {rejected.length > 0 && (
              <div className="table-more">
                <button className="link-btn" onClick={searchActions.toggleRejected}>
                  {showRejected ? "隐藏" : "显示"}被排除的 {rejected.length} 个结果（Live、伴奏、DJ 等其他版本）
                </button>
              </div>
            )}
          </div>
          {track && (
            <p className="muted small">
              下载记录保存在 <a href={href({ page: "playlist", id: track.playlist_id })}>「单曲搜索」清单</a>。
            </p>
          )}
        </section>
      )}
    </div>
  );
}

function SourceStrip({ sources, startedAt, elapsed, done }: { sources: SourceState[]; startedAt: number; elapsed: number | null; done: boolean }) {
  const [now, setNow] = useState(performance.now());
  useEffect(() => {
    if (done) return;
    const timer = setInterval(() => setNow(performance.now()), 100);
    return () => clearInterval(timer);
  }, [done]);
  const finished = sources.filter((s) => s.state !== "pending").length;
  return (
    <div className="source-strip" aria-live="polite">
      <span className="source-strip-head mono">
        {done ? "完成" : `${finished}/${sources.length}`}
        {done ? (elapsed != null ? ` · ${elapsed.toFixed(1)}s` : "") : ` · ${((Math.max(now, startedAt) - startedAt) / 1000).toFixed(1)}s`}
      </span>
      {sources.map((s) => (
        <span key={s.id} className={`source-pill is-${s.state}`} title={s.error ?? undefined}>
          {s.state === "pending" ? <LoaderCircle className="spin" size={12} /> : s.state === "ok" ? <Check size={12} /> : <X size={12} />}
          {s.label}
          {s.state === "error" && s.error && <span className="source-pill-note">{s.error.replace(/^[^：]*：/, "")}</span>}
        </span>
      ))}
    </div>
  );
}

function SkeletonRows() {
  return (
    <div className="skeleton-rows" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div key={i} className="skeleton-row" style={{ animationDelay: `${i * 120}ms` }}>
          <span className="sk sk-dot" />
          <span className="sk sk-line w-40" />
          <span className="sk sk-line w-15" />
          <span className="sk sk-line w-25" />
        </div>
      ))}
    </div>
  );
}
