// 事象 1 件の詳細ビュー。**一覧ページと dashboard widget の両方から使う**ため、
// ページから切り出して共有している (ドロワーの中身を 2 箇所に複製しない)。
//
// 表示要領は **記事ドロワー (ArticleReadView) に合わせる** (2026-08-24)。
// 同じアプリの中でドロワーごとに構造が違うと、読み手は毎回「どこに何があるか」を
// 探し直すことになる。ヘッダ (メタ行 → 見出し → 時間軸) → カード群、という並びと
// カードの見た目・ラベル書式を共有する。
//
// 表示の原則 (docs/event_news_design.md §3):
//  - 生成本文と原記事を構造で区別する (セクション名に「生成」と明記)。構成記事は全件出す
//  - 裏取りは「独立媒体数 × tier」。記事数を裏取りとして見せない
//  - メタデータは **決定論の集約** (LLM を通らない) なので、生成本文とは別カードに置く

import { useQuery } from "@tanstack/react-query";
import { formatJst } from "../../utils/date";
import { vocabLabel } from "../../hooks/useVocab";
import { IMPORTANCE_TONE } from "../article/ArticleReadView";
import {
  fetchEventNewsDetail,
  type EventNewsDetail,
  type EventNewsFact,
  type EventNewsListItem,
  type EventNewsMetadata,
} from "../../api/eventnews";

