// 事象 1 件の詳細ビュー。**一覧ページと dashboard widget の両方から使う**ため、
// ページから切り出して共有している (ドロワーの中身を 2 箇所に複製しない)。
//
// **通常ニュース (ArticleReadView) を基準にし、事象ニュース固有のものを差し込む**
// (2026-08-24)。事象ニュースを前面に出して通常ニュースを置き換えていく前提のため、
// 「事象ニュース独自の画面」ではなく「記事画面の上位互換」として作る:
//
//  - **単独記事の事象は ArticleReadView をそのまま埋め込む**。記事が 1 件なら中身は
//    通常ニュースと同一で、そこから更にドロワーを開かせるのは純粋な遠回り。本文
//    (日本語訳・IoC コピー含む) まで 1 枚で読み切れる
//  - 複数記事の事象も **節の並びを記事画面に合わせる**
//    (ヘッダ → 判定 → エンティティ → 要点/要約 → 本文 → 固有節)
//
// 表示の原則 (docs/event_news_design.md §3):
//  - 生成本文と原記事を構造で区別する (セクション名に「生成」と明記)。構成記事は全件出す
//  - 裏取りは「独立媒体数 × tier」。記事数を裏取りとして見せない
//  - メタデータは **決定論の集約** (LLM を通らない) なので、生成本文とは別カードに置く

import { useQuery } from "@tanstack/react-query";
import { formatJst } from "../../utils/date";
import { buildSections } from "../../public/sections";
import { vocabLabel } from "../../hooks/useVocab";
import { ArticleReadView, IMPORTANCE_TONE } from "../article/ArticleReadView";
import {
  PMESII_LABELS,
  fetchArticleDetail,
  type ArticleDetailResponse,
} from "../../api/article";
import { JudgementCard, type Judgement, type JudgementValue } from "../../components/analysis/JudgementCard";
import { EntitySection } from "../../components/analysis/EntitySection";
import { EventNoteEditor } from "./EventNoteEditor";
import { intentLabel, isHypothesisIntent } from "../../utils/diamond";
import { sectorLabel } from "../../components/geo/sectorColors";
import { countryLabel } from "../../utils/countryLabels";
import { useChannelMeta } from "../../components/channel";
import {
  fetchEventNewsDetail,
  type EventNewsDetail,
  type EventNewsFact,
  type EventNewsFacet,
  type EventNewsListItem,
} from "../../api/eventnews";

const CARD = "bg-surface-1 border border-border-subtle rounded-lg p-4";
const CARD_LABEL = "text-fg-muted text-xs uppercase";

// 読む面 (要約 / 要点 / 本文) は枠で囲わない。単独記事のドロワーと同じ扱いにする
// — 統合記事だけ箱のままだと、同じ画面で 2 つの様式が並ぶ (2026-08-29 利用者指摘)。
const READ_PANEL = "bg-surface-2/60 rounded-lg p-4";
const READ_LABEL = "text-fg-subtle text-[12px] font-semibold";

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
function Paragraph({
  facts,
  articleIdOf,
}: {
  facts: EventNewsFact[];
  articleIdOf: (n: number) => string | undefined;
}) {
  return (
        <p className="m-0">
          {facts.map((f, i) => (
            <span key={i}>
              {f.text}
              {f.source_index > 0 && articleIdOf(f.source_index) && (
                <a
                  href={`/app/article/${encodeURIComponent(articleIdOf(f.source_index)!)}`}
                  title={`出典 [${f.source_index}] を開く`}
                  className="align-super text-[12px] font-mono text-fg-subtle hover:text-accent ml-0.5 mr-0.5 no-underline"
                >
                  [{f.source_index}]
                </a>
              )}
            </span>
          ))}
        </p>
  );
}

/** 本文。**節を持つ版は節見出しつきで組む** (公開面と同じ `buildSections` を使う。
 *  組み方を 2 つ持つとどちらかが古くなる)。節を持たない古い版は段落だけで描く。 */
function Body({
  facts,
  articleIdOf,
}: {
  facts: EventNewsFact[];
  articleIdOf: (n: number) => string | undefined;
}) {
  const sections = buildSections(facts);
  const paragraphs = new Map<number, EventNewsFact[]>();
  for (const f of facts) {
    const key = f.paragraph || 1;
    const list = paragraphs.get(key);
    if (list) list.push(f);
    else paragraphs.set(key, [f]);
  }
  return (
    <div className="text-sm text-fg-muted leading-relaxed mt-3 space-y-4">
      {sections.length > 0
        ? sections.map((sec) => (
            <section key={sec.key} className="space-y-2">
              <div className="text-[13px] font-semibold tracking-wide text-accent">{sec.label}</div>
              {sec.paragraphs.map((para, i) => (
                <Paragraph key={i} facts={para as EventNewsFact[]} articleIdOf={articleIdOf} />
              ))}
            </section>
          ))
        : [...paragraphs.keys()]
            .sort((a, b) => a - b)
            .map((k) => (
              <Paragraph key={k} facts={paragraphs.get(k)!} articleIdOf={articleIdOf} />
            ))}
    </div>
  );
}

