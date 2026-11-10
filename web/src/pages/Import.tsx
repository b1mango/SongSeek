import { useRef, useState } from "react";
import { ClipboardPaste, FileSpreadsheet, Link2, LoaderCircle, Play, TriangleAlert, Upload, X } from "lucide-react";
import { api, type ParseInput } from "../api";
import { useToast } from "../components";
import { fmtDuration } from "../format";
import { navigate } from "../router";
import { upsertJob, upsertPlaylist } from "../store";
import type { ColumnKey, ParseResult } from "../types";

type Mode = "file" | "text" | "url";

const COLUMN_LABEL: Record<ColumnKey, string> = { title: "歌名", artist: "歌手", album: "专辑", duration: "时长" };
const PREVIEW_LIMIT = 200;

export function ImportPage() {
  const toast = useToast();
  const [mode, setMode] = useState<Mode>("file");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [dragging, setDragging] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [lastInput, setLastInput] = useState<ParseInput | null>(null);
  const [name, setName] = useState("");
  const [removed, setRemoved] = useState<Set<number>>(new Set());
  const fileInput = useRef<HTMLInputElement>(null);

  async function parse(input: ParseInput, mapping?: ParseResult["mapping"]) {
    setParsing(true);
    try {
      const r = await api.parse(input, mapping);
      setResult(r);
      setLastInput(input);
      if (!mapping) {
        setName(r.name);
        setRemoved(new Set());
      }
      if (r.rows.length === 0) toast("bad", "没有解析出任何歌曲，检查一下格式或列对应关系");
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setParsing(false);
    }
  }

  function pickFile(f: File | undefined) {
    if (!f) return;
    setFile(f);
    parse({ kind: "file", file: f });
  }

  async function create(startSearch: boolean) {
    if (!result) return;
    const rows = result.rows.filter((_, i) => !removed.has(i));
    setCreating(true);
    try {
      const p = await api.createPlaylist(name.trim() || result.name, result.source, rows);
      upsertPlaylist(p);
      // Leave the import page first: if starting the search fails, retrying here would create a second copy.
      navigate({ page: "playlist", id: p.id });
      if (startSearch) upsertJob(await api.searchPlaylist(p.id));
    } catch (e) {
      toast("bad", (e as Error).message);
    } finally {
      setCreating(false);
    }
  }

  const kept = result ? result.rows.length - removed.size : 0;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>导入清单</h1>
          <p className="muted">支持 Excel、CSV、Markdown 表格、纯文本（每行一首），以及网易云公开歌单链接。</p>
        </div>
      </header>

      <div className="tabs" role="tablist">
        {(
          [
            ["file", "上传文件", FileSpreadsheet],
            ["text", "粘贴文本", ClipboardPaste],
            ["url", "歌单链接", Link2],
          ] as const
        ).map(([m, label, Icon]) => (
          <button key={m} role="tab" aria-selected={mode === m} className={`tab${mode === m ? " is-on" : ""}`} onClick={() => setMode(m)}>
            <Icon size={15} aria-hidden /> {label}
          </button>
        ))}
      </div>

      <section className="card import-input">
        {mode === "file" && (
          <div
            className={`drop${dragging ? " is-over" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              pickFile(e.dataTransfer.files[0]);
            }}
            onClick={() => fileInput.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === "Enter" && fileInput.current?.click()}
          >
            <Upload size={26} aria-hidden />
            <div className="drop-title">{file ? file.name : "把文件拖到这里，或点击选择"}</div>
            <div className="muted small">.xlsx · .csv · .md · .txt</div>
            <input
              ref={fileInput}
              type="file"
              accept=".xlsx,.csv,.md,.markdown,.txt"
              hidden
              onChange={(e) => {
                pickFile(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
          </div>
        )}

        {mode === "text" && (
          <div className="stack gap-12">
            <textarea
              className="input textarea mono-ish"
              rows={10}
              placeholder={"每行一首，例如：\n晴天 - 周杰伦\n后来 - 刘若英\n平凡之路"}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="row between">
              <span className="muted small">格式「歌名 - 歌手」；只写歌名也可以，会优先挑热门原唱，并标为待审核。</span>
              <button className="btn btn-primary" disabled={!text.trim() || parsing} onClick={() => parse({ kind: "text", text })}>
                {parsing && <LoaderCircle className="spin" size={16} />} 解析
              </button>
            </div>
          </div>
        )}

        {mode === "url" && (
          <form
            className="row gap-12"
            onSubmit={(e) => {
              e.preventDefault();
              if (url.trim()) parse({ kind: "url", url: url.trim() });
            }}
          >
            <input className="input grow" placeholder="粘贴网易云歌单链接或分享文字" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn btn-primary" disabled={!url.trim() || parsing}>
              {parsing && <LoaderCircle className="spin" size={16} />} 读取歌单
            </button>
          </form>
        )}
        {mode === "url" && (
          <div className="muted small">浏览器地址栏的歌单链接、App 里「分享 → 复制链接」得到的文字或短链接都可以直接粘贴；歌单需要是公开的。</div>
        )}
        {parsing && mode === "file" && (
          <div className="muted small row gap-8">
            <LoaderCircle className="spin" size={14} /> 正在解析…
          </div>
        )}
      </section>

      {result && (
        <section className="card import-preview">
          <div className="row between wrap gap-12">
            <label className="field grow">
              <span className="field-label">清单名称</span>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <div className="row gap-8 end">
              <button className="btn" disabled={!kept || creating} onClick={() => create(false)}>
                仅创建
              </button>
              <button className="btn btn-primary" disabled={!kept || creating} onClick={() => create(true)}>
                {creating ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />} 创建并开始搜索（{kept} 首）
              </button>
            </div>
          </div>

          {result.columns.length > 0 && lastInput && (
            <div className="mapping">
              <span className="field-label">列对应</span>
              <div className="mapping-grid">
                {(Object.keys(COLUMN_LABEL) as ColumnKey[]).map((k) => (
                  <label key={k} className="mapping-item">
                    <span>{COLUMN_LABEL[k]}</span>
                    <select
                      className="input select"
                      value={result.mapping[k] ?? ""}
                      onChange={(e) => parse(lastInput, { ...result.mapping, [k]: e.target.value || null })}
                    >
                      <option value="">（无）</option>
                      {result.columns.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            </div>
          )}

          {result.warnings.length > 0 && (
            <ul className="warnings">
              {result.warnings.slice(0, 8).map((w, i) => (
                <li key={i}>
                  <TriangleAlert size={14} aria-hidden /> {w}
                </li>
              ))}
              {result.warnings.length > 8 && <li className="muted">另有 {result.warnings.length - 8} 条提示</li>}
            </ul>
          )}

          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>歌名</th>
                  <th>歌手</th>
                  <th>专辑</th>
                  <th className="num">时长</th>
                  <th aria-label="操作" />
                </tr>
              </thead>
              <tbody>
                {result.rows.slice(0, PREVIEW_LIMIT).map((r, i) => (
                  <tr key={i} className={removed.has(i) ? "is-removed" : ""}>
                    <td className="num mono muted">{i + 1}</td>
                    <td className="strong">{r.title}</td>
                    <td>{r.artist || <span className="muted">未写</span>}</td>
                    <td className="muted">{r.album}</td>
                    <td className="num mono">{fmtDuration(r.duration)}</td>
                    <td className="num">
                      <button
                        className="icon-btn"
                        aria-label={removed.has(i) ? "恢复" : "移除"}
                        title={removed.has(i) ? "恢复" : "不导入这一行"}
                        onClick={() =>
                          setRemoved((s) => {
                            const n = new Set(s);
                            if (n.has(i)) n.delete(i);
                            else n.add(i);
                            return n;
                          })
                        }
                      >
                        <X size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.rows.length > PREVIEW_LIMIT && (
              <div className="table-more muted small">
                仅预览前 {PREVIEW_LIMIT} 行，共 {result.rows.length} 行，全部会被导入。
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
