import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useFilters, type PeriodType } from "../../state/filters";
import { SynthesisProse } from "../SynthesisProse";
import { formatJst, formatJstDate } from "../../utils/date";
import { spotlightApi, type SpotlightSummary, type SourceBasis } from "../../api/spotlight";
import { LedgerView } from "./LedgerView";
import type {
  Tradecraft,
  ForecastAccuracy,
  GroundedEstimate,
  GroundedJudgment,
} from "../../api/types";
import { canEditOperationalConfig, useRuntimeFlags } from "../../hooks/useRuntimeFlags";
import { ConfidenceBadge } from "../ConfidenceBadge";
import { SectionHeading } from "../SectionHeading";
import { Seg } from "../Shell";
import { useHorizontalSwipe } from "../../hooks/useHorizontalSwipe";
import { intelHref } from "../../utils/intelNav";
import { vocabLabel } from "../../hooks/useVocab";

export function SynthesisTab() {
  // 面 (読む / 根拠 / 点検) の切替は上部コントロールバー (Shell) が担う。ここは読むだけ。
  const view = useFilters((s) => s.synthesisView);
  if (view === "basis") return <BasisView />;
  if (view === "review") return <ReviewView />;
  return <ReadView />;
}

/** 読む面: 全体総括を先頭に固定し、PIR ごとの Spotlight を続ける。
 *
 *  ⭐ **根拠と点検はここに置かない。** 旧構成は読み物・根拠・点検を 1 本に縦積みして
 *  おり、実測でページ 16,165px のうち読むものは 1,283px (8%) しかなかった。
 *  「なぜそう言えるか」(根拠) と「当たっているか」(点検) は、読むこととは別の行為。
 */
function ReadView() {
  return (
    <div className="space-y-6">
      <GlobalSynthesisView />
      <SpotlightView />
    </div>
  );
}

//: 期間の並び (フリックの前後もこの順)。
const PERIODS: { v: PeriodType; label: string }[] = [
  { v: "daily", label: "日次" },
  { v: "weekly", label: "週次" },
  { v: "monthly", label: "月次" },
];

function GlobalSynthesisView() {
  const f = useFilters();
  const { data, isLoading } = useQuery({
    queryKey: ["synthesis", f.period_type],
    queryFn: () => api.synthesis(f.period_type),
  });

  // モバイルは左右フリックでも切り替える (小さい画面で切替を探させない)。
  const step = (d: 1 | -1) => {
    const i = PERIODS.findIndex((p) => p.v === f.period_type);
    const next = PERIODS[Math.min(PERIODS.length - 1, Math.max(0, i + d))];
    if (next) f.setPeriodType(next.v);
  };
  const swipeRef = useHorizontalSwipe<HTMLDivElement>({
    onNext: () => step(1),
    onPrev: () => step(-1),
  });

  const periodLabel = PERIODS.find((p) => p.v === f.period_type)?.label ?? "";

  return (
    <div ref={swipeRef}>
      {/* 期間 (日/週/月) の切替は上部コントロールバー (Shell) が担う。 */}
      {/* 目次は撤去 (2026-08-29)。飛び先の 3/5 は別の面へ移った — 面をまたぐ
          アンカーは無言で効かなくなるので、残さず消す。 */}
      {/* 期間の切替は **この節の中** に置く。変わるのは全体総括だけで、
          PIR 別の動向は常に直近 7 日 — 画面上部に置くと全部が切り替わると読める。 */}
      <SectionHeading
        title={`${periodLabel}総括`}
        note={
          data?.latest
            ? `${formatJstDate(data.latest.period_start)} 〜 ${formatJstDate(data.latest.period_end)}`
            : undefined
        }
        action={<Seg items={PERIODS} value={f.period_type} onChange={f.setPeriodType} />}
      />

      {isLoading && <SkeletonRows />}

      {!isLoading && data && !data.has_data && (
        <div className="bg-surface-1 border border-dashed border-border-default rounded-lg p-10 text-center text-fg-muted">
          <p>まだ <strong className="text-fg">{f.period_type === "daily" ? "日次" : f.period_type === "monthly" ? "月次" : "週次"}</strong>の状況総括が生成されていません</p>
          <p className="text-xs mt-2">
            {f.period_type === "daily"
              ? "次回の自動実行 (07:30 / 19:30 JST) または処理完了後に自動生成"
              : f.period_type === "monthly"
                ? "次回の自動実行 (月末 20:00 JST) で生成"
                : "次回の自動実行 (日曜 18:30 JST) で生成"}
          </p>
        </div>
      )}

      {data?.has_data && data.latest && (
        <>
          {/* Hero */}
          {/* リード。公開ページの「注目」と同じ扱い — ラベルを付けず、見出しそのものを
              大きく出す。素性 (件数・生成時刻) は下に小さく添える。 */}
          <div className="mb-5">
            <p className="m-0 text-[19px] leading-[1.75] font-bold text-fg">{data.latest.headline}</p>
            <div className="mt-2 text-[12px] text-fg-subtle flex flex-wrap gap-x-3 gap-y-1">
              <span>
                根拠 {data.latest.article_count} 記事
                {data.tradecraft?.grounded_estimate?.considered_count
                  ? ` / 考慮 ${data.tradecraft.grounded_estimate.considered_count}`
                  : ""}
              </span>
              <span>{formatJst(data.latest.generated_at)}</span>
              <span>{data.latest.llm_model}</span>
            </div>
          </div>

          {/* Sections — 2 カラム grid。行内のカード高さは stretch で揃える (隙間ガタつき防止)。
              奇数個の最後 (6. PIR) は全幅 + 内部 2 段組で空きスロットを作らない。 */}
          <div id="syn-narrative" className="grid grid-cols-1 lg:grid-cols-2 gap-x-6 gap-y-1 mb-5 scroll-mt-24">
            <Section title="軸別の重みと不均衡" body={data.latest.weight_section} />
            <Section title="軸間連鎖の解釈" body={data.latest.chain_section} />
            <Section title="重心" body={data.latest.cog_section} />
            <Section title="波及解釈 (中期予想)" body={data.latest.spillover_section} />
            <Section title="PIR 達成度" body={data.latest.pir_section} className="lg:col-span-2" columns />
          </div>

          {/* 根拠と点検はここに置かない (面が違う)。主張から辿れる導線だけ残す。 */}
          <BasisLink tc={data.tradecraft} />
        </>
      )}
    </div>
  );
}

