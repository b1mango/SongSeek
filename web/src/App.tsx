import { useEffect } from "react";
import { CloudOff, FileUp, ListMusic, Moon, Search, Settings2, Sun, SunMoon } from "lucide-react";
import { api } from "./api";
import { ToastProvider, useToast } from "./components";
import { TitleBar } from "./desktop";
import { PlayerProvider } from "./player";
import { href, navigate, requestJobFocus, useRoute, type Route } from "./router";
import { refreshPlaylists, upsertJob, useLiveConnection, useStore } from "./store";
import { HomePage } from "./pages/Home";
import { ImportPage } from "./pages/Import";
import { PlaylistPage } from "./pages/Playlist";
import { ReviewPage } from "./pages/Review";
import { SearchPage } from "./pages/Search";
import { SettingsPage } from "./pages/Settings";
import { setThemePref, useThemePref } from "./theme";
import type { Job, ThemePref } from "./types";

export function App() {
  return (
    <ToastProvider>
      <PlayerProvider>
        <Shell />
      </PlayerProvider>
    </ToastProvider>
  );
}

function Shell() {
  useLiveConnection();
  const route = useRoute();
  const toast = useToast();
  useEffect(() => {
    refreshPlaylists().catch((e) => toast("bad", e.message));
  }, [toast]);

  return (
    <div className="shell">
      <TitleBar />
      <Sidebar route={route} />
      <main className="main">
        <div className="page-enter" key={route.page === "playlist" || route.page === "review" ? `${route.page}:${route.id}` : route.page}>
          <Page route={route} />
        </div>
      </main>
    </div>
  );
}

function Page({ route }: { route: Route }) {
  switch (route.page) {
    case "import":
      return <ImportPage />;
    case "search":
      return <SearchPage />;
    case "settings":
      return <SettingsPage />;
    case "playlist":
      return <PlaylistPage key={route.id} id={route.id} />;
    case "review":
      return <ReviewPage key={route.id} id={route.id} focus={route.track} />;
    default:
      return <HomePage />;
  }
}

function Sidebar({ route }: { route: Route }) {
  const playlists = useStore((s) => s.playlists);
  const jobs = useStore((s) => s.jobs);
  const connected = useStore((s) => s.connected);
  const running = jobs.filter((j) => j.status === "running");
  const activeId = route.page === "playlist" || route.page === "review" ? route.id : null;

  const nav = (r: Route, label: string, Icon: typeof Search, active: boolean) => (
    <a className={`nav-item${active ? " is-active" : ""}`} href={href(r)}>
      <Icon size={16} aria-hidden />
      {label}
    </a>
  );

  return (
    <aside className="sidebar">
      <a className="brand" href="#/">
        <img className="brand-mark" src="./logo.svg" alt="" aria-hidden draggable={false} />
        <span className="brand-name">觅音</span>
        <span className="brand-en">SongSeek</span>
      </a>

      <nav className="nav">
        {nav({ page: "import" }, "导入清单", FileUp, route.page === "import")}
        {nav({ page: "search" }, "单曲搜索", Search, route.page === "search")}
      </nav>

      <div className="side-section">
        <div className="side-label">
          <ListMusic size={13} aria-hidden /> 清单
        </div>
        {playlists.length === 0 && <div className="side-empty">还没有清单</div>}
        <ul className="side-lists">
          {playlists.map((p) => {
            const done = p.counts.done;
            const review = p.counts.needs_review;
            return (
              <li key={p.id}>
                <a className={`side-list${activeId === p.id ? " is-active" : ""}`} href={href({ page: "playlist", id: p.id })}>
                  <span className="side-list-name">{p.name}</span>
                  {review > 0 && (
                    <span className="side-pill tone-warn" title={`${review} 首待审核`}>
                      {review}
                    </span>
                  )}
                  <span className="side-list-meta mono">
                    {done}/{p.counts.total}
                  </span>
                  <span className="side-progress" style={{ width: `${p.counts.total ? (done / p.counts.total) * 100 : 0}%` }} />
                </a>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="side-foot">
        {running.map((j) => (
          <JobChip key={j.id} job={j} />
        ))}
        {!connected && (
          <div className="offline">
            <CloudOff size={14} aria-hidden /> 未连接本地服务
          </div>
        )}
        <div className="side-foot-row">
          {nav({ page: "settings" }, "音源与设置", Settings2, route.page === "settings")}
          <ThemeToggle />
        </div>
      </div>
    </aside>
  );
}

const THEMES: { pref: ThemePref; label: string; Icon: typeof Search }[] = [
  { pref: "system", label: "跟随系统", Icon: SunMoon },
  { pref: "light", label: "浅色", Icon: Sun },
  { pref: "dark", label: "深色", Icon: Moon },
];

/** Cycles 跟随系统 → 浅色 → 深色; the icon shows the current choice. */
function ThemeToggle() {
  const toast = useToast();
  const pref = useThemePref();
  const i = THEMES.findIndex((t) => t.pref === pref);
  const { label, Icon } = THEMES[i];
  const next = THEMES[(i + 1) % THEMES.length];
  return (
    <button
      className="icon-btn ghost theme-toggle"
      aria-label={`外观：${label}`}
      title={`外观：${label}（点击切换为${next.label}）`}
      onClick={() => setThemePref(next.pref).catch((e) => toast("bad", (e as Error).message))}
    >
      <Icon size={16} />
    </button>
  );
}

function JobChip({ job }: { job: Job }) {
  const toast = useToast();
  const pct = job.total ? Math.round((job.completed / job.total) * 100) : 0;
  const open = () => {
    // jobs without a playlist come from 单曲搜索
    if (job.playlist_id == null) return navigate({ page: "search" });
    requestJobFocus(job.playlist_id);
    navigate({ page: "playlist", id: job.playlist_id });
  };
  return (
    <div
      className="job-chip"
      role="button"
      tabIndex={0}
      title="查看进度"
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      }}
    >
      <div className="job-chip-head">
        <span>{job.kind === "search" ? "搜索中" : job.kind === "lyrics" ? "补歌词" : "下载中"}</span>
        <span className="mono">
          {job.completed}/{job.total}
        </span>
        <button
          className="link-btn"
          onClick={(e) => {
            e.stopPropagation();
            api
              .cancelJob(job.id)
              .then(upsertJob)
              .catch((err) => toast("bad", err.message));
          }}
          onKeyDown={(e) => e.stopPropagation()}
        >
          停止
        </button>
      </div>
      <div className="meter">
        <span style={{ width: `${pct}%` }} />
      </div>
      {job.current && <div className="job-chip-current">{job.current}</div>}
    </div>
  );
}
