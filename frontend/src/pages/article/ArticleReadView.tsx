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
  type ArticleEntityGroup,
} from "../../api/article";

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
  fullFlow = false,
}: {
  articleId: string;
  body: string | null;
  bodyJa: string | null;
  bodySource: string | null;
  extractionFailureReason: string | null;
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

  if (!body && !bodyJa) return null;
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
    <details className="bg-surface-1 border border-border-subtle rounded-lg p-4" open>
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

// ---------- IoC 一括コピー + 単記事 STIX (2026-07-25) ----------

// コピー対象: ioc_* (IP/ドメイン/URL/ハッシュ) + CVE。TTP は IoC ではないため対象外。
function isCopyableIocType(type: string): boolean {
  return type.startsWith("ioc_") || type === "cve";
}

// defang: 共有時の誤クリック防止の作法 (hxxp / [.])。ハッシュ・CVE は無変換。
function defangValue(type: string, value: string): string {
  if (type === "ioc_url") return value.replace(/^http/i, "hxxp").replace(/\./g, "[.]");
  if (type === "ioc_ip" || type === "ioc_domain") return value.replace(/\./g, "[.]");
  return value;
}

function buildIocClipboard(entities: ArticleEntityGroup[], defang: boolean): string {
  const sections = entities
    .filter((g) => isCopyableIocType(g.type) && g.values.length > 0)
    .map((g) => {
      const values = defang ? g.values.map((v) => defangValue(g.type, v)) : g.values;
      return `# ${vocabLabel("entity_type", g.type)}\n${values.join("\n")}`;
    });
  return sections.join("\n\n");
}

function EntityActions({
  articleId,
  entities,
}: {
  articleId: string;
  entities: ArticleEntityGroup[];
}) {
  const [copied, setCopied] = useState<"plain" | "defang" | "error" | null>(null);
  const hasIoc = entities.some((g) => isCopyableIocType(g.type) && g.values.length > 0);
  if (entities.length === 0) return null;

  const copy = async (mode: "plain" | "defang") => {
    try {
      await navigator.clipboard.writeText(buildIocClipboard(entities, mode === "defang"));
      setCopied(mode);
    } catch {
      setCopied("error");
    }
    setTimeout(() => setCopied(null), 1500);
  };

  const btn =
    "bg-surface-2 border border-border-default rounded px-2 py-0.5 text-[11px] text-fg-muted hover:text-accent hover:border-accent-soft transition-colors";

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {copied === "error" && <span className="text-[11px] text-critical">コピー失敗</span>}
      {(copied === "plain" || copied === "defang") && (
        <span className="text-[11px] text-accent">コピーしました</span>
      )}
      {hasIoc && (
        <>
          <button onClick={() => copy("defang")} className={btn} title="IoC を defang 表記 (hxxp / [.]) で一括コピー">
            IoC コピー (defang)
          </button>
          <button onClick={() => copy("plain")} className={btn} title="IoC をそのままの表記で一括コピー">
            plain
          </button>
        </>
      )}
      <a
        href={`/api/v1/articles/${encodeURIComponent(articleId)}/stix`}
        className={btn}
        title="この記事の STIX 2.1 bundle をダウンロード"
      >
        STIX
      </a>
    </div>
  );
}

