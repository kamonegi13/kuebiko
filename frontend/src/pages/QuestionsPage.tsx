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
import { SectionHeading } from "../components/SectionHeading";
import { SynthesisProse } from "../components/SynthesisProse";
import { vocabLabel } from "../hooks/useVocab";
import {
  questionsApi,
  type PostureCard,
  type PostureTrajectoryPoint,
  type QuestionIndicator,
} from "../api/jpci";
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

/** 動いた問いの最新の動き (継続でない改訂)。判定そのものはサーバの moved_ids が正。 */
function latestMove(q: PostureCard): PostureTrajectoryPoint | undefined {
  return [...q.trajectory].reverse().find((t) => t.delta_type !== "no_change" && t.delta_type !== "");
}

/** 推移の要約: 30 日の確度の始点→終点と、動いた回数。月次の状況総括の代わりに読む長期の軌跡。 */
function trajectorySummary(q: PostureCard): string {
  const t = q.trajectory;
  if (t.length === 0) return "";
  const first = vocabLabel("confidence", t[0].confidence);
  const last = vocabLabel("confidence", t[t.length - 1].confidence);
  const moves = t.filter((x) => x.delta_type !== "no_change" && x.delta_type !== "").length;
  const span = first === last ? `確度は ${last} のまま` : `確度 ${first} → ${last}`;
  return `${t[0].at.slice(5, 10)} 以降 ${t.length} 回評価・${span}・動き ${moves} 回`;
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

function QuestionCard({ q, moved }: { q: PostureCard; moved: boolean }) {
  // 既定は「答え + 前回から」まで。根拠・指標・推移は畳む — 4 問が一望できることを優先し、
  // 深掘りは開いた 1 問に絞る (全部開いていると、どの問いが動いたかが埋もれる)。
  const [detail, setDetail] = useState(false);
  const [history, setHistory] = useState(false);
  const idle = daysSince(q.last_evidence_at);
  const move = moved ? latestMove(q) : undefined;

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
            {move ? `${vocabLabel("delta_type", move.delta_type)} (24 時間以内)` : "24 時間は動いていない"}
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
                  {trajectorySummary(q)}
                </button>
                {history && (
                  <ol className="m-0 mt-2 p-0 list-none space-y-1 border-l border-border-subtle pl-3">
                    {[...q.trajectory].reverse().map((t) => (
                      <li key={t.rev} className="text-[12px] text-fg-subtle leading-relaxed">
                        <span className="text-fg-muted">{t.at.slice(0, 10)}</span>{" "}
                        {vocabLabel("confidence", t.confidence)}
                        {t.delta_type !== "no_change" && t.delta_type !== "" && (
                          <span className="text-warning"> {vocabLabel("delta_type", t.delta_type)}</span>
                        )}
                        {(t.reason ?? t.note) ? ` — ${t.reason ?? t.note}` : ""}
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
  // 「動いた / 動いていない」で節を分ける (現況の 根拠 / 点検 と同じ節立て)。
  // **静かな問いも省略はしない** —「静か≠安全」。節を分けるのは畳むためではなく、
  // 読者が「今日どこを読むべきか」を最初の一瞥で決められるようにするため。
  const ordered = [...questions].sort((a, b) => a.situation_id.localeCompare(b.situation_id));

  const movedIds = new Set(summary.moved_ids ?? []);
  const moved = ordered.filter((q) => movedIds.has(q.situation_id));
  const quiet = ordered.filter((q) => !movedIds.has(q.situation_id));

  return (
    <div className={pageContainer("wide")}>
      {/* リード: 現況と同じ扱い — **枠で囲まず**見出しそのものを大きく出し、素性を下に小さく。
          全般 (ページ全体の要約) を箱にすると、下の個別カードと同じ重さに見えてしまう。 */}
      <div className="mb-5">
        <p className="m-0 text-[19px] leading-[1.75] font-bold text-fg">
          本日、{summary.total} 問中 {summary.moved} 問の答えが動いた
        </p>
        <div className="mt-2 text-[12px] text-fg-subtle flex flex-wrap gap-x-3 gap-y-1">
          <span>PIR (優先情報要求) = 継続して追う問い</span>
          <span>「動いた」= 直近 {summary.window_hours ?? 24} 時間に答え・確度・見立てが変わった</span>
          <span>期間で区切らない — これまでに知り得たすべてから導いた現在の推定</span>
          {summary.unassessed > 0 && <span>未評価 {summary.unassessed} 問</span>}
        </div>
      </div>

      {moved.length > 0 && (
        <section className="mb-6">
          {/* sticky: 長いカードを読み進むと、いま「動いた問い」を見ているのか
              「動いていない問い」なのかが分からなくなる (2026-08-29 と同じ理由)。 */}
          <SectionHeading title="動いた PIR" note={`${moved.length} 問`} sticky />
          <p className="mt-0 mb-4 text-[13px] text-fg-subtle">
            直近 24 時間に答え・確度・見立てのいずれかが変わったもの。何がそれを動かしたかを併記する。
          </p>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            {moved.map((q) => (
              <QuestionCard key={q.situation_id} q={q} moved={movedIds.has(q.situation_id)} />
            ))}
          </div>
        </section>
      )}

      {quiet.length > 0 && (
        <section>
          <SectionHeading title="動いていない PIR" note={`${quiet.length} 問`} sticky />
          <p className="mt-0 mb-4 text-[13px] text-fg-subtle">
            24 時間、答えは変わっていない。静かであることは安全を意味しない —
            鮮度と、何が見えれば答えが変わるかを併せて見る。
          </p>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            {quiet.map((q) => (
              <QuestionCard key={q.situation_id} q={q} moved={movedIds.has(q.situation_id)} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
