// 常設情報要求 (問い) の面 — PIR ブリーフ 段A。
//
// **問いを主語にして読む面**。重要インフラ board の posture カードは見出しが国名・本文が
// 仮説ラベルで、問いは tooltip に隠れていた (docs/intelligence_requirements_layering.md §3b)。
// ここでは「我々は何を問うており、いまの答えは何で、前回から何がどう動いたか」の順で読む。
//
// **期間の産物ではない** — 日・週・月の窓を持たず、いま時点の答えと前回からの差分を示す
// (docs/pir_brief_design.md §1)。窓の切替 UI を置かないのは仕様。
//
// 表示は現況 (SynthesisTab) と同じ語彙に揃える: 答えの散文は `SynthesisProse`
// (文単位の判定行 + 確度チップ抽出 + 高さ折りたたみ)、節は**枠でなく区切り線**で切る。
// 箱を並べると、どれも同じ重さに見えて読み進む手掛かりが無くなるため。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { pageContainer } from "../components/Page";
import { SynthesisProse } from "../components/SynthesisProse";
import { vocabLabel } from "../hooks/useVocab";
import { questionsApi, type PostureCard, type QuestionIndicator } from "../api/jpci";
import { POSTURE_CONF_TONE } from "../components/jpci/Badges";

/** 指標の状態 → 表示 (open=まだ見えていない / hit=見えた / expired=期限切れ)。 */
const INDICATOR_TONE: Record<string, string> = {
  open: "text-fg-subtle",
  hit: "text-warning",
  expired: "text-fg-subtle opacity-60",
};
const INDICATOR_LABEL: Record<string, string> = {
  open: "未発火",
  hit: "発火",
  expired: "期限切れ",
};
/** 鮮度の警告線 (日)。予約枠の再評価 staleness と同じ基準。 */
const STALE_DAYS = 7;

function daysSince(iso: string): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86_400_000);
}

function isMoved(q: PostureCard): boolean {
  return q.delta_type !== "no_change" && q.delta_type !== "";
}

/** 節見出し + 本文。現況と同じく枠でなく区切り線で切る。 */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-border-subtle pt-3">
      <h4 className="m-0 mb-2 text-[13px] text-fg-muted font-semibold tracking-wide">{title}</h4>
      {children}
    </div>
  );
}

function Indicators({ items }: { items: QuestionIndicator[] }) {
  if (items.length === 0) {
    return <div className="text-[13px] text-fg-subtle">指標は立っていない</div>;
  }
  return (
    <ul className="m-0 p-0 list-none space-y-1">
      {items.map((i) => (
        <li key={`${i.indicator}-${i.opened_at}`} className="text-[13px] leading-relaxed">
          <span
            className={`text-[11px] font-semibold tracking-wide mr-2 ${
              INDICATOR_TONE[i.status] ?? INDICATOR_TONE.open
            }`}
          >
            {INDICATOR_LABEL[i.status] ?? i.status}
          </span>
          <span className="text-fg">{i.indicator}</span>
        </li>
      ))}
    </ul>
  );
}

