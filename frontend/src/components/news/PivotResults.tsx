// 逆引き (pivot) ビュー: 1 つの entity (IOC/CVE/actor/malware 等) を参照する記事と、
// それらに共起する entity を表示する。共起パネルの click で再 pivot できる
// (IOC→アクター帰属 / infrastructure 相関の起点)。NewsPage の pivot ビューで使う。

import { formatJst } from "../../utils/date";
import { type PivotResponse } from "../../api/pivot";
import { useChannelMeta } from "../channel";
import { vocabLabel } from "../../hooks/useVocab";

const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

export function PivotResults({ data, onPivot }: { data: PivotResponse; onPivot: (t: string, v: string) => void }) {
  const chMeta = useChannelMeta();
  return (
    <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px] gap-5 items-start">
      <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <div className="px-4 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">
          参照記事 {data.article_count} 件 — {vocabLabel("entity_type", data.entity.type)}:
          <span className="font-mono text-accent ml-1">{data.entity.value}</span>
        </div>
        {data.articles.length === 0 && (<div className="px-4 py-6 text-center text-fg-subtle text-sm">該当記事なし</div>)}
        <ul className="divide-y divide-border-subtle">
          {data.articles.map((a) => (
            <li key={a.article_id} className="px-4 py-2.5 hover:bg-surface-2 transition-colors">
              <a href={`/app/article/${encodeURIComponent(a.article_id)}`} className="text-sm text-fg hover:text-accent block">{a.title}</a>
              <div className="flex flex-wrap items-center gap-2 mt-1 text-xs">
                <span className={`font-medium ${IMPORTANCE_TONE[a.importance] || "text-fg-subtle"}`}>{vocabLabel("importance", a.importance)}</span>
                {a.category && <span className="text-fg-subtle">{vocabLabel("category", a.category)}</span>}
                {a.posted_channel && <span className="text-fg-subtle">{chMeta(a.posted_channel).label}</span>}
                {a.created_at && <span className="text-fg-subtle ml-auto tnum">{formatJst(a.created_at)}</span>}
              </div>
            </li>
          ))}
        </ul>
      </div>
      <div className="space-y-3">
        {data.related.length === 0 && (<div className="bg-surface-1 border border-border-subtle rounded-lg px-4 py-6 text-center text-fg-subtle text-sm">共起エンティティなし</div>)}
        {data.related.map((g) => (
          <div key={g.type} className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
            <div className="px-3 py-1.5 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">{vocabLabel("entity_type", g.type)}</div>
            <div className="p-2 flex flex-wrap gap-1.5">
              {g.items.map((it) => (
                <button key={it.value} onClick={() => onPivot(g.type, it.value)} title={`${it.value} で再 pivot`}
                  className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted hover:text-accent hover:border-accent-soft transition-colors">
                  <span className="font-mono">{it.value}</span>
                  <span className="text-fg-subtle tnum">{it.count}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
