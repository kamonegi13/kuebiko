// 記事の読み取り専用ビュー (ヘッダ / Diamond 判定 / エンティティ / 要約 / 本文)。
// ArticleDetailPage (フル画面) と ArticlePeek (サイドピーク) の共有 SSoT — 表示ロジックを
// 二重化するとドリフト発生器になるため、読み取り部は本コンポーネントに一本化する
// (2026-07-31 サイドピーク導入時に ArticleDetailPage から verbatim 抽出)。
// 編集系 (メモ・ブックマーク = NoteEditor) はフル画面専用のため本ビューには含めない。

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { formatJst } from "../../utils/date";
import { intentLabel, intentTone, isHypothesisIntent } from "../../utils/diamond";
import { useChannelMeta } from "../../components/channel";
import { vocabLabel } from "../../hooks/useVocab";
import { sectorLabel } from "../../components/geo/sectorColors";
import { countryLabel } from "../../utils/countryLabels";
import {
  PMESII_LABELS,
  translateArticle,
  type ArticleDetailResponse,
} from "../../api/article";
import { JudgementCard, type Judgement } from "../../components/analysis/JudgementCard";
import { EntitySection } from "../../components/analysis/EntitySection";
import { ArticleSituations } from "../../components/analysis/ArticleSituations";

export const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

// ---------- 本文 (原文 / オンデマンド日本語訳) ----------

// 原文が既に日本語か (かな比率 5%+)。日本語原文に翻訳ボタンを出さない
// (backend の is_probably_japanese と同一ヒューリスティック)。
function isProbablyJapanese(text: string): boolean {
  const stripped = text.trim();
  if (!stripped) return false;
  const kana = (stripped.match(/[ぁ-んァ-ヶ]/g) ?? []).length;
  return kana / stripped.length >= 0.05;
}

