import { useEffect, useRef, useState } from "react";
import { Check, CircleAlert, Crown, Download, FolderOpen, ListChecks, LoaderCircle, RotateCcw, ScrollText, Search, X } from "lucide-react";
import { api } from "../api";
import { CheckChips, Flags, LrcSwitch, LyricsTag, StatusBadge, useLyricsDefault, useToast } from "../components";
import { fmtDuration } from "../format";
import { useFlipList } from "../motion";
import { PlayButton } from "../player";
import { href, navigate } from "../router";
import { searchActions, useSearch, type SearchState } from "../searchStore";
import { onEvent, upsertJob } from "../store";
import type { ArtistSong, Candidate, LyricsInfo, Track } from "../types";

const hasLyrics = (info: LyricsInfo | null | undefined) => !!info && !info.error && !!(info.path || info.embedded);

export function SearchPage() {
  const toast = useToast();
  const s = useSearch();
  const { query, dur, loading, mode, track, cands, error } = s;

  useEffect(() => {
    searchActions.loadCatalog().catch(() => {});
    if (track && ["queued", "downloading"].includes(track.status)) searchActions.refreshTrack().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => onEvent("resync", () => void searchActions.refreshTrack().catch(() => {})), []);

  useEffect(() => {
    if (!error) return;
    toast("bad", error);
    searchActions.clearError();
  }, [error, toast]);

  const searching = loading && !track && mode === "song";

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>单曲搜索</h1>
          <p className="muted">
            输入「歌名 歌手」找一首歌，只输歌手名会列出 TA 的热门歌曲。点下方音源可以临时开关，下载过的歌记录在「单曲搜索」清单里。
          </p>
        </div>
      </header>

      <form
        className={`hero-search${loading ? " is-searching" : ""}`}
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) searchActions.run();
        }}
      >
        <Search size={20} aria-hidden />
        <input
          className="input bare grow"
          placeholder="例如：晴天 周杰伦，或者只输 周杰伦"
          value={query}
          onChange={(e) => searchActions.setQuery(e.target.value)}
          autoFocus
        />
        <input
          className="input bare mono w-90"
          placeholder="时长 可选"
          value={dur}
          onChange={(e) => searchActions.setDur(e.target.value)}
          aria-label="时长（可选）"
          title="填了时长会更准，例如 4:29"
        />
        <button className="btn btn-primary" disabled={!query.trim()}>
          {loading ? <LoaderCircle className="spin" size={16} /> : null} 搜索
        </button>
      </form>

      <SourceSwitches s={s} />

      {mode === "artist" ? <ArtistView s={s} /> : (track || cands.length > 0 || searching) && <SongView s={s} />}
    </div>
  );
}

/* ---------- source switches: green = on, grey = switched off; doubles as live progress ---------- */

