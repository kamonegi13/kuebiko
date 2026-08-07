import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useFilters } from "../../state/filters";
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
import { useRuntimeFlags } from "../../hooks/useRuntimeFlags";
import { ConfidenceBadge } from "../ConfidenceBadge";
import { intelHref } from "../../utils/intelNav";
import { vocabLabel } from "../../hooks/useVocab";

export function SynthesisTab() {
  // 全体総括 / Spotlight の切替は上部コントロールバー (Shell) が担う。ここは選択値を読むだけ。
  const view = useFilters((s) => s.synthesisView);
  if (view === "ledger") return <LedgerView />;
  return <div>{view === "global" ? <GlobalSynthesisView /> : <SpotlightView />}</div>;
}

function GlobalSynthesisView() {
  const f = useFilters();
  const { data, isLoading } = useQuery({
    queryKey: ["synthesis", f.period_type],
    queryFn: () => api.synthesis(f.period_type),
  });

  return (
    <div>
      {/* 期間 (日/週/月) の切替は上部コントロールバー (Shell) が担う。 */}
      <div className="mb-3 px-1 flex items-baseline justify-between flex-wrap gap-2">
        <h3 className="m-0 text-lg font-bold text-fg tracking-tight">状況総括</h3>
        {/* セクションジャンプ (長文レポートの目次) */}
        {data?.has_data && (
          <nav className="flex flex-wrap gap-1.5 text-[11px]">
            {[
              ["#syn-narrative", "総括本文"],
              ["#syn-tradecraft", "トレードクラフト"],
              ["#syn-ach", "ACH"],
              ["#syn-forecast", "予測"],
              ["#syn-evidence", "証拠"],
            ].map(([href, label]) => (
              <a
                key={href}
                href={href}
                className="px-2 py-0.5 rounded-full bg-surface-2 border border-border-subtle text-fg-muted hover:text-fg hover:border-border-default no-underline"
              >
                {label}
              </a>
            ))}
          </nav>
        )}
      </div>

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
          <div className="bg-accent-subtle border border-accent-soft border-l-[3px] border-l-accent rounded-lg p-5 mb-5">
            <div className="text-[11px] text-accent-hover uppercase tracking-wider font-semibold mb-1.5">見出し</div>
            <div className="text-md leading-[1.8] text-fg">{data.latest.headline}</div>
            <div className="mt-2.5 text-[11px] text-fg-subtle flex flex-wrap gap-3.5">
              <span><strong className="text-fg-muted font-semibold">期間:</strong> {formatJstDate(data.latest.period_start)} 〜 {formatJstDate(data.latest.period_end)}</span>
              <span>
                <strong className="text-fg-muted font-semibold">根拠:</strong> {data.latest.article_count} 記事
                {data.tradecraft?.grounded_estimate?.considered_count
                  ? ` / 考慮 ${data.tradecraft.grounded_estimate.considered_count}`
                  : ""}
              </span>
              <span><strong className="text-fg-muted font-semibold">生成:</strong> {formatJst(data.latest.generated_at)}</span>
              <span><strong className="text-fg-muted font-semibold">モデル:</strong> <code className="text-[11px]">{data.latest.llm_model}</code></span>
            </div>
          </div>

          {/* Sections — 2 カラム grid。行内のカード高さは stretch で揃える (隙間ガタつき防止)。
              奇数個の最後 (6. PIR) は全幅 + 内部 2 段組で空きスロットを作らない。 */}
          <div id="syn-narrative" className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-5 scroll-mt-24">
            <Section title="2. 軸別の重みと不均衡" body={data.latest.weight_section} />
            <Section title="3. 軸間連鎖の解釈" body={data.latest.chain_section} />
            <Section title="4. 重心 (Center of Gravity)" body={data.latest.cog_section} />
            <Section title="5. 波及解釈 (中期予想)" body={data.latest.spillover_section} />
            <Section title="6. PIR 達成度" body={data.latest.pir_section} className="lg:col-span-2" columns />
          </div>

          {/* S2: 分析トレードクラフト (対立仮説・前提・覆る指標) */}
          {data.tradecraft && <TradecraftSection tc={data.tradecraft} />}

          {/* 証拠駆動評価 (ACH): 各判定の競合仮説・接地証拠・確度の根拠 (本文と照合可能) */}
          {data.tradecraft?.grounded_estimate && (
            <GroundedEstimateSection
              est={data.tradecraft.grounded_estimate}
              generatedAt={data.latest.generated_at}
            />
          )}

          {/* B(2): 予測スコアカード (前期予測の採点 + 的中率 + 今期の予測) */}
          {data.tradecraft &&
            (data.tradecraft.forecast_scorecard?.length ||
              data.tradecraft.forecasts?.length) && (
              <ForecastScorecard tc={data.tradecraft} acc={data.forecast_accuracy} />
            )}

          {/* Axes evidence */}
          {data.axes_evidence && Object.keys(data.axes_evidence).length > 0 && (
            <div id="syn-evidence" className="scroll-mt-24">
              <div className="flex items-baseline justify-between gap-2 mb-2.5">
                <h4 className="m-0 text-md text-fg font-semibold tracking-tight">7. 軸別の証拠 (深掘り)</h4>
                {/* ページ間 cross-nav の一貫性: 現況から関連ビュー (国家情勢 = 全 PMESII 軸) へ */}
                <a href={intelHref("pmesii")} className="text-xs text-accent hover:text-accent-hover no-underline shrink-0">
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
        </>
      )}
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
    <div className={`bg-surface-1 border border-border-subtle rounded-lg p-4 transition-colors hover:border-border-default ${className}`}>
      <h4 className="m-0 mb-2.5 text-[11px] text-accent-hover uppercase tracking-wider font-semibold pb-1.5 border-b border-border-subtle">{title}</h4>
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
        <span className="text-[10px] text-fg-subtle font-normal">— トンネル視を防ぐ別解・前提・監視指標 (ICD 203)</span>
      </h4>
      {tc.leading_assessment && (
        <div className="mb-3">
          <div className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1">主見立て</div>
          <SynthesisProse text={tc.leading_assessment} collapsedHeight={180} />
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {lists.map((l) =>
          l.items && l.items.length > 0 ? (
            <div key={l.title}>
              <div className={`text-[11px] font-semibold mb-1.5 ${l.tone}`}>
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
              <span className="text-[10px] text-warning uppercase tracking-wider font-semibold mr-1.5">出典の割引</span>
              <span className="text-fg-muted">{tc.source_caveat}</span>
            </div>
          )}
          {tc.forecast_alignment && (
            <div className="text-[13px]">
              <span className="text-[10px] text-accent-hover uppercase tracking-wider font-semibold mr-1.5">予測整合</span>
              <span className="text-fg-muted">{tc.forecast_alignment}</span>
            </div>
          )}
          {tc.freshness_note && (
            <div className="text-[13px]">
              <span className="text-[10px] text-cyan-400 uppercase tracking-wider font-semibold mr-1.5">鮮度</span>
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
        <span className="text-[10px] text-fg-subtle font-normal">
          — 競合仮説・根拠となる証拠・確度の根拠 (本文と照合可能・対称客観性)
        </span>
        {generatedAt && (
          <span className="text-[10px] text-fg-subtle font-normal ml-auto whitespace-nowrap">
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
        <span className={`text-[10px] font-bold ${confTone}`}>{vocabLabel("confidence", j.confidence)}</span>
        {j.adversarial_refuted && (
          <span className="text-[10px] text-warning" title={j.adversarial_note}>検証で反証</span>
        )}
        {/* 台帳現在値 (2026-07-25): このカードはレポート生成時点の凍結スナップショット。
            台帳がその後更新/是正されていれば現在値を chip で併記する (記録は改変しない)。 */}
        {j.ledger_now?.differs && (
          <span
            className="text-[10px] px-1.5 py-px rounded-sm bg-accent-subtle text-accent-hover font-semibold whitespace-nowrap"
            title={`このカードはレポート生成時点の評価。台帳は rev${j.ledger_now.rev} (${formatJstDate(j.ledger_now.updated_at)}) で更新済み`}
          >
            台帳の現在値: {vocabLabel("ach_hypothesis", j.ledger_now.leading_hypothesis)} / {vocabLabel("confidence", j.ledger_now.confidence)}
          </span>
        )}
      </summary>
      <div className="p-3 space-y-2">
        <div className="text-[11px] text-fg-subtle">確度の根拠: {j.confidence_basis}</div>
        {j.ledger_now?.differs && (
          <div className="text-[11px] text-accent-hover">
            この評価はレポート生成時点のスナップショットです。情勢台帳の現在値 (rev{j.ledger_now.rev} ·{" "}
            {formatJstDate(j.ledger_now.updated_at)}) は「
            {vocabLabel("ach_hypothesis", j.ledger_now.leading_hypothesis)} /{" "}
            {vocabLabel("confidence", j.ledger_now.confidence)}」に更新されています。
          </div>
        )}

        <div>
          <div className="text-[11px] font-semibold text-fg-muted mb-1">競合仮説 (ACH)</div>
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
                <span className="text-[10px] text-fg-subtle tnum">
                  整合{h.consistent}/反{h.inconsistent}
                </span>
                <span className="text-[10px] text-fg-subtle">{h.verdict === "leading" ? "主説" : h.verdict === "refuted" ? "反証" : h.verdict === "neutral" ? "中立" : h.verdict}</span>
              </li>
            ))}
          </ul>
        </div>

        {assessedEvidence.length > 0 && (
          <div>
            <div className="text-[11px] font-semibold text-fg-muted mb-1">根拠となる証拠 (本文と照合)</div>
            <ul className="m-0 p-0 list-none space-y-1">
              {assessedEvidence.map((e, i) => (
                <li key={i} className="text-[13px] leading-snug">
                  <span
                    className={`text-[10px] mr-1 font-semibold ${
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
                  <span className="text-[10px] text-fg-subtle mr-1">
                    {e.attribution_basis}/{e.source_tier}
                  </span>
                  <span className="text-fg">{e.excerpt}</span>
                  {e.article_id && (
                    <a
                      href={`/app/article/${encodeURIComponent(e.article_id)}`}
                      className="text-accent text-[11px] ml-1 no-underline hover:underline"
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
          <div className="text-[11px] text-fg-subtle">
            ほかに未評価の割当記事 {unassessedCount} 件 — ACH は未読/未引用 (情勢台帳の証拠台帳で確認可能)
          </div>
        )}

        {j.missing_evidence.length > 0 && (
          <div className="text-[12px]">
            <span className="text-[10px] text-warning font-semibold mr-1">欠落証拠</span>
            {j.missing_evidence.join(" / ")}
          </div>
        )}
        {j.adversarial_note && (
          <div className="text-[12px]">
            <span className="text-[10px] text-fg-subtle font-semibold mr-1">検証(red-team)</span>
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
};

function ForecastScorecard({ tc, acc }: { tc: Tradecraft; acc?: ForecastAccuracy }) {
  return (
    <div id="syn-forecast" className="bg-surface-1 border border-border-subtle rounded-lg p-4 mb-5 scroll-mt-24">
      <h4 className="m-0 mb-3 text-md text-fg font-semibold tracking-tight flex items-center gap-2">
        予測スコアカード
        <span className="text-[10px] text-fg-subtle font-normal">— 予測の説明責任と的中率</span>
        {acc && acc.scored > 0 && acc.hit_rate_pct != null && (
          <span className="ml-auto text-xs tnum text-fg-muted">
            直近的中率{" "}
            <span className="font-bold text-fg">{acc.hit_rate_pct}%</span>
            <span className="text-fg-subtle">
              {" "}
              (現{acc.realized}/部{acc.partial}/外{acc.missed})
            </span>
          </span>
        )}
      </h4>
      {tc.forecast_scorecard && tc.forecast_scorecard.length > 0 && (
        <div className="mb-3">
          <div className="text-[11px] font-semibold mb-1.5 text-fg-subtle">前期予測の採点</div>
          <ul className="m-0 p-0 list-none space-y-1.5">
            {tc.forecast_scorecard.map((s, i) => (
              <li key={i} className="text-sm text-fg leading-snug">
                <span
                  className={`text-[10px] font-bold mr-1.5 ${_VERDICT_TONE[s.verdict] ?? "text-fg-muted"}`}
                >
                  {vocabLabel("forecast_verdict", s.verdict)}
                </span>
                {s.claim}
                {s.reason && <span className="text-fg-subtle text-xs">（{s.reason}）</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {tc.forecasts && tc.forecasts.length > 0 && (
        <div>
          <div className="text-[11px] font-semibold mb-1.5 text-accent-hover">今期の予測 (次期に採点)</div>
          <ul className="m-0 p-0 list-none space-y-1">
            {tc.forecasts.map((f, i) => (
              <li key={i} className="text-sm text-fg leading-snug pl-3 -indent-3">
                ・{f.claim}
                <span className="text-fg-subtle text-[11px]"> [{vocabLabel("confidence", f.confidence)}]</span>
              </li>
            ))}
          </ul>
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
  const { read_only } = useRuntimeFlags();
  const { data, isLoading } = useQuery({
    queryKey: ["spotlight-list", "weekly"],
    queryFn: () => spotlightApi.list("weekly"),
  });

  return (
    <div>
      <div className="flex justify-between items-baseline mb-4 px-1 flex-wrap gap-2">
        <div>
          <h3 className="m-0 text-lg font-bold text-fg tracking-tight">PIR Spotlight — 縦断的なまとめ</h3>
          <p className="m-0 mt-1 text-fg-muted text-xs">
            各 PIR を切り口とした戦術-作戦レベルの深掘り。全体状況総括 (横断) の補完。
            毎週月曜 09:00 JST に自動で再生成。
          </p>
        </div>
        <span className="text-fg-subtle text-xs">期間: 週次</span>
      </div>

      {isLoading && <SkeletonRows />}

      {!isLoading && (!data || data.items.length === 0) && (
        <div className="bg-surface-1 border border-dashed border-border-default rounded-lg p-10 text-center text-fg-muted">
          <p className="m-0">まだ Spotlight が生成されていません</p>
          <p className="text-xs mt-2">
            次回の自動実行 (月曜 09:00 JST) で生成。すぐ作るには各 PIR の詳細画面の「Spotlight 手動実行」から
          </p>
        </div>
      )}

      {data && data.items.length > 0 && (
        <div className="space-y-4">
          {data.items.map((s) => (
            <SpotlightCard key={s.pir_id} spotlight={s} qc={qc} readOnly={read_only} />
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

  const regenMain = useMutation({
    mutationFn: (model?: string) => spotlightApi.regenerate(s.pir_id, "weekly", model),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spotlight-list"] }),
  });

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4">
      {/* Header */}
      <div className="flex items-baseline justify-between mb-2 flex-wrap gap-2">
        <h4 className="m-0 text-md font-bold text-fg">{s.pir_title}</h4>
        <div className="flex items-center gap-2 text-[11px] text-fg-subtle">
          <span>生成 {formatJst(s.generated_at)}</span>
          <span>·</span>
          <span>記事数 {s.article_count}</span>
          <span>·</span>
          <code className="text-[10px] bg-surface-2 px-1.5 py-0.5 rounded">{s.llm_model}</code>
          <a href={`/app/pir/${encodeURIComponent(s.pir_id)}`} className="text-fg-muted hover:text-accent-hover no-underline ml-1">→ PIR</a>
        </div>
      </div>

      {/* Headline */}
      <div className="bg-accent-subtle border-l-[3px] border-l-accent rounded p-3 mb-3">
        <div className="text-[10px] text-accent-hover uppercase tracking-wider font-semibold mb-1">見出し</div>
        <div className="text-fg text-base leading-relaxed">{s.headline}</div>
      </div>

      {/* Key events */}
      {s.key_events.length > 0 && (
        <div className="mb-3">
          <div className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">主要イベント ({s.key_events.length})</div>
          <ul className="m-0 p-0 list-none space-y-1">
            {s.key_events.map((ke) => (
              <li key={ke.article_id} className="flex items-baseline gap-2 text-sm">
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-mono ${
                  ke.importance === "high" ? "bg-critical-soft text-critical" :
                  ke.importance === "medium" ? "bg-warning-soft text-warning" :
                  "bg-surface-3 text-fg-subtle"
                }`}>{ke.importance === "high" ? "高" : ke.importance === "medium" ? "中" : "低"}</span>
                <a href={ke.url} target="_blank" rel="noreferrer" className="text-fg hover:text-accent-hover flex-1 min-w-0 truncate">
                  {ke.title}
                </a>
                {/* S1: 出典基盤の確度バッジ (source メタから決定的に算出、reason は hover) */}
                <ConfidenceBadge sb={ke.source_basis} />
                <span className="text-[10px] text-fg-subtle">{ke.feed_title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Outlook — (a)〜(d) 構造の長文。読了幅 + 再段落化 + 折りたたみで表示 */}
      <div>
        <div className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">見通し</div>
        <SynthesisProse text={s.outlook} />
      </div>

      {/* Regenerate controls (LLM 比較用) */}
      {!readOnly && (
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