function BodySection({
  articleId,
  body,
  bodyJa,
  bodySource,
  extractionFailureReason,
  sourceUrl,
  fullFlow = false,
}: {
  articleId: string;
  body: string | null;
  bodyJa: string | null;
  bodySource: string | null;
  extractionFailureReason: string | null;
  /** 原文の所在。写しは本文を持たないので、そこへ案内する。 */
  sourceUrl: string | null;
  /** true = 本文を個別スクロールさせず全文フロー表示 (サイドピーク: パネルが単一スクロール) */
  fullFlow?: boolean;
}) {
  const qc = useQueryClient();
  const [showJa, setShowJa] = useState(bodyJa != null);
  // 長文の resumable 翻訳 (2026-08-06): サーバは 120 秒でチャンク境界中断し partial を
  // 返す。完了まで自動で再 POST し、訳せた先頭部分と進捗 (n/m) を逐次表示する。
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [partialText, setPartialText] = useState<string | null>(null);
  // 進捗が前回から進まない partial 応答が続いたら打ち切る (無限ループ防御)
  const lastDoneRef = useRef(-1);
  const translate = useMutation({
    mutationFn: () => translateArticle(articleId),
    onSuccess: (res) => {
      if (res.partial) {
        const done = res.done_chunks ?? 0;
        setProgress({ done, total: res.total_chunks ?? 0 });
        setPartialText(res.partial_text || null);
        if (done > lastDoneRef.current) {
          lastDoneRef.current = done;
          translate.mutate(); // 続きを自動継続 (チャンクは保存済み、続きから再開)
        }
        return;
      }
      lastDoneRef.current = -1;
      setProgress(null);
      setPartialText(null);
      // mutation の戻り値を query cache に直接反映する (invalidate の再フェッチ待ちの間
      // 「日本語訳」チップ表示なのに英語原文が見える一瞬を作らないため)。
      qc.setQueryData<ArticleDetailResponse>(["article-detail", articleId], (old) =>
        old ? { ...old, article: { ...old.article, body_ja: res.body_ja } } : old,
      );
      setShowJa(true);
    },
  });

  if (!body && !bodyJa) {
    // 写しは原文を持たない (収集した記事は再配布しない方針)。**黙って欄ごと消さない** —
    // 取得に失敗したのか、そもそも写していないのかを読み手が区別できなくなる。
    if (import.meta.env.VITE_MIRROR === "1") {
      return (
        <div className="rounded-lg border border-border-subtle bg-surface-1 px-4 py-3 text-sm text-fg-muted">
          本文は写しに含まれません。
          {sourceUrl && (
            <>
              {" "}
              <a href={sourceUrl} target="_blank" rel="noreferrer" className="text-accent underline">
                元記事を開く
              </a>
            </>
          )}
        </div>
      );
    }
    return null;
  }
  // 翻訳進行中は訳せた先頭部分を先に読めるようにする (完訳で bodyJa に置き換わる)
  const text = showJa && bodyJa ? bodyJa : (partialText ?? body ?? bodyJa ?? "");
  // body_ja='' は「処理済・訳不要 (原文が日本語)」の番兵 — 翻訳 UI 自体を出さない。
  const isTranslatable = !!body && !isProbablyJapanese(body);

  const langChip = (active: boolean) =>
    `px-2 py-0.5 rounded border text-xs transition-colors ${
      active
        ? "border-accent text-accent bg-accent/10"
        : "border-border-default text-fg-subtle hover:text-fg"
    }`;

  // 本文完全性 (2026-07-27): body_source に応じて「全文」か「フィード抜粋のみ」かを正直に表示。
  // feed_summary = 全文取得に失敗し RSS 抜粋で代用した切り株 (下流の分析が痩せる)。
  const isStump = bodySource === "feed_summary";
  const bodyLabel = isStump
    ? `本文 (フィード抜粋のみ・全文未取得 ${(body ?? "").length.toLocaleString()} 字${bodyJa ? "・日本語訳あり" : ""})`
    : `本文 (抽出済 ${(body ?? "").length.toLocaleString()} 字${bodyJa ? "・日本語訳あり" : ""})`;

  return (
    // 要約と同じく枠で囲わない。読む面は罫線と余白で区切る (公開ページと同じ)。
    <details className="border-t border-border-subtle pt-3" open>
      <summary
        className={`text-xs uppercase cursor-pointer select-none ${isStump ? "text-warning" : "text-fg-muted"}`}
      >
        {bodyLabel}
        {isStump && extractionFailureReason && (
          <span className="ml-1 lowercase text-fg-subtle">（全文取得失敗: {extractionFailureReason}）</span>
        )}
      </summary>
      {(bodyJa || isTranslatable) && (
        <div className="flex flex-wrap items-center gap-2 mt-3">
          {bodyJa ? (
            <>
              <button onClick={() => setShowJa(true)} className={langChip(showJa)}>
                日本語訳
              </button>
              {body && (
                <button onClick={() => setShowJa(false)} className={langChip(!showJa)}>
                  原文
                </button>
              )}
            </>
          ) : (
            <>
              <button
                onClick={() => {
                  // 手動 (再) 開始時は停滞ガードをリセット (自動継続を再度有効化)
                  lastDoneRef.current = -1;
                  translate.mutate();
                }}
                disabled={translate.isPending}
                className="bg-accent text-on-accent text-xs font-medium px-3 py-1 rounded hover:opacity-90 transition-opacity disabled:opacity-50"
              >
                {translate.isPending
                  ? progress
                    ? `翻訳中… (${progress.done}/${progress.total} チャンク完了・冒頭から順次表示)`
                    : "翻訳中… (本文の長さにより数十秒〜数分)"
                  : partialText
                    ? "翻訳を再開"
                    : "日本語訳を生成"}
              </button>
              {translate.isError && (
                <span className="text-xs text-critical">
                  {translate.error instanceof Error ? translate.error.message : "翻訳に失敗しました"}
                </span>
              )}
            </>
          )}
        </div>
      )}
      <div
        className={`text-sm text-fg-muted leading-relaxed whitespace-pre-wrap mt-3 ${
          fullFlow ? "" : "max-h-[600px] overflow-y-auto"
        }`}
      >
        {text}
      </div>
    </details>
  );
}