/** 集計 facet → 共有カードの値。複数記事なら件数を添える。 */
function facetValues(
  f: EventNewsFacet | null | undefined,
  withCounts: boolean,
  labelOf?: (value: string) => string,
): JudgementValue[] | null {
  if (!f) return null;
  return f.values.map((v) => ({
    value: v.value,
    label: (labelOf ? labelOf(v.value) : vocabLabel(f.vocab, v.value)) || v.value,
    articles: withCounts ? v.articles : undefined,
  }));
}

/** 構成記事の判定を、記事画面と **同じカード・同じ行** で見せる。 */
function EventJudgement({ d }: { d: EventNewsDetail }) {
  const chMeta = useChannelMeta();
  const j = d.metadata.judgement;
  const multi = d.members.length > 1;
  const judgement: Judgement = {
    intent: facetValues(j.intent, multi, (v) => intentLabel(v)),
    intentConfidence: (j.intent_confidence ?? [])
      .map((c) => ({
        label: vocabLabel("confidence", c.value),
        hypothesis: isHypothesisIntent(c.value),
        articles: multi ? c.articles : undefined,
      }))
      .filter((c) => c.label),
    texts: (j.texts ?? []).map((t) => ({
      label: t.label,
      // 出典番号は複数記事のときだけ意味を持つ (1 件なら自明)
      items: t.items.map((it) => ({ text: it.text, sourceIndex: multi ? it.source_index : undefined })),
    })),
    stance: facetValues(j.stance, multi),
    victim: [
      [
        ...(facetValues(j.victim_sector, multi, (v) => sectorLabel(v)) ?? []),
        ...(facetValues(j.victim_country, multi, (v) => countryLabel(v)) ?? []),
      ],
    ],
    delivery:
      (j.channel?.values.length ?? 0) > 0 ? (
        <>
          {j.channel!.values.map((v, i) => (
            <span key={v.value}>
              {i > 0 && <span className="text-fg-subtle"> / </span>}
              <span className="text-fg">{chMeta(v.value).label}</span>
              {multi && <span className="text-fg-subtle text-xs ml-0.5 tnum">({v.articles})</span>}
            </span>
          ))}
        </>
      ) : null,
    pmesii: (j.pmesii ?? [])
      .map((x) => ({
        label: PMESII_LABELS.find((p) => p.key === x.axis)?.label ?? x.axis,
        articles: multi ? x.articles : undefined,
      })),
    extraRows: [
      {
        label: "裏取り",
        node: (
          <>
            独立 {d.independent_sources} 媒体
            {/* 内訳を出す。「3 媒体」がニュース 3 社なのか X の転載 3 件なのかで
                意味が全く違うため、数だけを見せない (docs/event_news_design.md §3-3)。 */}
            {d.corroboration.length > 0 && (
              <span className="text-fg-subtle">
                {" — "}
                {d.corroboration
                  .map((c) => `${vocabLabel("source_tier", c.tier) || c.tier} ${c.media}`)
                  .join(" ・ ")}
              </span>
            )}
            {d.state_media_count > 0 && (
              <span className="text-critical"> ・国営 {d.state_media_count}</span>
            )}
          </>
        ),
      },
    ],
  };
  return <JudgementCard j={judgement} />;
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
              <div className="text-[13px] text-fg-subtle flex flex-wrap items-center gap-x-2 mt-0.5">
                <span>{m.feed_title}</span>
                <span>{vocabLabel("source_tier", m.source_tier) || m.source_tier}</span>
                {m.account_class && (
                  <span className="text-accent">
                    {vocabLabel("account_class", m.account_class) || m.account_class}
                  </span>
                )}
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

/** 単独記事の事象 = 中身は通常ニュースと同一。**記事ビューをそのまま埋め込む**。
 *
 * ここから更に記事ドロワーを開かせるのは遠回りでしかない (記事は 1 件しかない)。
 * 本文・日本語訳・IoC コピーまで含めて 1 枚で読み切れるようにする。
 */
function SingleArticleBody({ d }: { d: EventNewsDetail }) {
  const articleId = d.members[0]?.article_id ?? "";
  const { data, isLoading, error } = useQuery<ArticleDetailResponse>({
    queryKey: ["article-detail", articleId],
    queryFn: () => fetchArticleDetail(articleId),
    enabled: articleId !== "",
    retry: false,
  });

  return (
    <div className="space-y-4">
      {/* 事象ニュース固有の差し込み: 裏取りの状態 */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <SourceChip item={d} />
        <span className="text-fg-subtle">
          他媒体が報じると事象として統合され、本文が生成される
        </span>
      </div>
      {isLoading && <div className="text-fg-subtle text-sm">読み込み中…</div>}
      {(error || (!isLoading && !data)) && (
        <div className="text-critical text-sm bg-surface-1 border border-border-subtle rounded-lg px-4 py-3">
          記事を取得できませんでした
          {/* 取得できなくても事象側が持つ要約だけは読ませる (空振りで終わらせない) */}
          {d.members[0]?.summary && (
            <p className="text-sm text-fg-muted leading-relaxed whitespace-pre-wrap mt-2 mb-0">
              {d.members[0].summary}
            </p>
          )}
        </div>
      )}
      {data && <ArticleReadView data={data} variant="peek" />}
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
  // 記事 1 件の事象は中身が通常ニュースそのもの。記事ビューを埋め込み、
  // 事象側のヘッダを重ねない (見出し・重要度・時刻が二重になるため)。
  if (!d.news && d.members.length === 1) return <SingleArticleBody d={d} />;
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
          {/* ⚠ ラベルは公開面と揃える。BLUF は散文の**要約**、箇条書きが**要点**で
              別物 (2026-08-25 利用者指摘)。片方だけ「要点」と呼ぶと語が割れる。 */}
          <div className={READ_PANEL}>
            <div className={`${READ_LABEL} mb-1.5`}>要約 (kuebiko 生成)</div>
            <p className="m-0 text-[15px] text-fg leading-[1.85] whitespace-pre-wrap">{d.news.bluf}</p>
          </div>

          {(d.news.key_points ?? []).length > 0 && (
            <div className={READ_PANEL}>
              <div className={`${READ_LABEL} mb-1.5`}>要点</div>
              <ul className="m-0 pl-4 space-y-1.5">
                {(d.news.key_points ?? []).map((point, i) => (
                  <li key={i} className="text-[15px] text-fg leading-[1.8]">
                    {point}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <details className="border-t border-border-subtle pt-3" open>
            <summary className={`${READ_LABEL} cursor-pointer select-none`}>
              本文 (kuebiko 生成・{new Set(d.news.facts.map((f) => f.paragraph || 1)).size} 段落)
            </summary>
            <Body facts={d.news.facts} articleIdOf={articleIdOf} />
          </details>

          {(d.news.caveats ?? []).length > 0 && (
            <div className={READ_PANEL}>
              <div className={`${READ_LABEL} mb-1.5`}>読むうえでの但し書き</div>
              <ul className="m-0 pl-4 space-y-1">
                {(d.news.caveats ?? []).map((c, i) => (
                  <li key={i} className="text-sm text-fg-muted leading-relaxed">
                    {c.text}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {d.news.discrepancies.length > 0 && (
            <div className={READ_PANEL}>
              <div className={`${READ_LABEL} mb-1.5`}>ソース間の相違</div>
              <ul className="list-disc pl-5 text-sm text-fg-muted leading-relaxed space-y-1 m-0">
                {d.news.discrepancies.map((x, i) => <li key={i}>{x.text}</li>)}
              </ul>
            </div>
          )}

          {d.news.unknowns.length > 0 && (
            <div className={READ_PANEL}>
              <div className={`${READ_LABEL} mb-1.5`}>未確認・不明</div>
              <ul className="list-disc pl-5 text-sm text-fg-muted leading-relaxed space-y-1 m-0">
                {d.news.unknowns.map((x, i) => <li key={i}>{x}</li>)}
              </ul>
            </div>
          )}
        </>
      ) : (
        /* 複数記事だが生成がまだ (または失敗した) 状態。空振りで終わらせない。 */
        <div className={READ_PANEL}>
          <div className={`${READ_LABEL} mb-1.5`}>要約 (原記事)</div>
          <p className="text-sm text-fg leading-relaxed whitespace-pre-wrap m-0">
            {d.members[0]?.summary}
          </p>
          <p className="text-xs text-fg-subtle mt-3 pt-3 border-t border-border-subtle m-0">
            統合本文は次の生成で作られる。それまでは最初に報じた記事の要約を表示している。
          </p>
        </div>
      )}

      {/* 分析メタ (判定 / エンティティ) は **本文の後ろ**。読むものを先に置く
          — 判定の箱を先頭に積むと、記事に辿り着く前に分析画面を読まされる
          (単独記事のドロワーと同じ扱い。2026-08-29 利用者指摘)。
          描画は共有コンポーネント (components/analysis/*)。 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <EventJudgement d={d} />
        <EntitySection
          subjectActors={d.metadata.subject_actors}
          subjectActorSource={d.metadata.subject_actors.length > 0 ? "aggregate" : null}
          groups={d.metadata.entities}
          note="構成記事の抽出結果を集計（数字 = 言及した記事数）"
        />
      </div>

      <MembersCard d={d} />
      <EventNoteEditor itemId={d.id} />

      {d.news && <p className="text-xs text-fg-subtle m-0">{d.note}</p>}
    </div>
  );
}
