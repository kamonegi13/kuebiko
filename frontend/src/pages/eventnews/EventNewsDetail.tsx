// 事象 1 件の詳細ビュー。**一覧ページと dashboard widget の両方から使う**ため、
// ページから切り出して共有している (ドロワーの中身を 2 箇所に複製しない)。
//
// 表示の原則 (docs/event_news_design.md §3):
//  - 生成本文と原記事を構造で区別する。構成記事は必ず全件出す
//  - 裏取りは「独立媒体数 × tier」。記事数を裏取りとして見せない
//  - メタデータは **決定論の集約** (LLM を通らない) なので、生成本文とは別枠に置く

import { useQuery } from "@tanstack/react-query";
import { formatJst } from "../../utils/date";
import { vocabLabel } from "../../hooks/useVocab";
import {
  fetchEventNewsDetail,
  type EventNewsDetail,
  type EventNewsFact,
  type EventNewsListItem,
  type EventNewsMetadata,
} from "../../api/eventnews";

/** 裏取り = 独立媒体数。記事数では表さない (docs/event_news_design.md §3-3)。 */
export function SourceChip({
  item,
}: {
  item: Pick<EventNewsListItem, "independent_sources" | "state_media_count" | "unclassified_sources">;
}) {
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

// entity chip → 記事サーフェス (/app/news) の逆引き。記事詳細と同じ規約。
function pivotHref(type: string, value: string): string {
  return `/app/news?${new URLSearchParams({ pivot_type: type, pivot_value: value })}`;
}

function chipTone(score: number): string {
  if (score >= 9) return "text-critical";
  if (score >= 7) return "text-warning";
  return "text-fg-subtle";
}

/** 原記事から抽出済みのメタデータ。**生成物ではない**ので本文と別枠に置く。 */
function MetadataPanel({ meta }: { meta: EventNewsMetadata }) {
  const hasAny =
    meta.entities.length > 0 || meta.subject_actors.length > 0 || meta.facets.length > 0;
  if (!hasAny) return null;

  return (
    <div className="border-t border-border-subtle pt-3">
      <h4 className="text-xs uppercase tracking-wider text-fg-muted font-medium mb-2">
        原記事から抽出したメタデータ
        <span className="ml-2 normal-case tracking-normal text-fg-subtle font-normal">
          生成本文ではなく、構成記事の抽出結果を集計したもの（丸数字は言及した記事数）
        </span>
      </h4>

      {meta.subject_actors.length > 0 && (
        <div className="mb-2.5">
          <div className="text-fg-subtle text-xs mb-1">主題アクター</div>
          <div className="flex flex-wrap gap-1.5">
            {meta.subject_actors.map((sa) => (
              <a
                key={sa.id}
                href={pivotHref("actor", sa.id)}
                title={`${sa.label} で逆引き（${sa.articles} 記事が主題として帰属）`}
                className="inline-flex items-center gap-1 bg-accent/10 border border-accent-soft rounded px-2 py-0.5 text-xs font-medium text-accent hover:bg-accent/20 transition-colors"
              >
                {sa.label}
                <span className="tnum text-[10px] text-accent/70">{sa.articles}</span>
              </a>
            ))}
          </div>
        </div>
      )}

      {meta.facets.map((f) => (
        <div key={f.key} className="mb-2.5">
          <div className="text-fg-subtle text-xs mb-1">{vocabLabel("facet", f.key) || f.key}</div>
          <div className="flex flex-wrap gap-1.5">
            {f.values.map((v) => (
              <span
                key={v.value}
                className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted"
              >
                {vocabLabel(f.key, v.value) || v.value}
                <span className="tnum text-[10px] text-fg-subtle">{v.articles}</span>
              </span>
            ))}
          </div>
        </div>
      ))}

      {meta.entities.map((g) => (
        <div key={g.type} className="mb-2.5">
          <div className="text-fg-subtle text-xs mb-1">
            {vocabLabel("entity_type", g.type)}
            {g.omitted > 0 && <span className="ml-1 text-fg-subtle">（他 {g.omitted} 件）</span>}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {g.values.map((v) => {
              const cvss = g.cvss?.[v.value];
              return (
                <a
                  key={v.value}
                  href={pivotHref(g.type, v.value)}
                  title={
                    cvss
                      ? `${v.value} — CVSS ${cvss.score} ${cvss.severity}（${v.articles} 記事が言及）`
                      : `${v.value} で逆引き（${v.articles} 記事が言及）`
                  }
                  className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs font-mono text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
                >
                  {v.value}
                  {cvss && (
                    <span className={`tnum font-semibold ${chipTone(cvss.score)}`}>
                      {cvss.score.toFixed(1)}
                    </span>
                  )}
                  {v.articles > 1 && (
                    <span className="tnum text-[10px] text-fg-subtle">{v.articles}</span>
                  )}
                </a>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

export function EventNewsDetailBody({ id }: { id: string }) {
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

      <MetadataPanel meta={d.metadata} />

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
