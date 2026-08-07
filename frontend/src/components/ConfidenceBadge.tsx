import type { SourceBasis } from "../api/spotlight";
import { vocabLabel } from "../hooks/useVocab";

// S1: 出典基盤の確度バッジ。source メタから決定的に算出した信頼度を一目で示す。
// reason は hover tooltip。briefing 通読で「各主張の足場」を判断するために使う。
export function ConfidenceBadge({ sb }: { sb: SourceBasis | undefined | null }) {
  if (!sb) return null;
  // src=0: article_ids が記事に未照合 = 出典を検証できない。確度を主張せず中立表示にする
  // (「検証できないのに確度中」という false confidence を避ける)。
  if (sb.source_count === 0) {
    return (
      <span
        title={sb.reason || "引用元の記事が見つからないため出典を検証できません"}
        className="text-[10px] px-1.5 py-0.5 rounded border border-border-default text-fg-subtle shrink-0 whitespace-nowrap"
      >
        出典未照合
      </span>
    );
  }
  const tone =
    sb.confidence === "high"
      ? "bg-accent-subtle text-accent border-accent/30"
      : sb.confidence === "low"
      ? "bg-critical-soft text-critical border-critical/30"
      : sb.confidence === "medium"
      ? "bg-warning-soft text-warning border-warning/30"
      : "bg-surface-2 text-fg-subtle border-border-default";
  // ラベルは backend 配信 vocab "confidence" (vocabLabel) を SSoT に。未知値は原値へ fallback。
  const label = vocabLabel("confidence", sb.confidence);
  const suffix = sb.has_official_authority ? " 公式" : sb.social_only ? " 要裏取り" : "";
  return (
    <span
      title={sb.reason}
      className={`text-[10px] px-1.5 py-0.5 rounded border shrink-0 whitespace-nowrap ${tone}`}
    >
      {label}
      {suffix}
    </span>
  );
}
