// セクター canonical id → 色 (脅威マップのバブル/凡例)。dark bg で識別しやすい categorical。
// victim_sectors.yaml の canonical と対応。未知/不定 (uncategorized/multi_sector/unknown) は
// **中立グレー** = 「セクター不明」を正直に符号化する ([[geo_map_design_discussion]] 精度の誠実性)。
import { vocabLabel } from "../../hooks/useVocab";

// 「セクター不明」を表す中立色 (fg-faint)。
export const SECTOR_UNKNOWN_COLOR = "#4a5260";

// セクター不明として扱う canonical 値 (色は中立グレー、凡例の先頭外)。
export const SECTOR_UNKNOWN_KEYS = new Set([
  "uncategorized",
  "multi_sector",
  "unknown",
  "none",
  "",
]);

const SECTOR_COLORS: Record<string, string> = {
  government: "#6b88ff",
  national_security: "#f87171",
  defense: "#c084fc",
  financial: "#45b878",
  healthcare: "#ff6b6b",
  energy: "#f5a623",
  critical_infra: "#fb923c",
  telecom: "#22d3ee",
  manufacturing: "#d4a373",
  food_agriculture: "#a3e635",
  technology: "#38bdf8",
  education: "#a78bfa",
  media: "#f472b6",
  retail: "#2dd4bf",
  research: "#818cf8",
  space: "#e879f9",
  ngo: "#fbbf24",
  professional_services: "#f0abfc",
  enterprise: "#94a3b8",
  other: "#64748b",
};

export function sectorColor(sector: string | null | undefined): string {
  if (!sector || SECTOR_UNKNOWN_KEYS.has(sector)) return SECTOR_UNKNOWN_COLOR;
  return SECTOR_COLORS[sector] ?? SECTOR_UNKNOWN_COLOR;
}

/**
 * セクター canonical id を日本語表示名にする (未知値は原値をそのまま返す)。
 * ラベルは backend 配信 vocab "sector" (config/victim_sectors.yaml が SSoT) を解決する。
 */
export function sectorLabel(sector: string | null | undefined): string {
  if (!sector) return "";
  return vocabLabel("sector", sector);
}
