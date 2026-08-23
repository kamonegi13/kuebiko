import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { Drawer } from "../components/Drawer";
import { formatJstCompact, formatJst } from "../utils/date";
import {
  fetchEventNews,
  fetchEventNewsDetail,
  type EventNewsDetail,
  type EventNewsFact,
  type EventNewsListItem,
} from "../api/eventnews";

const IMPORTANCE_FILTERS: { key: string; label: string }[] = [
  { key: "high", label: "high のみ" },
  { key: "high,medium", label: "high + medium" },
  { key: "", label: "すべて" },
];

/** 裏取り = 独立媒体数。記事数では表さない (docs/event_news_design.md §3-3)。 */
function SourceChip({ item }: { item: Pick<EventNewsListItem, "independent_sources" | "state_media_count" | "unclassified_sources"> }) {
  const solo = item.independent_sources <= 1;
  return (
    <span
      className={`px-1 rounded ${solo ? "bg-warning-soft text-warning" : "bg-surface-2 text-fg-muted"}`}
      title={solo ? "裏取りがまだ無い単独報" : "同一事象を報じた独立媒体の数"}
    >
      {solo ? "1 媒体のみ" : `独立 ${item.independent_sources} 媒体`}
      {item.state_media_count > 0 && <span className="text-critical"> ・国営 {item.state_media_count}</span>}
    </span>
  );
}

/** facts を段落へまとめ、文末に控えめな出典番号を置く。
 *
 * 出典番号は **素の記事リンク** (`/app/article/:id`) にする。ArticlePeek の
 * グローバル・クリックインターセプトがこれを捕捉して右ドロワーで開くため、
 * 履歴統合 (バックで閉じる) や再入 guard をこちらで再実装しなくて済む。
 */
function Body({ facts, articleIdOf }: { facts: EventNewsFact[]; articleIdOf: (n: number) => string | undefined }) {
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
              {f.source_index > 0 && articleIdOf(f.source_index) && (
                <a
                  href={`/app/article/${encodeURIComponent(articleIdOf(f.source_index)!)}`}
                  title={`出典 [${f.source_index}] を開く`}
                  className="align-super text-[10px] font-mono text-fg-subtle hover:text-accent ml-0.5 mr-0.5 no-underline"
                >
                  [{f.source_index}]
                </a>
              )}
            </span>
          ))}
        </p>
      ))}
    </>
  );
}