/** 読み物から根拠へ降りる導線。**根拠が読み物と同居しないことと、根拠へ辿れないことは別**
 *  なので、何件の判定に裏付けがあるかを示して 1 クリックで渡す。 */
function BasisLink({ tc }: { tc?: Tradecraft }) {
  const n = tc?.grounded_estimate?.judgments?.length ?? 0;
  const setView = useFilters((s) => s.setSynthesisView);
  if (!tc) return null;
  return (
    <button
      type="button"
      onClick={() => setView("basis")}
      className="w-full text-left bg-surface-1 border border-border-subtle rounded-lg px-4 py-3 text-sm text-fg-muted hover:border-border-default hover:text-fg cursor-pointer"
    >
      根拠を見る
      {n > 0 && <span className="ml-2 text-fg-subtle">競合仮説と証拠 {n} 件</span>}
      <span className="ml-2 text-accent">→</span>
    </button>
  );
}

/** 根拠の面: なぜそう言えるか。ACH (競合仮説と証拠) / 分析トレードクラフト / 軸別の証拠。
 *
 *  ⭐ 読み物とは **別の行為**。ここは照合しに来る場所で、通読する場所ではない。 */
function BasisView() {
  const f = useFilters();
  const { data, isLoading } = useQuery({
    queryKey: ["synthesis", f.period_type],
    queryFn: () => api.synthesis(f.period_type),
  });
  if (isLoading) return <SkeletonRows />;
  if (!data?.has_data || !data.latest) return <EmptyBasis />;
  return (
    <div>
      <h3 className="m-0 mb-1 text-lg font-bold text-fg tracking-tight">根拠</h3>
      <p className="mt-0 mb-4 text-[13px] text-fg-subtle">
        現況の各判定が何に基づくか。競合仮説・証拠・前提・覆る指標を、本文と照合できる形で示す。
      </p>
      {data.tradecraft && <TradecraftSection tc={data.tradecraft} />}
      {data.tradecraft?.grounded_estimate && (
        <GroundedEstimateSection
          est={data.tradecraft.grounded_estimate}
          generatedAt={data.latest.generated_at}
        />
      )}
      {data.axes_evidence && Object.keys(data.axes_evidence).length > 0 && (
        <div id="syn-evidence" className="scroll-mt-24">
          <div className="flex items-baseline justify-between gap-2 mb-2.5">
            <h4 className="m-0 text-md text-fg font-semibold tracking-tight">軸別の証拠</h4>
            <a
              href={intelHref("pmesii")}
              className="text-xs text-accent hover:text-accent-hover no-underline shrink-0"
            >
              国家情勢で軸を見る →
            </a>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-2.5">
            {Object.entries(data.axes_evidence).map(([axisId, events]) => (
              <AxisCard key={axisId} axisId={axisId} events={events} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** 点検の面: 当たっているか。予測スコアカードと情勢台帳。
 *
 *  ⭐ 根拠 (なぜ言えるか) とは別。定期的に振り返る材料で、読む流れには挟まない。 */
function ReviewView() {
  const f = useFilters();
  const { data, isLoading } = useQuery({
    queryKey: ["synthesis", f.period_type],
    queryFn: () => api.synthesis(f.period_type),
  });
  return (
    <div className="space-y-6">
      <div>
        <h3 className="m-0 mb-1 text-lg font-bold text-fg tracking-tight">点検</h3>
        <p className="mt-0 mb-4 text-[13px] text-fg-subtle">
          出した予測が当たったか、情勢の見立てが今どうなっているか。
        </p>
        {isLoading && <SkeletonRows />}
        {data?.tradecraft &&
          (data.tradecraft.forecast_scorecard?.length || data.tradecraft.forecasts?.length) && (
            <ForecastScorecard tc={data.tradecraft} acc={data.forecast_accuracy} />
          )}
      </div>
      <LedgerView />
    </div>
  );
}

function EmptyBasis() {
  return (
    <div className="bg-surface-1 border border-dashed border-border-default rounded-lg p-10 text-center text-fg-muted">
      まだ状況総括が生成されていないため、根拠もありません
    </div>
  );
}

function Section({ title, body, className = "", columns = false }: {
  title: string;
  body: string;
  className?: string;
  columns?: boolean;
}) {
  return (
    // 枠を並べるのではなく **区切り線で節を切る** (公開ページと同じ)。箱が 5 つ
    // 並ぶと、どれも同じ重さに見えて読み進む手掛かりが無くなる。
    <div className={`border-t border-border-subtle pt-3 ${className}`}>
      <h4 className="m-0 mb-2 text-[13px] text-fg-muted font-semibold tracking-wide">{title}</h4>
      <SynthesisProse text={body} columns={columns} />
    </div>
  );
}

// S2: 分析トレードクラフト (ICD 203) — 主見立てだけでなく対立仮説・前提・覆る指標を提示し
// 読者 (analyst) の確証バイアス/トンネル視を防ぐ。
function TradecraftSection({ tc }: { tc: Tradecraft }) {
  const hasContent =
    !!tc.leading_assessment ||
    !!tc.alternatives?.length ||
    !!tc.key_assumptions?.length ||
    !!tc.indicators?.length;
  if (!hasContent) return null;

  const lists: { title: string; items?: string[]; tone: string }[] = [
    { title: "対立仮説 (別の可能性)", items: tc.alternatives, tone: "text-warning" },
    { title: "前提 (崩れると見立ても崩れる)", items: tc.key_assumptions, tone: "text-fg-muted" },
    { title: "覆る指標 (来週の監視対象)", items: tc.indicators, tone: "text-accent-hover" },
  ];

  return (
    <div id="syn-tradecraft" className="bg-surface-1 border border-accent-soft rounded-lg p-4 mb-5 scroll-mt-24">
      <h4 className="m-0 mb-3 text-md text-fg font-semibold tracking-tight flex items-center gap-2">
        分析トレードクラフト
        <span className="text-[12px] text-fg-subtle font-normal">— トンネル視を防ぐ別解・前提・監視指標 (ICD 203)</span>
      </h4>
      {tc.leading_assessment && (
        <div className="mb-3">
          <div className="text-[12px] text-fg-subtle uppercase tracking-wider font-semibold mb-1">主見立て</div>
          <SynthesisProse text={tc.leading_assessment} collapsedHeight={180} />
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {lists.map((l) =>
          l.items && l.items.length > 0 ? (
            <div key={l.title}>
              <div className={`text-[13px] font-semibold mb-1.5 ${l.tone}`}>
                {l.title}
              </div>
              <ul className="m-0 p-0 list-none space-y-1">
                {l.items.map((it, i) => (
                  <li key={i} className="text-sm text-fg leading-snug pl-3 -indent-3">
                    ・{it}
                  </li>
                ))}
              </ul>
            </div>
          ) : null,
        )}
      </div>
      {/* 構造的矯正: 注入信号 (出典信頼度/FC2予測/鮮度) への明示応答 */}
      {(tc.source_caveat || tc.forecast_alignment || tc.freshness_note) && (
        <div className="mt-3 pt-3 border-t border-border-subtle space-y-1.5">
          {tc.source_caveat && (
            <div className="text-[13px]">
              <span className="text-[12px] text-warning uppercase tracking-wider font-semibold mr-1.5">出典の割引</span>
              <span className="text-fg-muted">{tc.source_caveat}</span>
            </div>
          )}
          {tc.forecast_alignment && (
            <div className="text-[13px]">
              <span className="text-[12px] text-accent-hover uppercase tracking-wider font-semibold mr-1.5">予測整合</span>
              <span className="text-fg-muted">{tc.forecast_alignment}</span>
            </div>
          )}
          {tc.freshness_note && (
            <div className="text-[13px]">
              <span className="text-[12px] text-cyan-400 uppercase tracking-wider font-semibold mr-1.5">鮮度</span>
              <span className="text-fg-muted">{tc.freshness_note}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// 証拠駆動評価 (ACH) — 各判定の競合仮説採点・接地証拠 (attribution basis/tier/極性)・確度の根拠を
// 開示し、「主張 → どの本文に依拠するか」を analyst が照合できるようにする (本質的トレーサビリティ)。
// 確度ラベルは backend 配信 vocab "confidence" (vocabLabel) を SSoT に。ここは色 (tone) のみ保持。
// ACH 仮説ラベルは vocab "ach_hypothesis" (vocabLabel) を SSoT に解決する。
const GCONF_TONE: Record<string, string> = {
  high: "text-critical",
  moderate: "text-warning",
  low: "text-fg-subtle",
};

function GroundedEstimateSection({ est, generatedAt }: { est: GroundedEstimate; generatedAt?: string }) {
  if (!est.judgments?.length) return null;
  return (
    <div id="syn-ach" className="bg-surface-1 border border-accent-soft rounded-lg p-4 mb-5 scroll-mt-24">
      <h4 className="m-0 mb-3 text-md text-fg font-semibold tracking-tight flex items-center gap-2 flex-wrap">
        証拠駆動評価 (ACH)
        <span className="text-[12px] text-fg-subtle font-normal">
          — 競合仮説・根拠となる証拠・確度の根拠 (本文と照合可能・対称客観性)
        </span>
        {generatedAt && (
          <span className="text-[12px] text-fg-subtle font-normal ml-auto whitespace-nowrap">
            {formatJstDate(generatedAt)} 生成時点のスナップショット — 台帳更新がある判定には現在値を併記
          </span>
        )}
      </h4>
      <div className="space-y-2.5">
        {est.judgments.map((j) => (
          <GroundedJudgmentCard key={j.id} j={j} />
        ))}
      </div>
    </div>
  );
}

function GroundedJudgmentCard({ j }: { j: GroundedJudgment }) {
  const confTone = GCONF_TONE[j.confidence] ?? "text-fg-muted";
  // 状態分離: excerpt を持つ行 = ACH 評価済み証拠。旧レコードには割当だけの行
  // (excerpt 空・polarity=neutral) が混在するため、描画は評価済みに限定し、
  // 未評価分は件数で正直に示す (「中立の証拠」に見せない)。
  const assessedEvidence = j.evidence.filter((e) => e.excerpt);
  const unassessedCount = j.evidence.length - assessedEvidence.length + (j.unassessed_count ?? 0);
  return (
    <details className="border border-border-subtle rounded-md overflow-hidden">
      <summary className="list-none cursor-pointer px-3 py-2 bg-surface-2 hover:bg-surface-3 flex flex-wrap items-center gap-2 [&::-webkit-details-marker]:hidden">
        <span className="text-sm text-fg flex-1 min-w-0">{j.claim}</span>
        <span className="text-xs text-accent-hover">{vocabLabel("ach_hypothesis", j.leading_hypothesis)}</span>
        <span className={`text-[12px] font-bold ${confTone}`}>{vocabLabel("confidence", j.confidence)}</span>
        {j.adversarial_refuted && (
          <span className="text-[12px] text-warning" title={j.adversarial_note}>検証で反証</span>
        )}
        {/* 台帳現在値 (2026-07-25): このカードはレポート生成時点の凍結スナップショット。
            台帳がその後更新/是正されていれば現在値を chip で併記する (記録は改変しない)。 */}
        {j.ledger_now?.differs && (
          <span
            className="text-[12px] px-1.5 py-px rounded-sm bg-accent-subtle text-accent-hover font-semibold whitespace-nowrap"
            title={`このカードはレポート生成時点の評価。台帳は rev${j.ledger_now.rev} (${formatJstDate(j.ledger_now.updated_at)}) で更新済み`}
          >
            台帳の現在値: {vocabLabel("ach_hypothesis", j.ledger_now.leading_hypothesis)} / {vocabLabel("confidence", j.ledger_now.confidence)}
          </span>
        )}
      </summary>
      <div className="p-3 space-y-2">
        <div className="text-[13px] text-fg-subtle">確度の根拠: {j.confidence_basis}</div>
        {j.ledger_now?.differs && (
          <div className="text-[13px] text-accent-hover">
            この評価はレポート生成時点のスナップショットです。情勢台帳の現在値 (rev{j.ledger_now.rev} ·{" "}
            {formatJstDate(j.ledger_now.updated_at)}) は「
            {vocabLabel("ach_hypothesis", j.ledger_now.leading_hypothesis)} /{" "}
            {vocabLabel("confidence", j.ledger_now.confidence)}」に更新されています。
          </div>
        )}

        <div>
          <div className="text-[13px] font-semibold text-fg-muted mb-1">競合仮説 (ACH)</div>
          <ul className="m-0 p-0 list-none space-y-0.5">
            {j.hypotheses.map((h) => (
              <li key={h.hypothesis} className="text-[13px] flex items-center gap-2">
                <span
                  className={
                    h.verdict === "leading"
                      ? "text-accent-hover font-semibold"
                      : h.verdict === "refuted"
                        ? "text-fg-subtle line-through"
                        : "text-fg"
                  }
                >
                  {vocabLabel("ach_hypothesis", h.hypothesis)}
                </span>
                <span className="text-[12px] text-fg-subtle tnum">
                  整合{h.consistent}/反{h.inconsistent}
                </span>
                <span className="text-[12px] text-fg-subtle">{h.verdict === "leading" ? "主説" : h.verdict === "refuted" ? "反証" : h.verdict === "neutral" ? "中立" : h.verdict}</span>
              </li>
            ))}
          </ul>
        </div>

        {assessedEvidence.length > 0 && (
          <div>
            <div className="text-[13px] font-semibold text-fg-muted mb-1">根拠となる証拠 (本文と照合)</div>
            <ul className="m-0 p-0 list-none space-y-1">
              {assessedEvidence.map((e, i) => (
                <li key={i} className="text-[13px] leading-snug">
                  <span
                    className={`text-[12px] mr-1 font-semibold ${
                      e.polarity === "contradicts"
                        ? "text-critical"
                        : e.polarity === "supports"
                          ? "text-accent-hover"
                          : "text-fg-subtle"
                    }`}
                  >
                    [
                    {e.polarity === "supports"
                      ? "支持"
                      : e.polarity === "contradicts"
                        ? "反証"
                        : "中立"}
                    ]
                  </span>
                  <span className="text-[12px] text-fg-subtle mr-1">
                    {e.attribution_basis}/{e.source_tier}
                  </span>
                  <span className="text-fg">{e.excerpt}</span>
                  {e.article_id && (
                    <a
                      href={`/app/article/${encodeURIComponent(e.article_id)}`}
                      className="text-accent text-[13px] ml-1 no-underline hover:underline"
                    >
                      →本文
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {unassessedCount > 0 && (
          <div className="text-[13px] text-fg-subtle">
            ほかに未評価の割当記事 {unassessedCount} 件 — ACH は未読/未引用 (情勢台帳の証拠台帳で確認可能)
          </div>
        )}

        {j.missing_evidence.length > 0 && (
          <div className="text-[13.5px]">
            <span className="text-[12px] text-warning font-semibold mr-1">欠落証拠</span>
            {j.missing_evidence.join(" / ")}
          </div>
        )}
        {j.adversarial_note && (
          <div className="text-[13.5px]">
            <span className="text-[12px] text-fg-subtle font-semibold mr-1">検証(red-team)</span>
            {j.adversarial_note}
          </div>
        )}
      </div>
    </details>
  );
}

// B(2): 予測スコアカード — 前期予測の採点 (realized/partial/missed) + 的中率 + 今期の予測。
// spillover の予測を説明責任化し、的中率を累積して calibration を可視化する。
// label は vocab "forecast_verdict" (vocabLabel) を SSoT に。ここは色 (tone) のみ保持。
const _VERDICT_TONE: Record<string, string> = {
  realized: "text-accent",
  partial: "text-warning",
  missed: "text-critical",
  // 未照会は「外れ」ではないので critical にしない (的中率の分母からも外れている)
  unevaluated: "text-fg-subtle",
};

function ForecastScorecard({ tc, acc }: { tc: Tradecraft; acc?: ForecastAccuracy }) {
  // ⚠ 過去の採点は既定で畳む。全件描くと 241 件で 35,848px (33 画面) になり、
  //   先頭にあるはずの的中率と今期の予測が埋もれる (2026-08-29 実測)。
  //   **件数は畳んでいても常に出す** — 説明責任の面なので、隠していること自体は
  //   見えていなければならない。
  const [showPast, setShowPast] = useState(false);
  const past = tc.forecast_scorecard ?? [];
  return (
    <div id="syn-forecast" className="bg-surface-1 border border-border-subtle rounded-lg p-4 mb-5 scroll-mt-24">
      <h4 className="m-0 mb-3 text-md text-fg font-semibold tracking-tight flex items-center gap-2">
        予測スコアカード
        <span className="text-[12px] text-fg-subtle font-normal">— 予測の説明責任と的中率</span>
        {acc && acc.scored > 0 && acc.hit_rate_pct != null && (
          <span className="ml-auto text-xs tnum text-fg-muted">
            直近的中率{" "}
            <span className="font-bold text-fg">{acc.hit_rate_pct}%</span>
            <span className="text-fg-subtle">
              {" "}
              (現{acc.realized}/部{acc.partial}/外{acc.missed})
            </span>
            {/* 未照会は分母外。件数を出さないと「測っていない分」が的中率から黙って消える */}
            {acc.unevaluated > 0 && (
              <span
                className="text-fg-subtle"
                title="情勢が再評価されず、指標を一度も照会できなかった予測。外れではないため的中率の分母から除外している。"
              >
                {" "}
                ＋未照会{acc.unevaluated}(分母外)
              </span>
            )}
          </span>
        )}
      </h4>
      {tc.forecasts && tc.forecasts.length > 0 && (
        <div>
          <div className="text-[13px] font-semibold mb-1.5 text-accent-hover">今期の予測 (次期に採点)</div>
          <ul className="m-0 p-0 list-none space-y-1">
            {tc.forecasts.map((f, i) => (
              <li key={i} className="text-sm text-fg leading-snug pl-3 -indent-3">
                ・{f.claim}
                <span className="text-fg-subtle text-[13px]"> [{vocabLabel("confidence", f.confidence)}]</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {past.length > 0 && (
        <div className="mb-3">
          <button
            type="button"
            onClick={() => setShowPast((v) => !v)}
            className="text-[13px] font-semibold mb-1.5 text-fg-muted hover:text-fg bg-transparent border-0 p-0 cursor-pointer"
          >
            過去の採点 {past.length} 件 {showPast ? "を閉じる" : "の内訳を見る"}
          </button>
          {showPast && (
            <ul className="m-0 p-0 list-none space-y-1.5">
              {past.map((s, i) => (
                <li key={i} className="text-sm text-fg leading-snug">
                  <span
                    className={`text-[12px] font-bold mr-1.5 ${_VERDICT_TONE[s.verdict] ?? "text-fg-muted"}`}
                  >
                    {vocabLabel("forecast_verdict", s.verdict)}
                  </span>
                  {s.claim}
                  {s.reason && <span className="text-fg-subtle text-xs">（{s.reason}）</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// 軸別 evidence (drill-down): クリックで <details> を展開し、その軸の event を一覧表示。
// PMESII ページは撤去済のため遷移でなく**この場で展開**する (synthesis のエビデンスとして残す)。
// 各 event は article_ids があれば in-app 記事詳細へリンク。
function AxisCard({ axisId, events }: {
  axisId: string;
  events: { label: string; summary: string; article_ids?: string[]; source_basis?: SourceBasis }[];
}) {
  return (
    <details className="bg-surface-1 border border-border-subtle rounded-md overflow-hidden transition-colors hover:border-accent-soft">
      <summary
        className="list-none px-3.5 py-2.5 cursor-pointer bg-surface-2 text-fg font-semibold text-sm flex items-center justify-between hover:bg-surface-3 transition-colors [&::-webkit-details-marker]:hidden"
      >
        <span className="bg-accent-subtle text-accent-hover px-2 py-0.5 rounded-sm text-xs font-mono font-semibold">{axisId}</span>
        <span className="text-xs text-fg-muted tnum">{events.length} 件 ▾</span>
      </summary>
      <ul className="list-none m-0 p-2 px-3.5">
        {events.map((ev, i) => {
          const aid = ev.article_ids?.[0];
          const label = <span className="text-accent-hover font-semibold mr-1.5">{ev.label}</span>;
          return (
            <li key={i} className="py-1.5 border-b border-dashed border-border-subtle last:border-b-0 text-sm leading-snug flex items-baseline gap-1.5">
              {aid ? (
                <a href={`/app/article/${encodeURIComponent(aid)}`} className="min-w-0 flex-1 no-underline hover:opacity-80" title="記事詳細へ">
                  {label}<span className="text-fg hover:text-accent">{ev.summary}</span>
                </a>
              ) : (
                <span className="min-w-0 flex-1">{label}<span className="text-fg">{ev.summary}</span></span>
              )}
              <ConfidenceBadge sb={ev.source_basis} />
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function SkeletonRows() {
  return (
    <div className="space-y-3">
      {[1, 2, 3].map((i) => (
        <div key={i} className="h-24 bg-gradient-to-r from-surface-1 via-surface-3 to-surface-1 bg-[length:200%_100%] animate-shimmer rounded-lg" />
      ))}
    </div>
  );
}

// ===== Phase Diamond verify-spotlight: PIR Spotlight view =====

function SpotlightView() {
  const qc = useQueryClient();
  const flags = useRuntimeFlags();
  const { data, isLoading } = useQuery({
    queryKey: ["spotlight-list", "rolling7"],
    queryFn: () => spotlightApi.list("rolling7"),
  });

  // 並びは **該当件数の多い順**。今どこが動いているかが、並びそのもので分かる
  // (2026-08-29 利用者判断)。元配列は触らない。
  const items = [...(data?.items ?? [])].sort((a, b) => b.article_count - a.article_count);

  return (
    <div>
      {/* 期間の切替に従わないことを、節の側で明示する
          (上で日次/週次を選んでもここは変わらない — 黙って据え置くと故障に見える)。 */}
      <SectionHeading title="PIR 別の動向" note="直近 7 日で固定 · 該当の多い順" />

      {isLoading && <SkeletonRows />}

      {!isLoading && items.length === 0 && (
        <div className="bg-surface-1 border border-dashed border-border-default rounded-lg p-10 text-center text-fg-muted">
          <p className="m-0">まだ Spotlight が生成されていません</p>
          <p className="text-xs mt-2">
            次回の自動実行 (毎日 04:50 JST) で生成。すぐ作るには各 PIR の詳細画面の「Spotlight 手動実行」から
          </p>
        </div>
      )}

      {items.length > 0 && (
        // 2 カラムの格子 (公開ページと同じ密度)。1 カラムで縦に積むと、
        // 20 件では「並べているだけ」になり全体を見渡せない。
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3 items-start">
          {items.map((s) => (
            <SpotlightCard
              key={s.pir_id}
              spotlight={s}
              qc={qc}
              readOnly={!canEditOperationalConfig(flags)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function SpotlightCard({
  spotlight: s,
  qc,
  readOnly,
}: {
  spotlight: SpotlightSummary;
  qc: ReturnType<typeof useQueryClient>;
  readOnly: boolean;
}) {
  const [showCompare, setShowCompare] = useState(false);
  // ⚠ **全件畳んだ状態が既定** (2026-08-29 利用者指示)。1 枚だけ開いて置くと、
  //   その 1 件だけ扱いが違って見え、格子の並びも崩れる。
  //   見出しは 150-280 字あるので、畳んでいても中身は分かる。
  const [open, setOpen] = useState(false);

  const regenMain = useMutation({
    mutationFn: (model?: string) => spotlightApi.regenerate(s.pir_id, "rolling7", model),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spotlight-list"] }),
  });

  return (
    // 公開ページの記事カードと同じ組み立て: 小さなラベル → 太い見出し → 灰色の本文。
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 hover:border-border-default">
      {/* ラベル行 (公開ページの「● カテゴリ」に相当) */}
      <div className="flex items-center gap-2 mb-1.5 text-[12px]">
        <span className="w-1.5 h-1.5 rounded-full bg-accent shrink-0" aria-hidden />
        <a
          href={`/app/pir/${encodeURIComponent(s.pir_id)}`}
          className="font-semibold text-accent hover:text-accent-hover no-underline"
        >
          {s.pir_title}
        </a>
        <span className="flex-1" />
        <span className="text-fg-subtle tnum">該当 {s.article_count} 件</span>
      </div>

      {/* 見出し — 畳んでいても常に出す。これが一覧の中身。 */}
      <p className="m-0 mb-2 text-[15px] font-bold leading-[1.7] text-fg">{s.headline}</p>

      <div className="flex items-baseline gap-2 text-[12px] mb-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="text-accent hover:text-accent-hover bg-transparent border-0 p-0 cursor-pointer"
        >
          {open ? "閉じる" : `続きを読む · 主要事象 ${s.key_events.length} 件`}
        </button>
        <span className="flex-1" />
        <span className="text-fg-subtle">{formatJst(s.generated_at)}</span>
      </div>

      {open && (
        <>

      {/* Key events */}
      {s.key_events.length > 0 && (
        <div className="mb-3">
          <div className="text-[12px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">主要イベント ({s.key_events.length})</div>
          <ul className="m-0 p-0 list-none space-y-1">
            {s.key_events.map((ke) => (
              <li key={ke.article_id} className="flex items-baseline gap-2 text-sm">
                <span className={`text-[12px] px-1.5 py-0.5 rounded font-mono ${
                  ke.importance === "high" ? "bg-critical-soft text-critical" :
                  ke.importance === "medium" ? "bg-warning-soft text-warning" :
                  "bg-surface-3 text-fg-subtle"
                }`}>{ke.importance === "high" ? "高" : ke.importance === "medium" ? "中" : "低"}</span>
                <a href={ke.url} target="_blank" rel="noreferrer" className="text-fg hover:text-accent-hover flex-1 min-w-0 truncate">
                  {ke.title}
                </a>
                {/* S1: 出典基盤の確度バッジ (source メタから決定的に算出、reason は hover) */}
                <ConfidenceBadge sb={ke.source_basis} />
                <span className="text-[12px] text-fg-subtle">{ke.feed_title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Outlook — (a)〜(d) 構造の長文。読了幅 + 再段落化 + 折りたたみで表示 */}
      <div>
        <div className="text-[12px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">見通し</div>
        <SynthesisProse text={s.outlook} />
      </div>
        </>
      )}

      {/* 再生成は運用操作。読み物の一覧で毎カード見えると邪魔なので、開いたときだけ出す。 */}
      {!readOnly && open && (
        <div className="mt-3 pt-3 border-t border-border-subtle flex items-center gap-2 text-xs">
          <button
            onClick={() => setShowCompare((v) => !v)}
            className="inline-flex items-center gap-1 text-fg-muted hover:text-fg"
          ><RefreshCw className="h-3.5 w-3.5" />再生成 / モデル比較</button>
          {showCompare && (
            <>
              <span className="text-fg-subtle">→</span>
              <button
                onClick={() => regenMain.mutate(undefined)}
                disabled={regenMain.isPending}
                className="bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-fg disabled:opacity-50"
              >同じモデルで再生成</button>
              <button
                onClick={() => regenMain.mutate("gemma4:26b")}
                disabled={regenMain.isPending}
                className="bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-fg disabled:opacity-50"
              >26B で再生成</button>
              <button
                onClick={() => regenMain.mutate("gemma4:31b")}
                disabled={regenMain.isPending}
                className="bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-fg disabled:opacity-50"
              >31B で再生成</button>
              {regenMain.isPending && <span className="text-fg-muted">処理中 (~5-10 分)...</span>}
              {regenMain.isError && <span className="text-critical">エラー: {(regenMain.error as Error).message}</span>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
