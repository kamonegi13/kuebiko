// 段D (2026-09-29): 朝ブリーフの PIR ブリーフ = 常設の問いへの「いまの答え」。
// SIR (状況総括 = 窓の中で何が届いたか) の前に置き、状態を読んでから流れを読ませる。
// 動いた問いは理由つきで全文、動かない問いは 1 行 (答え + 鮮度 + 待っている指標)。
// 静穏日に「0 問」と明示できることが要件 (静か≠安全 — 古いと分からないことが危険)。

import { POSTURE_CONF_TONE } from "../../components/jpci/Badges";
import { vocabLabel } from "../../hooks/useVocab";
import { SUBHEAD } from "../../components/headings";
import type { PirBriefPayload, PirBriefQuestion } from "../../api/types";

const QUESTIONS_PATH = "/app/questions";
/** これ以上証拠が来ていない問いは鮮度を警告色で出す (問いの面と同じ目安)。 */
const STALE_DAYS = 7;

function ConfidenceChip({ confidence }: { confidence: string }) {
  return (
    <span
      className={`text-[12px] px-1.5 rounded border whitespace-nowrap ${
        POSTURE_CONF_TONE[confidence] ?? "border-border-default text-fg-subtle"
      }`}
    >
      {vocabLabel("confidence", confidence)}
    </span>
  );
}

function freshness(days: number | null): string {
  if (days === null) return "証拠の日付不明";
  return days === 0 ? "証拠は本日" : `最後の証拠から ${days} 日`;
}

function MovedQuestion({ q }: { q: PirBriefQuestion }) {
  return (
    <div className="border border-border-subtle rounded-md p-3 space-y-1.5 min-w-0">
      <div className="flex items-start gap-2 flex-wrap">
        <span className="text-sm font-semibold text-fg flex-1 min-w-0">{q.question}</span>
        <ConfidenceChip confidence={q.confidence} />
      </div>
      <p className="text-sm text-fg leading-relaxed m-0">{q.claim}</p>
      <ul className="m-0 p-0 list-none space-y-0.5">
        {q.moves.map((m, i) => (
          <li key={`${i}-${m.at}`} className="text-[13px] text-fg-muted">
            <span className="text-warning font-semibold">{m.label}</span>
            {m.reason && <span> — {m.reason}</span>}
          </li>
        ))}
      </ul>
      {q.fired_indicators.length > 0 && (
        <p className="text-[13px] text-fg-muted m-0">
          観測された指標: {q.fired_indicators.join(" / ")}
        </p>
      )}
      {q.missing_evidence.length > 0 && (
        <p className="text-[13px] text-fg-subtle m-0">まだ分からないこと: {q.missing_evidence[0]}</p>
      )}
    </div>
  );
}

function StillQuestion({ q }: { q: PirBriefQuestion }) {
  const stale = q.days_since_evidence !== null && q.days_since_evidence >= STALE_DAYS;
  return (
    <li className="text-[13px] leading-relaxed min-w-0">
      <span className="text-fg">{q.question}</span>
      <span className="text-fg-muted"> — {q.claim}</span>
      <span className="text-fg-subtle whitespace-nowrap">
        {" "}
        ({vocabLabel("confidence", q.confidence)}・
        <span className={stale ? "text-warning" : ""}>{freshness(q.days_since_evidence)}</span>
        ・待っている指標 {q.open_indicators})
      </span>
    </li>
  );
}

export function PirBriefSection({ brief }: { brief: PirBriefPayload }) {
  return (
    <section className="space-y-2">
      <h4 className={`${SUBHEAD} m-0`}>PIR ブリーフ — 常設の問いへの答え</h4>
      <p className="text-base font-semibold text-fg m-0">{brief.headline}</p>
      {brief.moved.length > 0 && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-2">
          {brief.moved.map((q) => (
            <MovedQuestion key={q.situation_id} q={q} />
          ))}
        </div>
      )}
      {brief.still.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs font-semibold text-fg-muted">動いていない問い</div>
          <ul className="m-0 pl-4 space-y-1">
            {brief.still.map((q) => (
              <StillQuestion key={q.situation_id} q={q} />
            ))}
          </ul>
        </div>
      )}
      <a href={QUESTIONS_PATH} className="text-[13px] text-accent hover:underline">
        問いごとの根拠・指標・推移 →
      </a>
    </section>
  );
}
