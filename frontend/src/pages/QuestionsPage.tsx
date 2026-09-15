// 常設情報要求 (問い) の面 — PIR ブリーフ 段A。
//
// **問いを主語にして読む面**。重要インフラ board の posture カードは見出しが国名・本文が
// 仮説ラベルで、問いは tooltip に隠れていた (docs/intelligence_requirements_layering.md §3b)。
// ここでは「我々は何を問うており、いまの答えは何で、前回から何がどう動いたか」の順で読む。
//
// **期間の産物ではない** — 日・週・月の窓を持たず、いま時点の答えと前回からの差分を示す
// (docs/pir_brief_design.md §1)。窓の切替 UI を置かないのは仕様。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, HelpCircle } from "lucide-react";
import { pageContainer } from "../components/Page";
import { vocabLabel } from "../hooks/useVocab";
import { questionsApi, type PostureCard, type QuestionIndicator } from "../api/jpci";
import { POSTURE_CONF_TONE } from "../components/jpci/Badges";

/** 指標の状態 → 表示 (open=まだ見えていない / hit=見えた / expired=期限切れ)。 */
const INDICATOR_TONE: Record<string, string> = {
  open: "border-border-default text-fg-subtle",
  hit: "border-warn-border text-warn-fg bg-warn-bg",
  expired: "border-border-subtle text-fg-subtle opacity-60",
};
const INDICATOR_LABEL: Record<string, string> = {
  open: "未発火",
  hit: "発火",
  expired: "期限切れ",
};

function daysSince(iso: string): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86_400_000);
}

/** 鮮度。静穏な問いほど重要 — 古いことでなく、古いと分からないことが危険。 */
function Freshness({ at }: { at: string }) {
  const d = daysSince(at);
  if (d === null) return null;
  const stale = d >= 7;
  return (
    <span className={`text-[12px] ${stale ? "text-warn-fg" : "text-fg-subtle"}`}>
      最終証拠 {at.slice(0, 10)}
      {d > 0 ? ` (${d} 日前)` : " (本日)"}
    </span>
  );
}

function Indicators({ items }: { items: QuestionIndicator[] }) {
  if (items.length === 0) {
    return <div className="text-[12px] text-fg-subtle">指標は立っていない</div>;
  }
  return (
    <ul className="space-y-1">
      {items.map((i) => (
        <li key={`${i.indicator}-${i.opened_at}`} className="flex items-start gap-2">
          <span
            className={`text-[11px] px-1.5 py-0.5 rounded border whitespace-nowrap mt-0.5 ${
              INDICATOR_TONE[i.status] ?? INDICATOR_TONE.open
            }`}
          >
            {INDICATOR_LABEL[i.status] ?? i.status}
          </span>
          <span className="text-[13px] text-fg leading-relaxed">{i.indicator}</span>
        </li>
      ))}
    </ul>
  );
}

function QuestionCard({ q }: { q: PostureCard }) {
  const [open, setOpen] = useState(false);
  return (
    <article className="border border-border-subtle rounded-lg p-4 space-y-3">
      {/* 問い — この面では見出し。tooltip ではない */}
      <h2 className="text-[15px] font-bold text-fg leading-relaxed flex items-start gap-2">
        <HelpCircle className="size-4 mt-1 shrink-0 text-fg-subtle" aria-hidden />
        <span>{q.question}</span>
      </h2>

      {!q.assessed ? (
        <div className="text-[13px] text-fg-subtle">
          帰属済みの証拠がまだ無く、評価していない (観測の不在は不在の証明ではない)
        </div>
      ) : (
        <>
          <div className="space-y-1.5">
            <div className="text-[12px] text-fg-subtle">いまの答え</div>
            <div className="text-[14px] text-fg leading-relaxed">{q.claim || q.leading_label}</div>
            <div className="flex items-center gap-2 flex-wrap">
              <span
                className={`text-[12px] px-1.5 py-0.5 rounded border whitespace-nowrap ${
                  POSTURE_CONF_TONE[q.confidence] ?? "border-border-default text-fg-subtle"
                }`}
              >
                {vocabLabel("confidence", q.confidence)}
              </span>
              <span className="text-[12px] text-fg-subtle">{q.leading_label}</span>
              {q.confidence_basis && (
                <span className="text-[12px] text-fg-subtle">根拠: {q.confidence_basis}</span>
              )}
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="text-[12px] text-fg-subtle">前回から</div>
            <div className="flex items-start gap-2 flex-wrap">
              <span className="text-[12px] px-1.5 py-0.5 rounded border border-border-default text-fg-muted whitespace-nowrap">
                {vocabLabel("delta_type", q.delta_type)}
              </span>
              <span className="text-[13px] text-fg leading-relaxed">
                {q.delta_note || (q.delta_type === "no_change" ? "答えは動いていない" : "—")}
              </span>
            </div>
          </div>

          {q.fired_indicators.length > 0 && (
            <div className="space-y-1">
              <div className="text-[12px] text-fg-subtle">前回評価以降に観測されたもの</div>
              <ul className="list-disc list-inside text-[13px] text-fg space-y-0.5">
                {q.fired_indicators.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="space-y-1.5">
            <div className="text-[12px] text-fg-subtle">何が見えれば答えが変わるか</div>
            <Indicators items={q.indicators} />
          </div>

          {q.missing_evidence.length > 0 && (
            <div className="space-y-1">
              <div className="text-[12px] text-fg-subtle">まだ分かっていないこと</div>
              <ul className="list-disc list-inside text-[13px] text-fg space-y-0.5">
                {q.missing_evidence.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="flex items-center gap-3 flex-wrap text-[12px] text-fg-subtle border-t border-border-subtle pt-2">
            <span>
              証拠 直接 (日本) {q.evidence_direct_30d} / 関連 {q.evidence_related_30d} 件 (30 日)
            </span>
            <Freshness at={q.last_evidence_at} />
          </div>

          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="text-[12px] text-accent hover:underline flex items-center gap-1"
          >
            {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
            答えの推移 ({q.trajectory.length} 版)
          </button>
          {open && (
            <ol className="space-y-1.5 border-l border-border-subtle pl-3">
              {[...q.trajectory].reverse().map((t) => (
                <li key={t.rev} className="text-[12px] text-fg-subtle">
                  <span className="text-fg-muted">{t.at.slice(0, 10)}</span>{" "}
                  {vocabLabel("confidence", t.confidence)}
                  {t.note ? ` — ${t.note}` : ""}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </article>
  );
}

export function QuestionsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["questions"],
    queryFn: questionsApi.list,
    refetchInterval: 120_000,
  });

  if (isLoading) return <div className={pageContainer("wide")}>読み込み中…</div>;
  if (error) return <div className={pageContainer("wide")}>読み込みに失敗しました</div>;
  if (!data) return null;

  const { questions, summary } = data;
  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <header className="space-y-1">
        <h1 className="text-lg font-bold text-fg">問い (常設情報要求)</h1>
        <p className="text-[13px] text-fg-muted leading-relaxed">
          継続して追う問いと、いま時点の答え。期間で区切らず、これまでに知り得たすべてから
          導いた現在の推定を示す。
        </p>
        <p className="text-[13px] text-fg">
          {summary.total} 問中 <span className="font-bold">{summary.moved} 問</span> の答えが
          前回の評価から動いた
          {summary.unassessed > 0 && ` (未評価 ${summary.unassessed} 問)`}
        </p>
      </header>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        {questions.map((q) => (
          <QuestionCard key={q.situation_id} q={q} />
        ))}
      </div>
    </div>
  );
}