function DetailBody({ id }: { id: string }) {
  const { data, isFetching, error } = useQuery({
    queryKey: ["eventnews", id],
    queryFn: () => fetchEventNewsDetail(id),
  });

  if (isFetching && !data) return <div className="text-fg-subtle text-sm">読み込み中…</div>;
  if (error) return <div className="text-critical text-sm">エラー: {String(error)}</div>;
  if (!data) return null;
  const d: EventNewsDetail = data;
  const articleIdOf = (n: number) => d.members.find((m) => m.index === n)?.article_id;

  return (
    <div className="space-y-4">
      {d.news ? (
        <div>
          <div className="flex flex-wrap gap-2 items-center text-[11px] text-fg-muted mb-3">
            <SourceChip item={d} />
            <span>{d.members.length} 記事を統合</span>
            <span>{formatJst(d.first_reported_at)} → {formatJst(d.last_reported_at)}</span>
            <span className="text-fg-subtle">v{d.news.version} · {d.news.model}</span>
          </div>
          <p className="border-l-[3px] border-accent pl-3.5 text-fg text-[15px] leading-relaxed max-w-[46em] mb-4">
            {d.news.bluf}
          </p>
          <Body facts={d.news.facts} articleIdOf={articleIdOf} />
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
        /* 単独記事 (案 A): 生成せず、原記事の要約を同じ枠で読ませる。 */
        <div>
          <div className="flex flex-wrap gap-2 items-center text-[11px] text-fg-muted mb-3">
            <SourceChip item={d} />
            <span>{d.members[0]?.feed_title}</span>
            <span>{formatJst(d.first_reported_at)}</span>
          </div>
          <p className="text-fg text-[15px] leading-[1.95] max-w-[46em] whitespace-pre-wrap">
            {d.members[0]?.summary}
          </p>
          <p className="text-xs text-fg-subtle mt-5 pt-3 border-t border-border-subtle">
            1 媒体のみの報道のため、記事の要約をそのまま表示している。他媒体が報じると事象として統合され本文が生成される。
          </p>
        </div>
      )}

      <div className="border-t border-border-subtle pt-3">
        <h4 className="text-xs uppercase tracking-wider text-fg-muted font-medium mb-2">
          原記事 {d.members.length} 件{d.news && "（[N] は本文の出典番号）"}
        </h4>
        <ul className="space-y-1">
          {d.members.map((m) => (
            <li
              key={m.article_id}
              className="flex items-start gap-2 border border-border-subtle rounded px-3 py-2"
            >
              <span className="font-mono text-xs text-accent shrink-0 mt-0.5">[{m.index}]</span>
              <div className="flex-1 min-w-0">
                {/* 素の記事リンク = ArticlePeek のインターセプトが右ドロワーで開く */}
                <a
                  href={`/app/article/${encodeURIComponent(m.article_id)}`}
                  className="block text-sm text-fg hover:text-accent hover:underline leading-snug"
                  title="この記事を開く"
                >
                  {m.title}
                </a>
                <div className="text-[11px] text-fg-subtle flex flex-wrap items-center gap-x-2 mt-0.5">
                  <span>{m.feed_title}</span>
                  <span>{m.source_tier}</span>
                  {m.contributed_new_facts && <span className="text-accent">新しい事実を追加</span>}
                  <a
                    href={m.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    data-peek-ignore
                    className="hover:text-accent"
                  >
                    元記事 ↗
                  </a>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function EventNewsPage() {
  const [openId, setOpenId] = useState<string | null>(null);
  const [imp, setImp] = useState("high,medium");
  const { data, isFetching, error } = useQuery({
    queryKey: ["eventnews-list", imp],
    queryFn: () => fetchEventNews(80, imp || undefined),
    refetchInterval: 5 * 60 * 1000,
  });

  const items = data?.items ?? [];
  const openItem = items.find((i) => i.id === openId) ?? null;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div>
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">事象ニュース</h2>
        <p className="text-fg-muted text-sm mt-1">
          収集した記事を事象単位で読む。複数媒体が報じた事象は 1 本に統合して生成し、新しい記事が加わると更新される。単独報はその記事の要約を表示する。
        </p>
      </div>

      <div className="flex gap-1.5">
        {IMPORTANCE_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setImp(f.key)}
            className={`text-xs px-3 py-1.5 rounded border transition-colors ${
              imp === f.key ? "border-accent text-accent bg-accent/10" : "border-border-default text-fg-muted hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
        {data && <span className="ml-auto self-center text-xs text-fg-subtle">{items.length} 件</span>}
      </div>

      {isFetching && !data && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {error && <div className="text-critical text-sm">エラー: {String(error)}</div>}
      {data && items.length === 0 && (
        <div className="text-fg-muted text-sm bg-surface-1 border border-border-subtle rounded-lg p-4">
          該当する事象がありません。
        </div>
      )}

      <ul className="space-y-1.5">
        {items.map((it) => (
          <li
            key={it.id}
            className="flex items-start gap-2 bg-surface-1 border border-border-subtle rounded-lg px-3 py-2.5"
          >
            <span
              className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${
                it.importance === "high" ? "bg-critical" : it.importance === "medium" ? "bg-warning" : "bg-fg-subtle"
              }`}
            />
            <div className="flex-1 min-w-0">
              <button
                onClick={() => setOpenId(it.id)}
                className="block w-full text-left text-sm font-medium leading-snug text-fg hover:text-accent hover:underline"
                title="事象の詳細を開く"
              >
                {it.headline}
              </button>
              {it.preview && (
                <p className="text-xs text-fg-muted leading-relaxed mt-1 line-clamp-4">{it.preview}</p>
              )}
              <div className="text-[11px] flex flex-wrap items-center gap-x-1.5 gap-y-1 mt-1">
                <SourceChip item={it} />
                {it.member_count > 1 && (
                  <span className="px-1 rounded bg-surface-2 text-fg-muted">{it.member_count} 記事を統合</span>
                )}
                {it.status === "updated" && (
                  <span className="px-1 rounded bg-accent/15 text-accent">更新</span>
                )}
                {it.best_source_tier && it.best_source_tier !== "news" && (
                  <span className="px-1 rounded bg-surface-2 text-fg-muted">{it.best_source_tier}</span>
                )}
                <span className="ml-auto shrink-0 text-fg-subtle" title="最新報道の時刻">
                  {formatJstCompact(it.last_reported_at)}
                </span>
              </div>
            </div>
          </li>
        ))}
      </ul>

      <Drawer
        isOpen={openId !== null}
        onClose={() => setOpenId(null)}
        title={openItem?.headline ?? "事象"}
        mobileGutter
        swipeToClose
      >
        {openId && <DetailBody id={openId} />}
      </Drawer>
    </div>
  );
}
