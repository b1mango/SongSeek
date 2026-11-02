import { ArrowRight, FileUp, Search } from "lucide-react";
import { SOURCE_LABEL } from "../format";
import { href } from "../router";
import { StatusStrip } from "../StatusStrip";
import { useStore } from "../store";

export function HomePage() {
  const playlists = useStore((s) => s.playlists);

  if (playlists.length === 0) {
    return (
      <div className="page home-empty">
        <h1 className="display">把一份歌单，变成一个文件夹。</h1>
        <p className="lede">导入清单或直接搜索。觅音会同时查多个音源、核对歌名、歌手、时长和版本，拿不准的交给你挑，确认后下载并校验每一个文件。</p>
        <ol className="steps">
          <li>
            <b>导入</b>
            <span>Excel、CSV、Markdown 表格、粘贴文本或网易云歌单链接</span>
          </li>
          <li>
            <b>审核</b>
            <span>只看拿不准的那几首，试听对比，一键换版本</span>
          </li>
          <li>
            <b>下载</b>
            <span>全曲解码校验、写好标签，失败自动换下一个音源</span>
          </li>
        </ol>
        <div className="row gap-12">
          <a className="btn btn-primary btn-lg" href={href({ page: "import" })}>
            <FileUp size={18} /> 导入清单
          </a>
          <a className="btn btn-lg" href={href({ page: "search" })}>
            <Search size={18} /> 搜一首歌
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>清单</h1>
          <p className="muted">{playlists.length} 份清单</p>
        </div>
        <a className="btn btn-primary" href={href({ page: "import" })}>
          <FileUp size={16} /> 导入清单
        </a>
      </header>
      <div className="cards">
        {playlists.map((p) => {
          const review = p.counts.needs_review;
          return (
            <a key={p.id} className="card pl-card" href={href({ page: "playlist", id: p.id })}>
              <div className="pl-card-head">
                <span className="pl-card-name">{p.name}</span>
                <span className="muted small">{SOURCE_LABEL[p.source] ?? p.source}</span>
              </div>
              <div className="pl-card-figure">
                <span className="mono big">{p.counts.done}</span>
                <span className="muted">/ {p.counts.total} 已完成</span>
              </div>
              <StatusStrip counts={p.counts} compact />
              <div className="pl-card-foot">
                {review > 0 ? <span className="tone-text-warn">{review} 首待审核</span> : <span className="muted">无待审核</span>}
                <ArrowRight size={16} aria-hidden />
              </div>
            </a>
          );
        })}
      </div>
    </div>
  );
}
