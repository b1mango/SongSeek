import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Activity, CircleCheck, CircleX, FolderOpen, Globe, KeyRound, LoaderCircle, Radar, Save } from "lucide-react";
import { api } from "../api";
import { useToast } from "../components";
import { useSystem } from "../desktop";
import { setLyricsPref } from "../store";
import type { Provider, ProxyMode, ProxyStatus, ProxyTest, Settings } from "../types";

export function SettingsPage() {
  const toast = useToast();
  const [providers, setProviders] = useState<Provider[] | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [cookie, setCookie] = useState("");
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const system = useSystem();

  useEffect(() => {
    api
      .providers()
      .then(setProviders)
      .catch((e) => toast("bad", e.message));
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

  // Download options save themselves shortly after the last edit, like every other switch on this page.
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedOnce, setSavedOnce] = useState(false);
  const latest = useRef(draft);
  latest.current = draft;
  const dirty = !!settings && !!draft && JSON.stringify(editable(settings)) !== JSON.stringify(editable(draft));
  useEffect(() => {
    if (!dirty || !draft) return;
    const timer = setTimeout(async () => {
      setSaving(true);
      try {
        const s = await api.saveSettings(editable(draft));
        setSettings(s);
        // keep whatever the user typed meanwhile; only adopt the server's normalised values if nothing changed
        if (latest.current === draft) setDraft(s);
        setLyricsPref(s.lyrics ?? true);
        setSaveError(null);
        setSavedOnce(true);
      } catch (e) {
        setSaveError((e as Error).message);
      } finally {
        setSaving(false);
      }
    }, 700);
    return () => clearTimeout(timer);
  }, [dirty, draft]);

  async function saveCookie(value: string) {
    setSaving(true);
    try {
      const s = await api.saveSettings({ netease_cookie: value });
      setSettings(s);
      setDraft((d) => (d ? { ...d, netease_cookie_set: s.netease_cookie_set } : s));
      setCookie("");
      toast("ok", value ? "Cookie 已保存" : "Cookie 已清除");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setSaving(false);
    }
  }

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
            <div className="row gap-8">
              <input className="input mono grow" value={draft.output_dir} onChange={(e) => set("output_dir", e.target.value)} />
              {system?.desktop && (
                <button
                  type="button"
                  className="btn"
                  onClick={async (e) => {
                    e.preventDefault();
                    try {
                      const { path } = await api.pickFolder(draft.output_dir);
                      if (path) set("output_dir", path);
                    } catch (err) {
                      toast("bad", (err as Error).message);
                    }
                  }}
                >
                  <FolderOpen size={15} /> 选择…
                </button>
              )}
            </div>
          </label>
          <label className="field">
            <span className="field-label">文件名模板</span>
            <input className="input mono" value={draft.filename_template} onChange={(e) => set("filename_template", e.target.value)} />
            <span className="field-help">
              可用 {"{title}"} {"{artist}"} {"{album}"}，扩展名自动补上。
            </span>
          </label>
          <label className="check-line">
            <input type="checkbox" checked={draft.group_by_playlist} onChange={(e) => set("group_by_playlist", e.target.checked)} />
            按清单名建子文件夹
          </label>
          <label className="check-line">
            <input type="checkbox" checked={draft.auto_approve} onChange={(e) => set("auto_approve", e.target.checked)} />
            各项检查都通过的歌自动选定（关掉后，每首都要你确认）
          </label>
          <div className="check-group">
            <label className="check-line">
              <input type="checkbox" checked={draft.lyrics ?? true} onChange={(e) => set("lyrics", e.target.checked)} />
              默认一并保存歌词（同名 .lrc 文件，放在歌曲旁边；每首歌的 LRC 按钮可以单独开关）
            </label>
            <label className="check-line sub">
              <input type="checkbox" checked={draft.lyrics_translation ?? false} onChange={(e) => set("lyrics_translation", e.target.checked)} />
              外语歌附上中文翻译（有翻译时，每句原文下面跟一行译文）
            </label>
            <label className="check-line sub">
              <input type="checkbox" checked={draft.lyrics_embed ?? false} onChange={(e) => set("lyrics_embed", e.target.checked)} />
              同时把歌词写进音频文件（部分播放器只认内嵌歌词）
            </label>
          </div>
          <div className="grid-2">
            <label className="field">
              <span className="field-label">同时搜索</span>
              <input
                className="input mono"
                type="number"
                min={1}
                max={16}
                value={draft.search_concurrency}
                onChange={(e) => set("search_concurrency", Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span className="field-label">同时下载</span>
              <input
                className="input mono"
                type="number"
                min={1}
                max={8}
                value={draft.download_concurrency}
                onChange={(e) => set("download_concurrency", Number(e.target.value))}
              />
            </label>
          </div>
          <div className={`autosave small${saveError ? " tone-text-bad" : " muted"}`} aria-live="polite">
            {saveError ? (
              <>
                <CircleX size={13} /> 没有保存：{saveError}
              </>
            ) : saving || dirty ? (
              <>
                <LoaderCircle className="spin" size={13} /> 正在保存…
              </>
            ) : savedOnce ? (
              <>
                <CircleCheck size={13} /> 已自动保存
              </>
            ) : (
              "修改后自动保存"
            )}
          </div>
        </section>
      )}

      {settings && (
        <NetworkCard
          settings={settings}
          onSaved={(s) => {
            setSettings(s);
            setDraft((d) => (d ? { ...d, proxy: s.proxy, proxy_mode: s.proxy_mode } : s));
          }}
        />
      )}

      {settings && (
        <section className="card stack gap-12">
          <h2 className="row gap-8">
            <KeyRound size={17} /> 网易云 Cookie（可选）
          </h2>
          <p className="muted small">
            填写后可以下载会员歌曲和更高音质。Cookie 只保存在本机数据目录，界面和日志都不会显示。 当前状态：
            {settings.netease_cookie_set ? <b className="tone-text-ok">已设置</b> : <b>未设置</b>}
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
            <button className="btn" disabled={!cookie.trim() || saving} onClick={() => saveCookie(cookie.trim())}>
              保存
            </button>
            {settings.netease_cookie_set && (
              <button className="btn btn-ghost danger" disabled={saving} onClick={() => saveCookie("")}>
                清除
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

const MODES: { id: ProxyMode; title: string; hint: string }[] = [
  { id: "auto", title: "自动", hint: "跟随系统代理" },
  { id: "manual", title: "手动", hint: "填写代理地址" },
  { id: "off", title: "不用", hint: "全部直连" },
];

const SOURCE_LABEL = { system: "系统代理", env: "环境变量", manual: "手动填写" } as const;

/** Proxy for YouTube: follows the system by default, saves on every change. */
function NetworkCard({ settings, onSaved }: { settings: Settings; onSaved: (s: Settings) => void }) {
  const toast = useToast();
  const mode: ProxyMode = settings.proxy_mode ?? (settings.proxy ? "manual" : "auto");
  const [address, setAddress] = useState(settings.proxy);
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [test, setTest] = useState<ProxyTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(() => {
    api
      .proxy()
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);
  useEffect(refresh, [refresh, settings.proxy_mode, settings.proxy]);
  useEffect(() => setAddress(settings.proxy), [settings.proxy]);

  async function save(patch: { proxy_mode?: ProxyMode; proxy?: string }) {
    setSaving(true);
    setTest(null);
    try {
      onSaved(await api.saveSettings(patch));
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.testProxy(mode, mode === "manual" ? address.trim() : undefined));
    } catch (e) {
      setTest({ ok: false, proxy: null, latency_ms: null, error: (e as Error).message });
    } finally {
      setTesting(false);
    }
  }

  const addressDirty = address.trim() !== settings.proxy;

  return (
    <section className="card stack gap-16 net-card">
      <div className="row between section-head">
        <h2 className="row gap-8">
          <Globe size={17} /> 网络
        </h2>
        <button className="btn btn-sm" onClick={runTest} disabled={testing || saving || (mode === "manual" && !address.trim())}>
          {testing ? <LoaderCircle className="spin" size={14} /> : <Radar size={14} />} 测试 YouTube 连接
        </button>
      </div>
      <p className="muted small net-lead">只有 YouTube 需要代理。网易云、QQ、酷狗等国内音源始终直连，走代理反而容易被限流。</p>

      <div className="mode-picker" role="radiogroup" aria-label="代理方式">
        {MODES.map((m) => (
          <button
            key={m.id}
            role="radio"
            aria-checked={mode === m.id}
            className={`mode-option${mode === m.id ? " is-on" : ""}`}
            disabled={saving}
            onClick={() => mode !== m.id && save({ proxy_mode: m.id })}
          >
            <span className="mode-title">{m.title}</span>
            <span className="mode-hint">{m.hint}</span>
          </button>
        ))}
      </div>

      {mode === "auto" && <AutoStatus status={status} onUse={(p) => save({ proxy_mode: "manual", proxy: p })} />}

      {mode === "manual" && (
        <form
          className="row gap-8"
          onSubmit={(e) => {
            e.preventDefault();
            if (addressDirty) save({ proxy: address.trim() });
          }}
        >
          <input
            className="input mono grow"
            placeholder="例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            aria-label="代理地址"
          />
          <button className="btn" disabled={!addressDirty || saving}>
            {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} 保存
          </button>
        </form>
      )}

      {mode === "off" && (
        <div className="net-status">
          <span className="dot tone-idle" />
          <span>所有音源直连。代理软件开了全局或 TUN 模式时，选这个即可。</span>
        </div>
      )}

      {test && (
        <div className={`net-test ${test.ok ? "is-ok" : "is-bad"}`} role="status">
          {test.ok ? <CircleCheck size={16} /> : <CircleX size={16} />}
          <span>
            {test.ok ? "YouTube 可以连通" : `连不上 YouTube${test.error ? "：" + test.error : ""}`}
            {test.latency_ms != null && <span className="mono"> · {test.latency_ms} ms</span>}
          </span>
          <span className="net-test-via mono">{test.proxy ? `经 ${test.proxy}` : "直连"}</span>
        </div>
      )}
    </section>
  );
}

function AutoStatus({ status, onUse }: { status: ProxyStatus | null; onUse: (proxy: string) => void }) {
  if (!status)
    return (
      <div className="net-status">
        <LoaderCircle className="spin" size={14} />
        <span className="muted">正在读取系统代理…</span>
      </div>
    );
  if (status.effective)
    return (
      <div className="net-status">
        <span className="dot tone-ok" />
        <span>已检测到{status.source ? SOURCE_LABEL[status.source] : "代理"}</span>
        <code className="net-addr">{status.effective}</code>
      </div>
    );
  return (
    <div className="stack gap-8">
      <div className="net-status">
        <span className="dot tone-warn" />
        <span>{status.pac ? "系统用的是自动配置脚本（PAC），暂不支持读取，YouTube 会直连。" : "没有检测到系统代理，YouTube 会直连。"}</span>
      </div>
      {status.local_candidates.length > 0 && (
        <div className="net-suggest">
          <span className="muted small">本机这些端口像是代理软件，但没设成系统代理：</span>
          <div className="row gap-8 wrap">
            {status.local_candidates.map((p) => (
              <button key={p} className="chip-btn mono" onClick={() => onUse(p)} title="改为手动并使用这个地址">
                {p.replace(/^https?:\/\//, "")} · 使用
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** The fields the download card edits; cookie, proxy and theme have their own controls and must not be overwritten from a stale draft. */
function editable(s: Settings) {
  const { netease_cookie_set: _c, proxy: _p, proxy_mode: _m, theme: _t, ...rest } = s;
  return rest;
}