const CARD = "bg-surface-1 border border-border-subtle rounded-lg p-4";
const CARD_LABEL = "text-fg-muted text-xs uppercase";

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
    <div className="text-sm text-fg-muted leading-relaxed mt-3 space-y-3">
      {[...paras.keys()].sort((a, b) => a - b).map((k) => (
        <p key={k} className="m-0">
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
    </div>
  );
}

// entity chip → 記事サーフェス (/app/news) の逆引き。記事詳細と同じ規約。
function pivotHref(type: string, value: string): string {
  return `/app/news?${new URLSearchParams({ pivot_type: type, pivot_value: value })}`;
}

function cvssTone(score: number): string {
  if (score >= 9) return "text-critical";
  if (score >= 7) return "text-warning";
  return "text-fg-subtle";
}

/** エンティティ カード。記事ドロワーの同名カードと同じ構造・見た目にする。
 *
 * 中身は **原記事から抽出済みの値を決定論で集計したもの** で、生成物ではない。
 */
function EntityCard({ meta }: { meta: EventNewsMetadata }) {
  const hasAny =
    meta.entities.length > 0 || meta.subject_actors.length > 0 || meta.facets.length > 0;
  if (!hasAny) return null;

  return (
    <div className={`${CARD} space-y-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className={CARD_LABEL}>エンティティ (クリックで逆引き)</div>
        <span className="text-fg-subtle text-[11px]">
          構成記事の抽出結果を集計（数字 = 言及した記事数）
        </span>
      </div>

      <div>
        <div className="text-fg-subtle text-xs mb-1">主題アクター</div>
        {meta.subject_actors.length > 0 ? (
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
        ) : (
          <div className="text-fg-subtle text-xs">未帰属（構成記事に主題アクターの帰属なし）</div>
        )}
      </div>

      {meta.facets.map((f) => (
        <div key={f.key}>
          <div className="text-fg-subtle text-xs mb-1">{f.label}</div>
          <div className="flex flex-wrap gap-1.5">
            {f.values.map((v) => (
              <span
                key={v.value}
                className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted"
              >
                {vocabLabel(f.vocab, v.value) || v.value}
                <span className="tnum text-[10px] text-fg-subtle">{v.articles}</span>
              </span>
            ))}
          </div>
        </div>
      ))}

      {meta.entities.map((g) => (
        <div key={g.type}>
          <div className="text-fg-subtle text-xs mb-1">
            {vocabLabel("entity_type", g.type)}
            {g.omitted > 0 && <span className="ml-1">（他 {g.omitted} 件）</span>}
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
                    <span className={`tnum font-semibold ${cvssTone(cvss.score)}`}>
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

/** 構成記事カード。記事ドロワーへ入る動線 (§3-1: 折りたたみは可・省略は不可)。 */
function MembersCard({ d }: { d: EventNewsDetail }) {
  return (
    <details className={CARD} open>
      <summary className={`${CARD_LABEL} cursor-pointer select-none`}>
        原記事 {d.members.length} 件{d.news ? "（[N] は本文の出典番号）" : ""}
      </summary>
      <ul className="space-y-1 mt-3">
        {d.members.map((m) => (
          <li key={m.article_id} className="flex items-start gap-2 border border-border-subtle rounded px-3 py-2">
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
                {m.published_at && <span className="tnum">{formatJst(m.published_at)}</span>}
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
    </details>
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
  const headline = d.news?.headline ?? d.members[0]?.title ?? "事象";
  const spanDays = Math.round(
    (new Date(d.last_reported_at).getTime() - new Date(d.first_reported_at).getTime()) / 86_400_000,
  );

  return (
    <div className="space-y-5">
      {/* ヘッダ — 記事ドロワーと同じ並び (メタ行 → 見出し → 時間軸) */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className={`font-semibold ${IMPORTANCE_TONE[d.importance] || "text-fg-subtle"}`}>
            {vocabLabel("importance", d.importance)}
          </span>
          <SourceChip item={d} />
          {d.members.length > 1 && <span className="text-fg-muted">{d.members.length} 記事を統合</span>}
          {d.status === "updated" && (
            <span className="px-1 rounded bg-accent/15 text-accent">更新</span>
          )}
          {d.best_source_tier && d.best_source_tier !== "news" && (
            <span className="text-fg-subtle">{d.best_source_tier}</span>
          )}
          <span className="text-fg-subtle ml-auto tnum">最新報道 {formatJst(d.last_reported_at)}</span>
        </div>
        <h2 className="m-0 text-xl font-bold text-fg leading-snug">{headline}</h2>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs pt-0.5">
          <span className="text-fg-subtle">時間軸</span>
          <span className="text-fg tnum">初報 {formatJst(d.first_reported_at)}</span>
          {/* 0-1 日は「同日〜翌日」で情報量が無いので出さない (記事側の報道ラグと同じ流儀) */}
          {spanDays >= 2 && (
            <span className="text-warning tnum" title="初報から最新報道までの期間">
              継続 {spanDays} 日
            </span>
          )}
          {d.news && (
            <span className="text-fg-subtle ml-auto">
              生成 v{d.news.version} · {d.news.model} · {formatJst(d.news.generated_at)}
            </span>
          )}
        </div>
      </div>

      {d.news ? (
        <>
          {/* 要点 — 記事ドロワーの「要約」カードと同じ位置・同じ見た目 */}
          <div className={CARD}>
            <div className={`${CARD_LABEL} mb-2`}>要点 (kuebiko 生成)</div>
            <p className="text-sm text-fg leading-relaxed whitespace-pre-wrap m-0">{d.news.bluf}</p>
          </div>

          <details className={CARD} open>
            <summary className={`${CARD_LABEL} cursor-pointer select-none`}>
              本文 (kuebiko 生成・{d.news.facts.length} 文)
            </summary>
            <Body facts={d.news.facts} articleIdOf={articleIdOf} />
          </details>

          {d.news.discrepancies.length > 0 && (
            <div className={CARD}>
              <div className={`${CARD_LABEL} mb-2`}>ソース間の相違</div>
              <ul className="list-disc pl-5 text-sm text-fg-muted leading-relaxed space-y-1 m-0">
                {d.news.discrepancies.map((x, i) => <li key={i}>{x.text}</li>)}
              </ul>
            </div>
          )}

          {d.news.unknowns.length > 0 && (
            <div className={CARD}>
              <div className={`${CARD_LABEL} mb-2`}>未確認・不明</div>
              <ul className="list-disc pl-5 text-sm text-fg-muted leading-relaxed space-y-1 m-0">
                {d.news.unknowns.map((x, i) => <li key={i}>{x}</li>)}
              </ul>
            </div>
          )}
        </>
      ) : (
        /* 単独記事 (案 A): 生成せず、原記事の要約を同じ枠で読ませる。 */
        <div className={CARD}>
          <div className={`${CARD_LABEL} mb-2`}>要約 (原記事)</div>
          <p className="text-sm text-fg leading-relaxed whitespace-pre-wrap m-0">
            {d.members[0]?.summary}
          </p>
          <p className="text-xs text-fg-subtle mt-3 pt-3 border-t border-border-subtle m-0">
            1 媒体のみの報道のため、記事の要約をそのまま表示している。他媒体が報じると事象として統合され本文が生成される。
          </p>
        </div>
      )}

      <EntityCard meta={d.metadata} />
      <MembersCard d={d} />

      {d.news && <p className="text-xs text-fg-subtle m-0">{d.note}</p>}
    </div>
  );
}
