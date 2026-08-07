// 日本重要インフラ board 用の小さなバッジ群 (精度/状態/システム軸/近接度段階/事業者層/行動)。
import type { IntentId, OperatorTier, Precision, StageId, SystemDomain } from "../../api/jpci";
import { intentLabel, nationMeta } from "../../utils/diamond";
import { vocabLabel } from "../../hooks/useVocab";

// 国家アクター行動の intent (事前配置を最も強く)。ラベルは SSoT (utils/diamond.ts の
// intentLabel = backend INTENT_LABELS_JA_SHORT と対) を参照 (M3: 「事前潜伏」方言排除)。
// 色は board 独自のリスク階調 (diamond.ts の凡例色とは意図的に別)。
// 表現原則 (2026-07-14): 世界行動は日本への含意が示唆どまりのため **輪郭のみ (塗りなし)**
// = 「塗り=確定 / 輪郭=示唆」。intent の同一性は色相で保つが警報級の視覚重量は与えない。
const INTENT_COLOR: Record<IntentId, string> = {
  prepositioning: "#f87171",
  disruption: "#fb923c",
  espionage: "#c084fc",
  subversion: "#facc15",
  coercion: "#94a3b8",
};

export function IntentBadge({ intent }: { intent: IntentId }) {
  const color = INTENT_COLOR[intent];
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded font-medium whitespace-nowrap"
      style={{ color, border: `1px solid ${color}40` }}
    >
      {intentLabel(intent) || intent}
    </span>
  );
}

// 敵性国家の旗色 (中朝露イラン)。ラベルは SSoT (utils/diamond.ts の nationMeta) を参照し、
// 複製辞書によるドリフトを防ぐ (DiamondDiagram.tsx と同じ出典)。
export function NationTag({ nation }: { nation: string }) {
  const label = nationMeta(nation)?.label ?? nation.toUpperCase();
  return <span className="text-[10px] text-fg-muted font-semibold">{label}</span>;
}

/** 事業者判定層: 指定事業者 (名簿) / 分野該当 (種別サフィックス型判定)。 */
export function OperatorTierBadge({ tier }: { tier: OperatorTier | undefined }) {
  if (!tier) return null;
  const isDesignated = tier === "designated";
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${
        isDesignated
          ? "bg-accent/15 text-accent border border-accent/40"
          : "border border-border-subtle text-fg-muted"
      }`}
    >
      {isDesignated ? "指定事業者" : "分野該当"}
    </span>
  );
}

/** 集中ノード (清算・決済・取引の単一障害点): 侵害が分野横断にカスケードする最大級の信号。 */
export function SystemicBadge() {
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded font-bold bg-critical/20 text-critical border border-critical/50"
      title="決済清算・取引所・DNS 等の集中点。侵害の影響が分野横断に波及する"
    >
      基幹ノード
    </span>
  );
}

// 近接度ラダーの色 (左=遠い→右=近い)。色のみ保持し、ラベルは backend vocab
// "jpci_stage" (vocabLabel) を SSoT に解決する (静的 value→label マップを持たない)。
export const STAGE_STYLE: Record<StageId, string> = {
  quiet: "#6b7280",
  indication: "#60a5fa",
  targeting: "#fbbf24",
  intrusion_it: "#fb923c",
  ot_approach: "#f87171",
};

export function StageBadge({ stage }: { stage: StageId }) {
  const color = STAGE_STYLE[stage];
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded border font-semibold whitespace-nowrap"
      style={{
        color,
        backgroundColor: `${color}1a`,
        borderColor: `${color}55`,
      }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />
      {vocabLabel("jpci_stage", stage)}
    </span>
  );
}

export function DeltaBadge({ delta }: { delta: -1 | 0 | 1 | null }) {
  if (delta === null) return <span className="text-[11px] text-fg-subtle">—</span>;
  if (delta > 0) return <span className="text-[11px] font-bold text-critical">▲ 上昇</span>;
  if (delta < 0) return <span className="text-[11px] font-bold text-accent">▼ 後退</span>;
  return <span className="text-[11px] text-fg-subtle">→</span>;
}

// 色のみ保持。ラベルは backend vocab "jpci_domain" (vocabLabel) を SSoT に解決。
const DOMAIN_COLOR: Record<SystemDomain, string> = {
  ot: "#f87171",
  boundary: "#fb923c",
  it: "#60a5fa",
  unknown: "#6b7280",
};

export function SystemDomainBadge({ domain }: { domain: SystemDomain }) {
  const color = DOMAIN_COLOR[domain];
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded"
      style={{ color, backgroundColor: `${color}1f` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />
      {vocabLabel("jpci_domain", domain)}
    </span>
  );
}

export function SystemDomainMix({
  mix,
}: {
  mix: { ot: number; boundary: number; it: number; unknown: number };
}) {
  const order: SystemDomain[] = ["ot", "boundary", "it", "unknown"];
  const total = order.reduce((a, k) => a + mix[k], 0);
  if (total === 0) return null;
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <div className="flex h-1.5 w-32 rounded overflow-hidden bg-surface-2">
        {order.map((k) =>
          mix[k] > 0 ? (
            <div
              key={k}
              style={{ width: `${(mix[k] / total) * 100}%`, backgroundColor: DOMAIN_COLOR[k] }}
            />
          ) : null,
        )}
      </div>
      {order
        .filter((k) => mix[k] > 0)
        .map((k) => (
          <span key={k} className="text-[10px] text-fg-subtle">
            {vocabLabel("jpci_domain", k)} {mix[k]}
          </span>
        ))}
    </div>
  );
}

export function PrecisionBadge({ level }: { level: Precision }) {
  // ラベルは backend vocab "jpci_precision" (vocabLabel) を SSoT に解決。
  return (
    <span className="text-[10px] px-1.5 py-0.5 rounded border border-border-subtle text-fg-subtle">
      {vocabLabel("jpci_precision", level)}
    </span>
  );
}

export function StatusBadge({ status }: { status: "actual" | "potential" }) {
  const isActual = status === "actual";
  const cls = isActual
    ? "bg-critical/10 text-critical"
    : "bg-warning-soft text-warning";
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${cls}`}>
      {isActual ? "実被害" : "潜在"}
    </span>
  );
}

export function ConfidencePill({ confidence }: { confidence: "high" | "medium" | "low" }) {
  // ラベルは backend 配信 vocab "confidence" (vocabLabel) を SSoT に。未知値は原値へ fallback。
  const label = vocabLabel("confidence", confidence);
  const cls =
    confidence === "high"
      ? "text-accent"
      : confidence === "low"
        ? "text-critical"
        : confidence === "medium"
          ? "text-warning"
          : "text-fg-subtle";
  return <span className={`text-[10px] ${cls}`}>{label}</span>;
}