// entity chip → 記事サーフェス (/app/news) の逆引きビュー (article_entities の type と同一)。
function pivotHref(type: string, value: string): string {
  const qs = new URLSearchParams({ pivot_type: type, pivot_value: value });
  return `/app/news?${qs.toString()}`;
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
  // 言及 (mention) actor 群から主題 id を除いて表示するための集合 (役割三分割、B1)。
  const subjectIdSet = new Set(a.subject_actor_ids);
  const activePmesii = PMESII_LABELS.filter((p) => a.pmesii[p.key]);

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

      {/* Diamond 軸 + 判定メタ */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
          <div className="text-fg-muted text-xs uppercase">Diamond / 判定</div>
          <dl className="text-sm space-y-1.5">
            {a.socio_political_intent && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">意図</dt>
                <dd className={`font-medium ${intentTone(a.socio_political_intent)}`}>
                  {intentLabel(a.socio_political_intent)}
                  {vocabLabel("confidence", a.intent_confidence) && (
                    <span
                      className={`ml-2 text-xs font-normal ${
                        isHypothesisIntent(a.intent_confidence)
                          ? "text-warning"
                          : "text-fg-subtle"
                      }`}
                    >
                      {vocabLabel("confidence", a.intent_confidence)}
                      {isHypothesisIntent(a.intent_confidence) && " (仮説)"}
                    </span>
                  )}
                </dd>
              </div>
            )}
            {a.socio_political_rationale && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">意図根拠</dt>
                <dd className="text-fg-muted">{a.socio_political_rationale}</dd>
              </div>
            )}
            {a.technical_axis_summary && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">技術面</dt>
                <dd className="text-fg-muted">{a.technical_axis_summary}</dd>
              </div>
            )}
            {a.remediation && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">対処</dt>
                <dd className="text-fg-muted">{a.remediation}</dd>
              </div>
            )}
            {a.editorial_stance && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">論調</dt>
                <dd className="text-fg-muted">{vocabLabel("stance", a.editorial_stance)}</dd>
              </div>
            )}
            {(a.victim_sector || a.victim_country) && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">被害</dt>
                <dd className="text-fg-muted">
                  {[sectorLabel(a.victim_sector), countryLabel(a.victim_country)].filter(Boolean).join(" / ")}
                </dd>
              </div>
            )}
            {/* flow Phase 3: なぜこのチャンネルに配信されたか。route() 非経由の記事 */}
            {/* (旧レコード / grok metadata 直指定) は説明が無いので行ごと省く。 */}
            {(a.routing_reason || a.posted_channel) && (
              <div className="flex gap-2">
                <dt className="text-fg-subtle w-24 shrink-0">配信判定</dt>
                <dd className="text-fg-muted">
                  {a.posted_channel && <span className="text-fg">{chMeta(a.posted_channel).label}</span>}
                  {a.routing_reason && (
                    <span className={a.posted_channel ? "ml-1" : ""}>
                      {a.posted_channel ? `— ${a.routing_reason}` : a.routing_reason}
                    </span>
                  )}
                </dd>
              </div>
            )}
          </dl>
          {activePmesii.length > 0 && (
            <div className="pt-1">
              <div className="text-fg-subtle text-xs mb-1">PMESII-PT</div>
              <div className="flex flex-wrap gap-1.5">
                {activePmesii.map((p) => (
                  <span
                    key={p.key}
                    className="bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted"
                  >
                    {p.label}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* エンティティ (役割三分割: 主題アクター / 言及された組織 / 技術指標)。
            言及 (mention) と主題 (subject) を分離し、報告機関 (NSA 等) が「脅威アクター」と
            誤表示される問題を構造的に解消する (2026-07-27 B1)。 */}
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-fg-muted text-xs uppercase">エンティティ (クリックで逆引き)</div>
            <EntityActions articleId={a.article_id} entities={data.entities} />
          </div>
          {/* 主題アクター = 記事の主語 (攻撃実行主体)。未帰属なら明示する。 */}
          <div>
            <div className="text-fg-subtle text-xs mb-1">主題アクター</div>
            {a.subject_actors.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {a.subject_actors.map((sa) => (
                  <a
                    key={sa.id}
                    href={pivotHref("actor", sa.id)}
                    title={`${sa.label} で逆引き`}
                    className="inline-flex items-center bg-accent/10 border border-accent-soft rounded px-2 py-0.5 text-xs font-medium text-accent hover:bg-accent/20 transition-colors"
                  >
                    {sa.label}
                  </a>
                ))}
              </div>
            ) : (
              <div className="text-fg-subtle text-xs">
                {a.subject_actor_source
                  ? "未帰属（評価済み・特定の攻撃主体なし）"
                  : "未評価"}
              </div>
            )}
          </div>
          {data.entities.length === 0 && (
            <div className="text-fg-subtle text-sm">抽出されたエンティティはありません</div>
          )}
          {data.entities.map((g) => {
            // actor (言及) 群は主題 id を除外し「言及された組織・関係者」として表示。
            // subject は上の主題アクター欄で既出のため二重表示しない。
            const isMentionActor = g.type === "actor";
            const values = isMentionActor
              ? g.values.filter((v) => !subjectIdSet.has(v))
              : g.values;
            if (values.length === 0) return null;
            const groupLabel = isMentionActor
              ? "言及された組織・関係者"
              : vocabLabel("entity_type", g.type);
            return (
            <div key={g.type}>
              <div className="text-fg-subtle text-xs mb-1">{groupLabel}</div>
              <div className="flex flex-wrap gap-1.5">
                {values.map((v) => {
                  const cvss = g.cvss?.[v];
                  return (
                    <a
                      key={v}
                      href={pivotHref(g.type, v)}
                      title={cvss ? `${v} — CVSS ${cvss.score} ${cvss.severity}` : `${v} で逆引き`}
                      className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs font-mono text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
                    >
                      {v}
                      {cvss && (
                        <span className={`tnum font-semibold ${cvss.score >= 9 ? "text-critical" : cvss.score >= 7 ? "text-warning" : "text-fg-subtle"}`}>
                          {cvss.score.toFixed(1)}
                        </span>
                      )}
                    </a>
                  );
                })}
              </div>
              {g.type === "cve" && g.affected && (() => {
                const vendors = Array.from(
                  new Set(Object.values(g.affected).flatMap((a) => a.vendors)),
                ).sort();
                if (vendors.length === 0) return null;
                return (
                  <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                    <span className="text-fg-subtle text-[11px]">影響ベンダ:</span>
                    {vendors.map((vd) => (
                      <a
                        key={vd}
                        href={`/app/news?affected_vendor=${encodeURIComponent(vd)}`}
                        title={`${vd} の脆弱性に言及する記事を絞り込む`}
                        className="inline-flex items-center bg-warning-soft border border-warning/40 rounded px-1.5 py-0.5 text-[11px] text-warning hover:bg-warning/20 transition-colors"
                      >
                        {vd}
                      </a>
                    ))}
                  </div>
                );
              })()}
            </div>
            );
          })}
        </div>
      </div>

      {/* 要約 */}
      {a.summary && (
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-4">
          <div className="text-fg-muted text-xs uppercase mb-2">要約</div>
          <p className="text-sm text-fg leading-relaxed whitespace-pre-wrap">{a.summary}</p>
        </div>
      )}

      {/* 本文 (原文 / オンデマンド日本語訳) */}
      <BodySection
        articleId={a.article_id}
        body={a.body}
        bodyJa={a.body_ja}
        bodySource={a.body_source}
        extractionFailureReason={a.extraction_failure_reason}
        fullFlow={variant === "peek"}
      />
    </div>
  );
}