function QuestionCard({ q }: { q: PostureCard }) {
  // 既定は「答え + 前回から」まで。根拠・指標・推移は畳む — 4 問が一望できることを優先し、
  // 深掘りは開いた 1 問に絞る (全部開いていると、どの問いが動いたかが埋もれる)。
  const [detail, setDetail] = useState(false);
  const [history, setHistory] = useState(false);
  const idle = daysSince(q.last_evidence_at);
  const moved = isMoved(q);

  return (
    <article className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
      <header className="space-y-1.5">
        <h2 className="m-0 text-[15px] leading-[1.7] font-bold text-fg tracking-tight">
          {q.question}
        </h2>
        <div className="text-[12px] text-fg-subtle flex flex-wrap gap-x-3 gap-y-1">
          <span
            className={`px-1.5 rounded border ${
              POSTURE_CONF_TONE[q.confidence] ?? "border-border-default text-fg-subtle"
            }`}
          >
            {vocabLabel("confidence", q.confidence)}
          </span>
          <span className={moved ? "text-warning font-semibold" : ""}>
            {moved ? vocabLabel("delta_type", q.delta_type) : "答えは動いていない"}
          </span>
          {idle !== null && (
            <span className={idle >= STALE_DAYS ? "text-warning" : ""}>
              最終証拠 {q.last_evidence_at.slice(0, 10)}
              {idle > 0 ? ` (${idle} 日前)` : " (本日)"}
            </span>
          )}
          <span>
            証拠 直接 {q.evidence_direct_30d} / 関連 {q.evidence_related_30d} 件 (30 日)
          </span>
        </div>
      </header>

      {!q.assessed ? (
        <p className="m-0 text-[13px] text-fg-subtle">
          帰属済みの証拠がまだ無く、評価していない (観測の不在は不在の証明ではない)
        </p>
      ) : (
        <>
          <Section title="いまの答え">
            <SynthesisProse text={q.claim || q.leading_label} collapsedHeight={160} />
          </Section>

          {(q.delta_note || q.fired_indicators.length > 0) && (
            <Section title="前回から">
              {q.delta_note && <SynthesisProse text={q.delta_note} collapsedHeight={120} />}
              {q.fired_indicators.length > 0 && (
                <ul className="m-0 mt-1 p-0 list-none space-y-0.5">
                  {q.fired_indicators.map((f) => (
                    <li key={f} className="text-[13px] text-fg leading-relaxed">
                      <span className="text-[11px] text-warning font-semibold tracking-wide mr-2">
                        観測
                      </span>
                      {f}
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          )}

          <button
            type="button"
            onClick={() => setDetail(!detail)}
            className="w-full text-left text-[12px] text-fg-muted hover:text-fg flex items-center gap-1 cursor-pointer"
          >
            {detail ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
            根拠と監視指標 (指標 {q.indicators.length} 件 / 推移 {q.trajectory.length} 版)
          </button>

          {detail && (
            <>
              <Section title="何が見えれば答えが変わるか">
                <Indicators items={q.indicators} />
              </Section>

              {q.missing_evidence.length > 0 && (
                <Section title="まだ分かっていないこと">
                  <ul className="m-0 p-0 list-none space-y-0.5">
                    {q.missing_evidence.map((m) => (
                      <li key={m} className="text-[13px] text-fg leading-relaxed">
                        {m}
                      </li>
                    ))}
                  </ul>
                </Section>
              )}

              {q.confidence_basis && (
                <Section title="確度の根拠">
                  <p className="m-0 text-[13px] text-fg-subtle leading-relaxed">
                    {q.confidence_basis}
                  </p>
                </Section>
              )}

              <Section title="答えの推移">
                <button
                  type="button"
                  onClick={() => setHistory(!history)}
                  className="text-[12px] text-fg-muted hover:text-fg flex items-center gap-1 cursor-pointer"
                >
                  {history ? (
                    <ChevronDown className="size-3.5" />
                  ) : (
                    <ChevronRight className="size-3.5" />
                  )}
                  {q.trajectory.length} 版
                </button>
                {history && (
                  <ol className="m-0 mt-2 p-0 list-none space-y-1 border-l border-border-subtle pl-3">
                    {[...q.trajectory].reverse().map((t) => (
                      <li key={t.rev} className="text-[12px] text-fg-subtle leading-relaxed">
                        <span className="text-fg-muted">{t.at.slice(0, 10)}</span>{" "}
                        {vocabLabel("confidence", t.confidence)}
                        {t.note ? ` — ${t.note}` : ""}
                      </li>
                    ))}
                  </ol>
                )}
              </Section>
            </>
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
  // 動いた問いを先に (静穏な問いは下へ)。**静かな問いも省略はしない** —「静か≠安全」。
  const ordered = [...questions].sort(
    (a, b) =>
      Number(isMoved(b)) - Number(isMoved(a)) || a.situation_id.localeCompare(b.situation_id)
  );

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <header className="bg-surface-1 border border-border-subtle rounded-lg p-4">
        <p className="m-0 text-[19px] leading-[1.75] font-bold text-fg">
          {summary.total} 問中 {summary.moved} 問の答えが前回の評価から動いた
        </p>
        <div className="mt-2 text-[12px] text-fg-subtle flex flex-wrap gap-x-3 gap-y-1">
          <span>継続して追う問いと、いま時点の答え</span>
          <span>期間で区切らず、これまでに知り得たすべてから導いた現在の推定</span>
          {summary.unassessed > 0 && <span>未評価 {summary.unassessed} 問</span>}
        </div>
      </header>
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
        {ordered.map((q) => (
          <QuestionCard key={q.situation_id} q={q} />
        ))}
      </div>
    </div>
  );
}
