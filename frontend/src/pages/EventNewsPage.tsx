import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { formatJst } from "../utils/date";
import {
  fetchEventNews,
  fetchEventNewsDetail,
  type EventNewsDetail,
  type EventNewsFact,
} from "../api/eventnews";

const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

const STATUS_LABEL: Record<string, string> = {
  new: "新規",
  updated: "更新",
  reinforced: "補強",
  dormant: "沈静",
};

/** 裏取りの表示 — 記事数ではなく「独立媒体数」で表し、未分類を 0 と見せない。 */
function SourceBadge({ item }: { item: { independent_sources: number; state_media_count: number; unclassified_sources: number } }) {
  const parts: string[] = [];
  if (item.state_media_count > 0) parts.push(`国営 ${item.state_media_count}`);
  if (item.unclassified_sources > 0) parts.push(`未分類 ${item.unclassified_sources}`);
  const solo = item.independent_sources <= 1;
  return (
    <span className={`text-xs ${solo ? "text-warning" : "text-fg-muted"}`} title={solo ? "裏取りがまだ無い単独報" : undefined}>
      {solo ? "1 媒体のみ・未裏取り" : `独立 ${item.independent_sources} 媒体`}
      {parts.length > 0 && <span className="text-fg-subtle">（{parts.join("・")}）</span>}
    </span>
  );
}

/** facts を段落へまとめ、文末に控えめな出典番号を置く (クリックで原文へ)。 */
function Body({ facts, onCite }: { facts: EventNewsFact[]; onCite: (n: number) => void }) {
  const paras = new Map<number, EventNewsFact[]>();
  for (const f of facts) {
    const k = f.paragraph || 1;
    if (!paras.has(k)) paras.set(k, []);
    paras.get(k)!.push(f);
  }
  return (
    <>
      {[...paras.keys()].sort((a, b) => a - b).map((k) => (
        <p key={k} className="text-fg text-[15px] leading-[1.95] max-w-[46em] mb-3">
          {paras.get(k)!.map((f, i) => (
            <span key={i}>
              {f.text}
              {f.source_index > 0 && (
                <button
                  onClick={() => onCite(f.source_index)}
                  title={`出典 [${f.source_index}] を開く`}
                  className="align-super text-[10px] font-mono text-fg-subtle hover:text-accent ml-0.5 mr-0.5"
                >
                  [{f.source_index}]
                </button>
              )}
            </span>
          ))}
        </p>
      ))}
    </>
  );
}

function Detail({ id }: { id: string }) {
  const [openMember, setOpenMember] = useState<number | null>(null);
  const { data, isFetching, error } = useQuery({
    queryKey: ["eventnews", id],
    queryFn: () => fetchEventNewsDetail(id),
  });

  if (isFetching && !data) return <div className="text-fg-subtle text-sm">読み込み中…</div>;
  if (error) return <div className="text-critical text-sm">エラー: {String(error)}</div>;
  if (!data) return null;
  const d: EventNewsDetail = data;

  return (
    <div className="space-y-4">
      {d.news ? (
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-5">
          <h3 className="m-0 text-lg font-bold text-fg leading-snug text-balance">{d.news.headline}</h3>
          <div className="flex flex-wrap gap-3 items-center text-xs text-fg-muted mt-1.5 mb-4">
            <SourceBadge item={d} />
            <span>{d.members.length} 記事</span>
            <span>{formatJst(d.first_reported_at)} → {formatJst(d.last_reported_at)}</span>
            <span className={IMPORTANCE_TONE[d.importance] ?? ""}>{d.importance}</span>
            <span className="text-fg-subtle">v{d.news.version} · {d.news.model}</span>
          </div>
          <p className="border-l-[3px] border-accent pl-3.5 text-fg text-[15px] leading-relaxed max-w-[46em] mb-4">
            {d.news.bluf}
          </p>
          <Body facts={d.news.facts} onCite={(n) => setOpenMember(n)} />
          {d.news.discrepancies.length > 0 && (
            <>
              <h4 className="text-xs uppercase tracking-wider text-fg-muted font-medium mt-5 mb-2">ソース間の相違</h4>
              <ul className="list-disc pl-5 max-w-[46em] text-fg text-sm space-y-1">
                {d.news.discrepancies.map((x, i) => <li key={i}>{x.text}</li>)}
              </ul>
            </>
          )}
          {d.news.unknowns.length > 0 && (
            <>
              <h4 className="text-xs uppercase tracking-wider text-fg-muted font-medium mt-5 mb-2">未確認・不明</h4>
              <ul className="list-disc pl-5 max-w-[46em] text-fg text-sm space-y-1">
                {d.news.unknowns.map((x, i) => <li key={i}>{x}</li>)}
              </ul>
            </>
          )}
          <p className="text-xs text-fg-subtle mt-5 pt-3 border-t border-border-subtle">{d.note}</p>
        </div>
      ) : (
        /* 単独記事 (案 A): 生成はせず、原記事の要約を **同じ枠** で読ませる。
           枠を揃えることで読む場所が 1 つに保たれる。 */
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-5">
          <h3 className="m-0 text-lg font-bold text-fg leading-snug text-balance">
            {d.members[0]?.title ?? "(記事なし)"}
          </h3>
          <div className="flex flex-wrap gap-3 items-center text-xs text-fg-muted mt-1.5 mb-4">
            <SourceBadge item={d} />
            <span>{d.members[0]?.feed_title}</span>
            <span>{formatJst(d.first_reported_at)}</span>
            <span className={IMPORTANCE_TONE[d.importance] ?? ""}>{d.importance}</span>
          </div>
          <p className="text-fg text-[15px] leading-[1.95] max-w-[46em] whitespace-pre-wrap">
            {d.members[0]?.summary}
          </p>
          <p className="text-xs text-fg-subtle mt-5 pt-3 border-t border-border-subtle">
            1 媒体のみの報道のため、記事の要約をそのまま表示している。他媒体が報じると事象として統合され本文が生成される。
          </p>
        </div>
      )}

      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4">
        <h4 className="text-xs uppercase tracking-wider text-fg-muted font-medium mb-2">
          原記事 {d.members.length} 件（[N] は本文の出典番号）
        </h4>
        <div className="space-y-1.5">
          {d.members.map((m) => (
            <details key={m.article_id} open={openMember === m.index} className="border border-border-subtle rounded">
              <summary className="px-3 py-2 cursor-pointer flex gap-2 items-baseline text-sm">
                <span className="font-mono text-xs text-accent shrink-0">[{m.index}]</span>
                <span className="text-fg">{m.title}</span>
                <span className="ml-auto text-xs text-fg-subtle shrink-0">
                  {m.feed_title} · {m.source_tier}
                </span>
              </summary>
              <div className="px-3 pb-3 pt-1 text-sm text-fg-muted border-t border-dashed border-border-subtle">
                {m.summary}
                <div className="mt-2">
                  <a href={m.url} target="_blank" rel="noopener noreferrer" className="text-accent text-xs">原記事 ↗</a>
                </div>
              </div>
            </details>
          ))}
        </div>
      </div>
    </div>
  );
}

