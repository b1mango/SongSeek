import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Activity, KeyRound, LoaderCircle, Save } from "lucide-react";
import { api } from "../api";
import { useToast } from "../components";
import type { Provider, Settings } from "../types";

export function SettingsPage() {
  const toast = useToast();
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [cookie, setCookie] = useState("");
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.providers().then(setProviders).catch((e) => toast("bad", e.message));
    api
      .settings()
      .then((s) => {
        setSettings(s);
        setDraft(s);
      })
      .catch((e) => toast("bad", e.message));
  }, [toast]);

  async function saveProviders(next: Provider[]) {
    const ordered = next.map((p, i) => ({ ...p, order: i + 1 }));
    setProviders(ordered);
    try {
      setProviders(await api.saveProviders(ordered.map(({ id, enabled, order }) => ({ id, enabled, order }))));
    } catch (e) {
      toast("bad", (e as Error).message);
    }
  }

  function move(i: number, dir: -1 | 1) {
    if (!providers) return;
    const next = providers.slice();
    const j = i + dir;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    saveProviders(next);
  }

  async function check() {
    setChecking(true);
    try {
      setProviders(await api.checkProviders());
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setChecking(false);
    }
  }

  async function save(extra?: { netease_cookie?: string }) {
    if (!draft) return;
    setSaving(true);
    try {
      const { netease_cookie_set: _ignored, ...patch } = draft;
      const s = await api.saveSettings({ ...patch, ...extra });
      setSettings(s);
      setDraft(s);
      setCookie("");
      toast("ok", "设置已保存");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const dirty = settings && draft && JSON.stringify(settings) !== JSON.stringify(draft);
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d));

  return (
    <div className="page narrow">
      <header className="page-head">
        <div>
          <h1>音源与设置</h1>
          <p className="muted">下载时按从上到下的顺序尝试音源；排在前面的音源在同分时也会优先。</p>
        </div>
      </header>

      <section className="card">
        <div className="row between section-head">
          <h2>音源</h2>
          <button className="btn btn-sm" onClick={check} disabled={checking}>
            {checking ? <LoaderCircle className="spin" size={14} /> : <Activity size={14} />} 检查可用性
          </button>
        </div>
        {!providers ? (
          <LoaderCircle className="spin" size={18} />
        ) : (
          <ol className="providers">
            {providers.map((p, i) => (
              <li key={p.id} className={`provider${p.enabled ? "" : " is-off"}`}>
                <span className="mono muted provider-order">{i + 1}</span>
                <div className="provider-main">
                  <div className="provider-name">
                    {p.label}
                    <span className="mono small muted">{p.max_quality}</span>
                    {p.needs === "cookie" && <span className="flag">会员歌曲需 Cookie</span>}
                    {p.needs === "proxy" && <span className="flag">通常需要代理</span>}
                  </div>
                  <div className="provider-health small">
                    {p.health ? (
                      <span className={p.health.ok ? "tone-text-ok" : "tone-text-bad"}>
                        <span className={`dot tone-${p.health.ok ? "ok" : "bad"}`} />
                        {p.health.ok ? "可用" : "不可用"}
                        {p.health.latency_ms != null && ` · ${p.health.latency_ms} ms`}
                        {p.health.message && ` · ${p.health.message}`}
                      </span>
                    ) : (
                      <span className="muted">尚未检查</span>
                    )}
                  </div>
                </div>
                <div className="row gap-4">
                  <button className="icon-btn" disabled={i === 0} onClick={() => move(i, -1)} aria-label="上移">
                    <ArrowUp size={15} />
                  </button>
                  <button className="icon-btn" disabled={i === providers.length - 1} onClick={() => move(i, 1)} aria-label="下移">
                    <ArrowDown size={15} />
                  </button>
                  <label className="switch" title={p.enabled ? "已启用" : "已停用"}>
                    <input
                      type="checkbox"
                      checked={p.enabled}
                      onChange={() => saveProviders(providers.map((x) => (x.id === p.id ? { ...x, enabled: !x.enabled } : x)))}
                      aria-label={`启用 ${p.label}`}
                    />
                    <span />
                  </label>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      {draft && (
        <section className="card stack gap-16">
          <h2>下载</h2>
          <label className="field">
            <span className="field-label">保存到</span>
            <input className="input mono" value={draft.output_dir} onChange={(e) => set("output_dir", e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">文件名模板</span>
            <input className="input mono" value={draft.filename_template} onChange={(e) => set("filename_template", e.target.value)} />
            <span className="field-help">可用 {"{title}"} {"{artist}"} {"{album}"}，扩展名自动补上。</span>
          </label>
          <label className="check-line">
            <input type="checkbox" checked={draft.group_by_playlist} onChange={(e) => set("group_by_playlist", e.target.checked)} />
            按清单名建子文件夹
          </label>
          <label className="check-line">
            <input type="checkbox" checked={draft.auto_approve} onChange={(e) => set("auto_approve", e.target.checked)} />
            各项检查都通过的歌自动选定（关掉后，每首都要你确认）
          </label>
          <div className="grid-2">
            <label className="field">
              <span className="field-label">同时搜索</span>
              <input className="input mono" type="number" min={1} max={16} value={draft.search_concurrency} onChange={(e) => set("search_concurrency", Number(e.target.value))} />
            </label>
            <label className="field">
              <span className="field-label">同时下载</span>
              <input className="input mono" type="number" min={1} max={8} value={draft.download_concurrency} onChange={(e) => set("download_concurrency", Number(e.target.value))} />
            </label>
          </div>
          <label className="field">
            <span className="field-label">代理</span>
            <input className="input mono" placeholder="留空表示直连，例如 http://127.0.0.1:7890" value={draft.proxy} onChange={(e) => set("proxy", e.target.value)} />
            <span className="field-help">只有 YouTube 走代理；网易云、QQ、酷狗等国内音源始终直连（走代理反而容易被限流）。</span>
          </label>
          <div className="row end">
            <button className="btn btn-primary" disabled={!dirty || saving} onClick={() => save()}>
              {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} 保存设置
            </button>
          </div>
        </section>
      )}

      {settings && (
        <section className="card stack gap-12">
          <h2 className="row gap-8">
            <KeyRound size={17} /> 网易云 Cookie（可选）
          </h2>
          <p className="muted small">
            填写后可以下载会员歌曲和更高音质。Cookie 只保存在本机数据目录，界面和日志都不会显示。
            当前状态：{settings.netease_cookie_set ? <b className="tone-text-ok">已设置</b> : <b>未设置</b>}
          </p>
          <div className="row gap-8">
            <input
              className="input mono grow"
              type="password"
              autoComplete="off"
              placeholder="粘贴 MUSIC_U=… 开头的 Cookie"
              value={cookie}
              onChange={(e) => setCookie(e.target.value)}
            />
            <button className="btn" disabled={!cookie.trim() || saving} onClick={() => save({ netease_cookie: cookie.trim() })}>
              保存
            </button>
            {settings.netease_cookie_set && (
              <button className="btn btn-ghost danger" disabled={saving} onClick={() => save({ netease_cookie: "" })}>
                清除
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