// ---------- 読み取りビュー本体 ----------

export function ArticleReadView({
  data,
  variant = "full",
}: {
  data: ArticleDetailResponse;
  /** peek = サイドピーク表示 (本文は全文フロー = 個別スクロールなし) */
  variant?: "full" | "peek";
}) {
  const chMeta = useChannelMeta();
  const a = data.article;
  const activePmesii = PMESII_LABELS.filter((p) => a.pmesii[p.key]);
  // 配信判定の表示は「ルール名」優先。解決できない (削除済みルール / route() 非経由) 記事は
  // 生の理由文にフォールバックする。
  const routingDetail = a.routing_rule_label || a.routing_reason;

  // 記事 1 件ぶんの判定を共有カードの形へ写す (件数は付けない = 集計ではないため)。
  const confidenceLabel = vocabLabel("confidence", a.intent_confidence);
  const judgement: Judgement = {
    intent: a.socio_political_intent
      ? [
          {
            value: a.socio_political_intent,
            label: intentLabel(a.socio_political_intent),
            tone: intentTone(a.socio_political_intent),
          },
        ]
      : null,
    intentConfidence: confidenceLabel
      ? [{ label: confidenceLabel, hypothesis: isHypothesisIntent(a.intent_confidence) }]
      : null,
    texts: [
      { label: "意図根拠", text: a.socio_political_rationale },
      { label: "技術面", text: a.technical_axis_summary },
      { label: "対処", text: a.remediation },
      { label: "所見", text: a.analyst_note },
    ]
      .filter((t): t is { label: string; text: string } => Boolean(t.text))
      .map((t) => ({ label: t.label, items: [{ text: t.text }] })),
    stance: a.editorial_stance
      ? [{ value: a.editorial_stance, label: vocabLabel("stance", a.editorial_stance) }]
      : null,
    victim: [
      [
        { value: a.victim_sector ?? "", label: sectorLabel(a.victim_sector) },
        { value: a.victim_country ?? "", label: countryLabel(a.victim_country) },
      ],
    ],
    delivery:
      a.routing_reason || a.posted_channel ? (
        <>
          {a.posted_channel && <span className="text-fg">{chMeta(a.posted_channel).label}</span>}
          {/* 表示はルール名 (label)。内部 id を含む生の理由は tooltip に退避する
              — 画面に "rules-engine: R2.…" が出ると何のルールか読めないため。 */}
          {routingDetail && (
            <span className={a.posted_channel ? "ml-1" : ""} title={a.routing_reason ?? undefined}>
              {a.posted_channel ? `— ${routingDetail}` : routingDetail}
            </span>
          )}
        </>
      ) : null,
    pmesii: activePmesii.map((p) => ({ label: p.label })),
  };
  const entityGroups = data.entities.map((g) => ({
    type: g.type,
    values: g.values.map((v) => ({ value: v })),
    cvss: g.cvss,
    affected: g.affected,
  }));

  return (
    <div className="space-y-5">
      {/* ヘッダ */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {a.importance && (
            <span className={`font-semibold ${IMPORTANCE_TONE[a.importance] || "text-fg-subtle"}`}>
              {vocabLabel("importance", a.importance)}
            </span>
          )}
          {a.category && <span className="text-fg-muted">{vocabLabel("category", a.category)}</span>}
          {a.article_type && (
            <span className="text-fg-subtle">{vocabLabel("article_type", a.article_type)}</span>
          )}
          {a.posted_channel && <span className="text-fg-subtle">{chMeta(a.posted_channel).label}</span>}
          {a.feed_title && <span className="text-fg-subtle">{a.feed_title}</span>}
          <span className="text-fg-subtle ml-auto tnum">
            {a.published_at ? `公開 ${formatJst(a.published_at)}` : a.created_at ? `取得 ${formatJst(a.created_at)}` : ""}
          </span>
        </div>
        <h2 className="m-0 text-xl font-bold text-fg leading-snug">{a.title}</h2>
        {/* 時間軸レイヤ b/c: 事象の実発生日を報道時刻と分離 (発生 / 報道ラグ / 滞留) */}
        {a.event_date && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs pt-0.5">
            <span className="text-fg-subtle">時間軸</span>
            <span className="text-fg tnum">
              事象 {a.event_date}
              {a.event_date_basis && (
                <span className="text-fg-subtle ml-1">（{vocabLabel("event_date_basis", a.event_date_basis)}）</span>
              )}
            </span>
            {a.reporting_lag_days != null && a.reporting_lag_days >= 2 && (
              <span className="text-warning tnum" title="報道日 − 発生日">
                報道ラグ {a.reporting_lag_days}日{a.reporting_lag_days >= 30 ? "・振り返り/続報" : ""}
              </span>
            )}
            {/* 0 日 = event_date と同値 (実質無情報、実測 21%) は出さない —
                reporting_lag_days >= 2 と同じ「意味のある値だけ出す」流儀 */}
            {a.dwell_days != null && a.dwell_days > 0 && (
              <span className="text-violet-400 tnum" title="検知/公表 − 侵害開始（滞留時間）">
                滞留 {a.dwell_days}日
              </span>
            )}
          </div>
        )}
        {/* アクション */}
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <a
            href={a.url}
            target="_blank"
            rel="noreferrer"
            className="bg-surface-2 border border-border-default rounded px-3 py-1 text-xs text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
          >
            元記事を開く ↗
          </a>
          {data.discord_url && (
            <a
              href={data.discord_url}
              target="_blank"
              rel="noreferrer"
              className="bg-accent text-on-accent rounded px-3 py-1 text-xs font-medium hover:opacity-90 transition-opacity"
            >
              Discord 投稿へジャンプ →
            </a>
          )}
        </div>
      </div>

      {/* 要約 — 様式は **公開ページ (PublicNewsSite の LeadSummary) と同じ**。
          枠付きの灰面で、拾い読みでも目に入るようにする。見出しの次に来る。 */}
      {a.summary && (
        <div className="rounded-lg border border-border-subtle bg-surface-2 px-4 py-3.5">
          <p className="m-0 mb-1.5 text-[13px] font-semibold tracking-wide text-fg-subtle">要約</p>
          <p className="m-0 text-[17px] leading-[1.85] text-fg whitespace-pre-wrap">{a.summary}</p>
        </div>
      )}

      {/* 本文 (原文 / オンデマンド日本語訳) */}
      <BodySection
        articleId={a.article_id}
        body={a.body}
        bodyJa={a.body_ja}
        bodySource={a.body_source}
        extractionFailureReason={a.extraction_failure_reason}
        sourceUrl={a.url ?? null}
        fullFlow={variant === "peek"}
      />

      {/* 分析メタ (Diamond 判定 / エンティティ) は **本文の後**。
          公開ページと同じく、読むものを先に置く — 判定の箱を先頭に積むと
          記事に辿り着く前に分析画面を読まされる (2026-08-29 利用者指摘)。
          描画は共有コンポーネント (components/analysis/*) に一本化する。
          事象ニュース側で同じカードを書き直したら行の並びもラベルもズレたため。 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <JudgementCard j={judgement} />
        <EntitySection
          subjectActors={a.subject_actors}
          subjectActorSource={a.subject_actor_source}
          subjectActorRationale={a.subject_actor_rationale}
          groups={entityGroups}
          stixArticleId={a.article_id}
        />
      </div>
      <ArticleSituations articleId={a.article_id} />
    </div>
  );
}