const IMPORTANCE_FILTERS: { key: string; label: string }[] = [
  { key: "high", label: "high のみ" },
  { key: "high,medium", label: "high + medium" },
  { key: "", label: "すべて" },
];

export function EventNewsPage() {
  const [selected, setSelected] = useState<string | null>(null);
  const [imp, setImp] = useState("high,medium");
  const { data, isFetching, error } = useQuery({
    queryKey: ["eventnews-list", imp],
    queryFn: () => fetchEventNews(80, imp || undefined),
    refetchInterval: 5 * 60 * 1000,
  });

  const items = data?.items ?? [];
  const current = selected ?? items.find((i) => i.has_news)?.id ?? items[0]?.id ?? null;

  return (
    <div className={`${pageContainer("wide")} space-y-5`}>
      <div>
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">事象ニュース</h2>
        <p className="text-fg-muted text-sm mt-1">
          収集した記事を事象単位で読む。複数媒体が報じた事象は 1 本に統合して生成し、新しい記事が加わると更新される。
          単独報はその記事の要約をそのまま表示する。
        </p>
      </div>

      <div className="flex gap-1.5">
        {IMPORTANCE_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => { setImp(f.key); setSelected(null); }}
            className={`text-xs px-3 py-1.5 rounded border transition-colors ${
              imp === f.key ? "border-accent text-accent bg-accent/10" : "border-border-default text-fg-muted hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {isFetching && !data && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">エラー: {String(error)}</div>}

      {items.length === 0 && data && (
        <div className="text-fg-muted text-sm bg-surface-1 border border-border-subtle rounded-lg p-4">
          まだ事象がありません。複数媒体が同じ事象を報じると、ここに現れます。
        </div>
      )}

      {items.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-4 items-start">
          <nav className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden lg:sticky lg:top-3 max-h-[calc(100vh-2rem)] overflow-y-auto">
            {items.map((it) => (
              <button
                key={it.id}
                onClick={() => setSelected(it.id)}
                className={`w-full text-left px-3 py-2.5 border-b border-border-subtle hover:bg-surface-2 transition-colors ${
                  current === it.id ? "bg-surface-2" : ""
                }`}
              >
                <div className="flex items-center gap-2 text-[11px] mb-0.5">
                  <span className={IMPORTANCE_TONE[it.importance] ?? "text-fg-subtle"}>{it.importance}</span>
                  <span className="text-fg-subtle">{STATUS_LABEL[it.status] ?? it.status}</span>
                  <span className="ml-auto text-fg-subtle">{formatJst(it.last_reported_at)}</span>
                </div>
                <div className="text-sm text-fg leading-snug">{it.headline}</div>
                {it.member_count > 1 && (
                  <div className="text-[11px] text-fg-subtle mt-0.5">{it.member_count} 記事を統合</div>
                )}
                <div className="mt-0.5"><SourceBadge item={it} /></div>
              </button>
            ))}
          </nav>
          <main className="min-w-0">{current && <Detail id={current} />}</main>
        </div>
      )}
    </div>
  );
}
