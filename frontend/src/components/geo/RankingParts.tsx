// 被害国ランキングの行部品 (MapPage の右パネルと dashboard widget で共用)。
// MapPage.tsx から verbatim 移動 (2026-07-11、ダッシュボードへのランキング widget 追加に伴い抽出)。

import type { GeoConfidence } from "../../api/geo";
import { sectorColor } from "./sectorColors";

// カバレッジ信頼度の表示メタ (色 + ラベル)。色は枠線/ドットのみ、件数色とは独立。
export const CONFIDENCE_META: Record<GeoConfidence, { dot: string; label: string }> = {
  thin: { dot: "#e0894a", label: "薄い (単一/少数ソース)" },
  medium: { dot: "#8a93a6", label: "並" },
  rich: { dot: "#4caf82", label: "厚い (多源)" },
};

// API が想定外の値を返した場合の安全な既定値 (型上は GeoConfidence で保証されているが、
// 実データはランタイムで検証されないため未知値での例外を防ぐ)。
const CONFIDENCE_META_FALLBACK = { dot: "#8a93a6", label: "不明" };

// 被害国ランキングの先頭に出すセクター構成ミニバー。複数セクターを比率で積む
// (top_sector 1 色だと複数混在を誤表現するため)。breakdown 外 (sector 不明) は中立色で残量表示。
export function SectorMiniBar({
  sectors,
  total,
}: {
  sectors: { sector: string; label: string; count: number }[];
  total: number;
}) {
  const denom = Math.max(1, total);
  const top = sectors.slice(0, 4);
  const rest = Math.max(0, total - top.reduce((s, x) => s + x.count, 0));
  const tip = [...sectors.map((s) => `${s.label}: ${s.count}`), rest > 0 ? `その他: ${rest}` : ""]
    .filter(Boolean)
    .join("\n");
  return (
    <span
      className="inline-flex h-2.5 w-9 shrink-0 overflow-hidden rounded-sm bg-surface-2"
      title={tip}
    >
      {top.map((s) => (
        <span
          key={s.sector}
          style={{ width: `${(s.count / denom) * 100}%`, background: sectorColor(s.sector) }}
        />
      ))}
      {rest > 0 && <span style={{ width: `${(rest / denom) * 100}%`, background: "#3a4150" }} />}
    </span>
  );
}

// カバレッジ信頼度ドット (corpus 由来)。色は信頼度のみを表し、件数色とは独立。
// hover で「Nソース / 最大X%（主に <source>）」を出し、暗域/単一依存を正直に伝える。
export function ConfidenceDot({
  confidence,
  sourceCount,
  share,
  topSource,
}: {
  confidence: GeoConfidence;
  sourceCount: number;
  share: number;
  topSource: string | null;
}) {
  const meta = CONFIDENCE_META[confidence] ?? CONFIDENCE_META_FALLBACK;
  const tip = `カバレッジ信頼度: ${meta.label}\n${sourceCount}ソース / 最大${Math.round(
    share * 100,
  )}%${topSource ? `（主に ${topSource}）` : ""}`;
  return (
    <span
      className="inline-block h-2 w-2 shrink-0 rounded-full"
      style={{ background: meta.dot }}
      title={tip}
    />
  );
}

// 「台帳のみ」タグ: 報道(選別済) が無く生レジストリだけで観測された国 (報道経路の盲点)。
export function LedgerTag() {
  return (
    <span
      className="shrink-0 rounded-sm border border-border-subtle px-1 text-[10px] text-fg-subtle"
      title="報道(選別済)が無く、台帳(ransomware.live 等の未加工リスト)のみで観測された国"
    >
      台帳のみ
    </span>
  );
}