function SourceSwitches({ s }: { s: SearchState }) {
  const toast = useToast();
  const { catalog, off, sources, startedAt, elapsed, loading } = s;
  const [now, setNow] = useState(performance.now());
  useEffect(() => {
    if (!loading) return;
    const timer = setInterval(() => setNow(performance.now()), 100);
    return () => clearInterval(timer);
  }, [loading]);

  const list = catalog.length ? catalog : sources.map(({ id, label }) => ({ id, label }));
  if (!list.length) return null;

  const finished = sources.filter((x) => x.state !== "pending").length;
  const on = list.filter((p) => !off.includes(p.id)).map((p) => p.id);
  // The switches moved since the shown results were searched: offer to search again.
  const stale = !loading && sources.length > 0 && (on.length !== sources.length || on.some((id) => !sources.some((x) => x.id === id)));

  return (
    <div className="source-strip" aria-live="polite">
      <span className="source-strip-head mono">
        {!sources.length
          ? `音源 ${on.length}/${list.length}`
          : loading
            ? `${finished}/${sources.length} · ${((Math.max(now, startedAt) - startedAt) / 1000).toFixed(1)}s`
            : `完成${elapsed != null ? ` · ${elapsed.toFixed(1)}s` : ""}`}
      </span>
      {list.map((p) => {
        const isOff = off.includes(p.id);
        const st = isOff ? undefined : sources.find((x) => x.id === p.id);
        const cls = isOff ? "is-off" : st && st.state !== "skipped" ? `is-${st.state}` : "is-on";
        return (
          <button
            key={p.id}
            type="button"
            className={`source-pill ${cls}`}
            aria-pressed={!isOff}
            title={
              isOff
                ? `${p.label}：已关闭，点击启用`
                : st?.state === "skipped"
                  ? `${p.label}：歌手模式用不到这个音源（点击关闭）`
                  : st?.error
                    ? `${st.error}（点击关闭这个音源）`
                    : `${p.label}：已启用，点击关闭`
            }
            onClick={() => {
              if (!searchActions.toggleSource(p.id)) toast("info", "至少保留一个音源");
            }}
          >
            {st?.state === "pending" ? (
              <LoaderCircle className="spin" size={12} />
            ) : st?.state === "ok" ? (
              <Check size={12} />
            ) : st?.state === "error" ? (
              <X size={12} />
            ) : (
              <span className="source-pill-dot" aria-hidden />
            )}
            {p.label}
            {st?.state === "error" && st.error && <span className="source-pill-note">{st.error.replace(/^[^：]*：/, "")}</span>}
          </button>
        );
      })}
      {stale && s.query.trim() && (
        <button type="button" className="link-btn source-strip-again" onClick={() => searchActions.run()}>
          <RotateCcw size={12} /> 用当前音源重搜
        </button>
      )}
    </div>
  );
}

/* ---------- song mode: one track, every provider's candidates ---------- */

