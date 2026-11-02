import { useEffect, useState } from "react";
import { CircleAlert, Download, FolderOpen, LoaderCircle, Search } from "lucide-react";
import { api } from "../api";
import { CheckChips, Flags, StatusBadge, useToast } from "../components";
import { fmtDuration } from "../format";
import { PlayButton } from "../player";
import { href } from "../router";
import { onEvent, upsertJob } from "../store";
import type { Candidate, Track } from "../types";

export function SearchPage() {
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [dur, setDur] = useState("");
  const [loading, setLoading] = useState(false);
  const [track, setTrack] = useState<Track | null>(null);
  const [cands, setCands] = useState<Candidate[]>([]);
  const [showRejected, setShowRejected] = useState(false);

  useEffect(
    () =>
      onEvent("track", (t) => {
        setTrack((cur) => (cur && cur.id === t.id ? t : cur));
      }),
    [],
  );

  async function search() {
    const m = dur.trim().match(/^(\d+):(\d{1,2})$/);
    const seconds = m ? Number(m[1]) * 60 + Number(m[2]) : dur.trim() ? Number(dur) : undefined;
    setLoading(true);
    setShowRejected(false);
    try {
      const r = await api.searchOne(query.trim(), seconds && isFinite(seconds) ? seconds : undefined);
      setTrack(r.track);
      setCands(r.candidates);
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function download(c: Candidate) {
    if (!track) return;
    try {
      setTrack(await api.choose(track.id, c.id));
      upsertJob(await api.downloadPlaylist(track.playlist_id, [track.id]));
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  const viable = cands.filter((c) => c.verdict !== "reject");
  const rejected = cands.filter((c) => c.verdict === "reject");
  const shown = showRejected ? cands : viable;
  const busy = track && ["queued", "downloading"].includes(track.status);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>单曲搜索</h1>
          <p className="muted">输入「歌名 歌手」，同时查询所有启用的音源。下载过的歌会记录在「单曲搜索」清单里。</p>
        </div>
      </header>

      <form
        className="hero-search"
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) search();
        }}
      >
        <Search size={20} aria-hidden />
        <input className="input bare grow" placeholder="例如：晴天 周杰伦" value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
        <input className="input bare mono w-90" placeholder="时长 可选" value={dur} onChange={(e) => setDur(e.target.value)} aria-label="时长（可选）" title="填了时长会更准，例如 4:29" />
        <button className="btn btn-primary" disabled={loading || !query.trim()}>
          {loading ? <LoaderCircle className="spin" size={16} /> : null} 搜索
        </button>
      </form>

      {track && (
        <section className="stack gap-12">
          {track.file ? (
            <div className="verdict tone-ok">
              <Download size={18} aria-hidden />
              <div>
                <b>已下载：{track.file.path.split("/").pop()}</b>
                <span>
                  {track.file.format.toUpperCase()}
                  {track.file.bitrate ? ` · ${track.file.bitrate}k` : ""} · {fmtDuration(track.file.duration)} · 已通过完整解码校验
                </span>
              </div>
              <div className="row gap-8 end">
                <PlayButton playable={{ key: `t:${track.id}`, url: api.audioUrl(track.id), title: track.title, subtitle: "已下载" }} />
                <button className="btn btn-sm" onClick={() => api.reveal(track.id).catch((e) => toast("bad", e.message))}>
                  <FolderOpen size={14} /> 打开位置
                </button>
              </div>
            </div>
          ) : busy ? (
            <div className="verdict tone-info">
              <LoaderCircle className="spin" size={18} aria-hidden />
              <div>
                <b>{track.status === "queued" ? "排队中" : "下载并校验中"}</b>
                <span>完成后会在这里显示文件。</span>
              </div>
            </div>
          ) : track.status === "failed" ? (
            <div className="verdict tone-bad">
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
                  <th aria-label="下载" />
                </tr>
              </thead>
              <tbody>
                {shown.map((c) => (
                  <tr key={c.id} className={`cand v-${c.verdict}${c.id === track.chosen_candidate_id ? " is-chosen" : ""}`}>
                    <td>
                      <PlayButton playable={{ key: `c:${c.id}`, url: c.preview_url, title: c.title, subtitle: `${c.artist} · ${c.provider_label} 试听` }} />
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
                        <span>{c.provider_label}</span>
                        <span className="mono small muted">{c.quality.label}</span>
                      </div>
                    </td>
                    <td className="num mono">{fmtDuration(c.duration)}</td>
                    <td>
                      <CheckChips checks={c.checks} only={["title", "artist", "version", "popularity"]} />
                    </td>
                    <td className="num">
                      {c.id === track.chosen_candidate_id && track.status !== "failed" ? (
                        <StatusBadge status={track.status} />
                      ) : (
                        <button className="btn btn-sm btn-choose" disabled={!!busy} onClick={() => download(c)}>
                          <Download size={14} /> 下载
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {cands.length === 0 && <div className="empty-row muted">没有搜到结果，换个写法试试。</div>}
            {rejected.length > 0 && (
              <div className="table-more">
                <button className="link-btn" onClick={() => setShowRejected((v) => !v)}>
                  {showRejected ? "隐藏" : "显示"}被排除的 {rejected.length} 个结果（Live、伴奏、DJ 等其他版本）
                </button>
              </div>
            )}
          </div>
          <p className="muted small">
            下载记录保存在 <a href={href({ page: "playlist", id: track.playlist_id })}>「单曲搜索」清单</a>。
          </p>
        </section>
      )}
    </div>
  );
}