function SongView({ s }: { s: SearchState }) {
  const toast = useToast();
  const lyricsDefault = useLyricsDefault();
  const { loading, session, track, cands, showRejected, lrc } = s;
  const [lyricsBusy, setLyricsBusy] = useState(false);
  const body = useRef<HTMLTableSectionElement | null>(null);
  const setTrack = searchActions.setTrack;

  async function download(c: Candidate) {
    if (!track) return;
    if (track.file_missing && c.id === track.chosen_candidate_id) return redownload();
    try {
      setTrack(await api.choose(track.id, c.id));
      upsertJob(await api.downloadPlaylist(track.playlist_id, [track.id], lrc[c.id] ?? lyricsDefault));
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
      if (info?.refresh_error) toast("info", `没更新成功，沿用原来的歌词：${info.refresh_error}`);
      else if (hasLyrics(info)) toast("ok", `已保存歌词（${info!.source_label ?? "歌词"}）`);
      else toast("info", info?.error ?? "没有找到这首歌的歌词");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setLyricsBusy(false);
    }
  }

  async function setWanted(on: boolean) {
    if (!track) return;
    try {
      setTrack(await api.updateTrack(track.id, { lyrics_wanted: on }));
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  const viable = cands.filter((c) => c.verdict !== "reject");
  const rejected = cands.filter((c) => c.verdict === "reject");
  const shown = showRejected ? cands : viable;
  const busy = track && ["queued", "downloading"].includes(track.status);
  const searching = loading && !track;
  const chosenShown = !!track && shown.some((c) => c.id === track.chosen_candidate_id);
  useFlipList(body, shown.map((c) => c.id).join(","), session, false);

  const lrcFor = (c: Candidate) => {
    const chosen = track && c.id === track.chosen_candidate_id;
    if (chosen && track.file && !track.file_missing) {
      const on = hasLyrics(track.file.lyrics);
      return (
        <LrcSwitch
          on={on}
          busy={lyricsBusy}
          onToggle={() => (on ? toast("info", `歌词已保存（${track.file?.lyrics?.source_label ?? "LRC"}）`) : fillLyrics())}
          title={on ? "已保存歌词" : "这首还没有歌词，点击查找并保存"}
        />
      );
    }
    if (chosen && busy) {
      const on = track.lyrics_wanted ?? lyricsDefault;
      return <LrcSwitch on={on} onToggle={() => setWanted(!on)} title={on ? "下载完成后保存歌词（点击取消）" : "不保存歌词（点击开启）"} />;
    }
    const on = lrc[c.id] ?? lyricsDefault;
    return (
      <LrcSwitch
        on={on}
        onToggle={() => searchActions.setLrc(String(c.id), !on)}
        title={on ? "下载时一起保存 .lrc 歌词（点击取消）" : "只下载音频（点击同时保存歌词）"}
      />
    );
  };

  async function redownload() {
    if (!track) return;
    try {
      const { updated, skipped = [] } = await api.batch([track.id], "redownload");
      if (updated[0]) setTrack(updated[0]);
      toast("info", skipped.length ? skipped[0].reason : "正在按原来的版本重新下载");
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  return (
    <section className="stack gap-12">
      {track?.file_missing ? (
        <div className="verdict tone-warn fade-in">
          <CircleAlert size={18} aria-hidden />
          <div>
            <b>文件已被移走或删除</b>
            <span>原来保存在 {track.file?.path}。可以按原来的版本重新下载，或在下面换一个。</span>
          </div>
          {track.chosen_candidate_id != null && (
            <div className="row gap-8 end">
              <button className="btn btn-sm" onClick={redownload}>
                <Download size={14} /> 重新下载
              </button>
            </div>
          )}
        </div>
      ) : track?.file ? (
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
            <PlayButton
              playable={{
                key: `t:${track.id}`,
                url: api.audioUrl(track.id),
                title: track.title,
                subtitle: `${track.artist} · 已下载`,
                lyricsUrl: api.trackLyricsUrl(track.id),
                coverUrl: api.trackCoverUrl(track.id),
              }}
            />
            {!chosenShown && !hasLyrics(track.file.lyrics) && (
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
              <th className="num" aria-label="歌词与下载" />
            </tr>
          </thead>
          <tbody ref={body}>
            {shown.map((c) => (
              <tr key={c.id} data-flip={c.id} className={`cand v-${c.verdict}${track && c.id === track.chosen_candidate_id ? " is-chosen" : ""}`}>
                <td>
                  <PlayButton
                    playable={{
                      key: `c:${c.id}`,
                      url: c.preview_url,
                      title: c.title,
                      subtitle: c.needs_member ? `${c.provider_label}需会员 · 已换其他音源同版本试听` : `${c.artist} · ${c.provider_label} 试听`,
                      lyricsUrl: api.candidateLyricsUrl(c.id),
                      coverUrl: api.candidateCoverUrl(c.id),
                    }}
                  />
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
                  <div className="row-acts">
                    {lrcFor(c)}
                    {track && c.id === track.chosen_candidate_id && !track.file_missing && !["failed", "matched"].includes(track.status) ? (
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
                  </div>
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
  );
}

/* ---------- artist mode: the artist's popular songs, merged across catalogs ---------- */

function ArtistView({ s }: { s: SearchState }) {
  const toast = useToast();
  const lyricsDefault = useLyricsDefault();
  const { artist, songs, loading, session, lrc, picked } = s;
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [lyricsBusy, setLyricsBusy] = useState<string | null>(null);
  const body = useRef<HTMLTableSectionElement | null>(null);
  useFlipList(body, songs.map((x) => x.token).join(","), session, false);

  const sources = [...new Map(songs.flatMap((x) => x.providers).map((p) => [p.id, p.label])).values()];

  async function download(song: ArtistSong) {
    setPending((x) => new Set(x).add(song.token));
    try {
      const { track, job } = await api.downloadArtistSong(song.token, lrc[song.token] ?? lyricsDefault, searchActions.chosenProviders());
      searchActions.setPicked(song.token, track);
      if (job) upsertJob(job);
      else if (!track.file) {
        toast("info", `「${song.title}」有几个版本拿不准，去审核页挑一下`);
        navigate({ page: "review", id: track.playlist_id, track: track.id });
      }
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setPending((x) => {
        const next = new Set(x);
        next.delete(song.token);
        return next;
      });
    }
  }

  async function fillLyrics(token: string, t: Track) {
    setLyricsBusy(token);
    try {
      const next = await api.trackLyrics(t.id, true);
      searchActions.setPicked(token, next);
      const info = next.file?.lyrics;
      if (info?.refresh_error) toast("info", `没更新成功，沿用原来的歌词：${info.refresh_error}`);
      else if (hasLyrics(info)) toast("ok", `已保存歌词（${info!.source_label ?? "歌词"}）`);
      else toast("info", info?.error ?? "没有找到这首歌的歌词");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setLyricsBusy(null);
    }
  }

  return (
    <section className="stack gap-12">
      {artist && (
        <div className="artist-head fade-in">
          <ArtistAvatar name={artist.name} url={artist.cover_url ?? songs[0]?.cover_url ?? null} />
          <div className="grow">
            <span className="artist-kicker">歌手</span>
            <h2>{artist.name}</h2>
            <p className="muted small">
              {artist.aliases.length > 0 && <>{artist.aliases.join(" / ")} · </>}
              {songs.length ? `热门歌曲 ${songs.length} 首` : "正在整理热门歌曲…"}
              {sources.length > 0 && ` · 来自 ${sources.join("、")}`}
            </p>
          </div>
          <p className="artist-tip muted small">已去掉 Live、DJ、伴奏等版本。要找同名歌曲，搜「歌名 - {artist.name}」。</p>
        </div>
      )}

      <div className="card flush">
        <table className="table cand-table artist-table">
          <thead>
            <tr>
              <th className="num" aria-label="排名" />
              <th aria-label="试听" />
              <th>歌曲</th>
              <th>来源</th>
              <th className="num">时长</th>
              <th className="num" aria-label="歌词与下载" />
            </tr>
          </thead>
          <tbody ref={body}>
            {songs.map((song, i) => {
              const t = picked[song.token];
              const working = pending.has(song.token);
              return (
                <tr key={song.token} data-flip={song.token} className={`cand${t ? " is-chosen" : ""}`}>
                  <td className="num mono muted rank-no">{i + 1}</td>
                  <td>
                    <PlayButton
                      playable={{
                        key: `a:${song.token}`,
                        url: song.preview_url,
                        title: song.title,
                        subtitle: `${song.artist} · 试听`,
                        lyricsUrl: song.lyrics_url,
                        coverUrl: song.cover_url,
                      }}
                    />
                  </td>
                  <td>
                    <div className="song-with-cover">
                      <Thumb url={song.cover_url} />
                      <div className="song">
                        <span className="song-title">
                          {song.title} <Flags flags={song.flags} />
                        </span>
                        <span className="song-artist">
                          {song.artist}
                          {song.album && <span className="muted"> · 《{song.album}》</span>}
                        </span>
                      </div>
                    </div>
                  </td>
                  <td>
                    <div className="src">
                      <span>{song.providers.map((p) => p.label).join(" · ")}</span>
                      <span className="small muted">下载时挑最佳音质</span>
                    </div>
                  </td>
                  <td className="num mono">{fmtDuration(song.duration)}</td>
                  <td className="num">
                    <div className="row-acts">
                      {t?.file && !t.file_missing ? (
                        <LrcSwitch
                          on={hasLyrics(t.file.lyrics)}
                          busy={lyricsBusy === song.token}
                          onToggle={() => (hasLyrics(t.file?.lyrics) ? toast("info", "歌词已保存") : fillLyrics(song.token, t))}
                          title={hasLyrics(t.file.lyrics) ? "已保存歌词" : "这首还没有歌词，点击查找并保存"}
                        />
                      ) : (
                        <LrcSwitch
                          on={lrc[song.token] ?? lyricsDefault}
                          onToggle={() => searchActions.setLrc(song.token, !(lrc[song.token] ?? lyricsDefault))}
                          title={(lrc[song.token] ?? lyricsDefault) ? "下载时一起保存 .lrc 歌词（点击取消）" : "只下载音频（点击同时保存歌词）"}
                        />
                      )}
                      <ArtistSongAction song={song} track={t} working={working} onDownload={() => download(song)} />
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {loading && songs.length === 0 && <SkeletonRows />}
        {!loading && songs.length === 0 && <div className="empty-row muted">没有拿到这位歌手的歌曲列表，换个写法或打开更多音源试试。</div>}
      </div>
      {Object.keys(picked).length > 0 && (
        <p className="muted small">
          下载的歌保存在 <a href={href({ page: "playlist", id: Object.values(picked)[0].playlist_id })}>「单曲搜索」清单</a>。
        </p>
      )}
    </section>
  );
}

function ArtistSongAction({
  song,
  track,
  working,
  onDownload,
}: {
  song: ArtistSong;
  track: Track | undefined;
  working: boolean;
  onDownload: () => void;
}) {
  const toast = useToast();
  if (working)
    return (
      <span className="row-acts-note muted small">
        <LoaderCircle className="spin" size={13} /> 匹配中
      </span>
    );
  if (track?.file_missing && track.chosen_candidate_id != null)
    return (
      <button
        className="btn btn-sm btn-choose"
        title="文件已被移走或删除，按原来的版本再下一次"
        onClick={() =>
          api
            .batch([track.id], "redownload")
            .then(({ updated, skipped = [] }) => {
              if (updated[0]) searchActions.setPicked(song.token, updated[0]);
              if (skipped.length) toast("info", skipped[0].reason);
            })
            .catch((e) => toast("bad", e.message))
        }
      >
        <RotateCcw size={14} /> 重新下载
      </button>
    );
  if (track?.file)
    return (
      <button
        className="icon-btn"
        onClick={() => api.reveal(track.id).catch((e) => toast("bad", e.message))}
        title="已下载，在文件夹中显示"
        aria-label={`在文件夹中显示 ${song.title}`}
      >
        <FolderOpen size={15} />
      </button>
    );
  if (track && ["queued", "downloading", "searching"].includes(track.status)) return <StatusBadge status={track.status} />;
  if (track && ["needs_review", "not_found"].includes(track.status))
    return (
      <a className="btn btn-sm" href={href({ page: "review", id: track.playlist_id, track: track.id })}>
        <ListChecks size={14} /> 去审核
      </a>
    );
  return (
    <button
      className="btn btn-sm btn-choose"
      onClick={onDownload}
      title={track?.status === "failed" ? (track.error ?? "上次下载失败，再试一次") : undefined}
    >
      {track?.status === "failed" ? <RotateCcw size={14} /> : <Download size={14} />} {track?.status === "failed" ? "重试" : "下载"}
    </button>
  );
}

function ArtistAvatar({ name, url }: { name: string; url: string | null }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  return (
    <span className="artist-avatar" aria-hidden>
      {url && !failed ? <img src={url} alt="" onError={() => setFailed(true)} /> : <span>{name.slice(0, 1)}</span>}
    </span>
  );
}

function Thumb({ url }: { url: string }) {
  const [state, setState] = useState<"loading" | "ok" | "bad">("loading");
  return (
    <span className={`song-thumb is-${state}`} aria-hidden>
      {state !== "bad" && <img src={url} alt="" loading="lazy" onLoad={() => setState("ok")} onError={() => setState("bad")} />}
    </span>
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
