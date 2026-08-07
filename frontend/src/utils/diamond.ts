// Diamond Model の表示 utility (Phase Diamond-Axes)。
// socio-political 軸 (intent) と nation のラベル・色・選択肢を一元管理する。
// backend canonical は src/cti/diamond_model.py の SOCIO_POLITICAL_INTENTS / INTENT_LABELS_JA。
//
// 配色根拠 (実在ベンダー慣行):
//   - nation: CrowdStrike (動物) / Microsoft (気象) の国別分類 + 国旗色 + SOC「主敵=赤」慣行
//     中=赤(PANDA/Typhoon) 露=青(BEAR/Blizzard) 北=紫(CHOLLIMA/Sleet) イラン=緑(KITTEN/Sandstorm)
//   - intent: SOC severity 慣行 (赤=破壊/事前配置, 琥珀=金銭[SPIDER/Tempest], 青=諜報, 紫=影響工作)
//   - 地政学動機 (Phase Geopolitical-Intent): 威圧=ローズ / 抑止=スカイ(防御) /
//     領土=オーカー(土地) / 体制転覆=バイオレット(grayzone, 影響工作隣接) / 外交=エメラルド(協調)
import { vocabLabel } from "../hooks/useVocab";

export interface IntentMeta {
  label: string; // 日本語ラベル (vocab "intent" を SSoT に解決)
  tone: string; // 既存チップ用 Tailwind token class
  hex: string; // 図 (SVG / border) 用 raw color
}

// canonical intent → 表示スタイル (tone + hex)。unknown は表示対象外 (集計・filter から除外)。
// label は backend 配信 vocab "intent" (vocabLabel) を SSoT に解決するためここには持たない。
// 統一 strategic-intent 軸: サイバー動機 + 地政学/国家動機 ([[diamond_model.py]] と一致)。
export const INTENT_STYLE: Record<string, { tone: string; hex: string }> = {
  espionage: { tone: "text-accent", hex: "#818cf8" },
  financial: { tone: "text-warning", hex: "#f5a623" },
  prepositioning: { tone: "text-critical", hex: "#ff6b6b" },
  disruption: { tone: "text-critical", hex: "#f04444" },
  influence: { tone: "text-accent", hex: "#c084fc" },
  hacktivism: { tone: "text-fg-muted", hex: "#94a3b8" },
  coercion: { tone: "text-critical", hex: "#fb7185" },
  deterrence: { tone: "text-accent", hex: "#38bdf8" },
  territorial: { tone: "text-warning", hex: "#ca8a04" },
  subversion: { tone: "text-accent", hex: "#a78bfa" },
  diplomacy: { tone: "text-fg-muted", hex: "#34d399" },
};

export function intentLabel(intent: string | null | undefined): string {
  return vocabLabel("intent", intent);
}

// tone + hex (INTENT_STYLE) と label (vocab) を束ねた表示メタ。未知 intent は null。
export function intentMeta(intent: string | null | undefined): IntentMeta | null {
  if (!intent) return null;
  const style = INTENT_STYLE[intent];
  if (!style) return null;
  return { label: intentLabel(intent), tone: style.tone, hex: style.hex };
}

export function intentTone(intent: string | null | undefined): string {
  if (!intent) return "text-fg-muted";
  return INTENT_STYLE[intent]?.tone ?? "text-fg-muted";
}

export function intentHex(intent: string | null | undefined): string {
  if (!intent) return "#6b7280";
  return INTENT_STYLE[intent]?.hex ?? "#6b7280";
}

// socio-political 軸の intent を持つか (unknown / 未知は false)。
export function hasIntent(intent: string | null | undefined): boolean {
  return !!intent && intent in INTENT_STYLE;
}

// intent の label/options は backend 配信 vocab (intent) へ移行済み
// (intentLabel / useVocabOptions("intent"))。静的マップを再生しないこと。

// ---------- intent confidence (確度、有機的結合監査 H3 配線) ----------
// null/undefined = 旧レジーム確定 tag (確度注記なし)。low = 仮説として淡色・注記表示する。
// 確度ラベル (高/中/低) は backend 配信 vocab "confidence" (vocabLabel) を SSoT に解決する。
// 以前ここにあった INTENT_CONFIDENCE_LABEL / intentConfidenceLabel は撤去済 (消費側で vocabLabel)。

// low = 弱シグナルからの仮説 (確定と同格に扱わない)。
export function isHypothesisIntent(conf: string | null | undefined): boolean {
  return conf === "low";
}

// ---------- nation (Adversary 頂点の配色) ----------

export interface NationMeta {
  label: string; // 日本語名 (country vocab を SSoT に解決 — 全 nation を被覆)
  hex: string; // border / accent 色
  vendor: string; // CrowdStrike 動物 / Microsoft 気象 (tooltip 用)
}

// 敵性主要 4 か国の視覚アイデンティティ (色 + ベンダー分類) のみ保持。
// label は country vocab から解決するためここには持たない (4 か国以外の nation も名前解決するため)。
// 配色根拠: 中=赤(PANDA/Typhoon) 露=青(BEAR/Blizzard) 北=紫(CHOLLIMA/Sleet) イラン=緑(KITTEN/Sandstorm)。
const NATION_STYLE: Record<string, { hex: string; vendor: string }> = {
  cn: { hex: "#e5484d", vendor: "PANDA / Typhoon" },
  ru: { hex: "#6b88ff", vendor: "BEAR / Blizzard" },
  kp: { hex: "#a371f7", vendor: "CHOLLIMA / Sleet" },
  ir: { hex: "#45b878", vendor: "KITTEN / Sandstorm" },
};

// style マップにない nation の既定色 (neutral gray、consumers が許容する fallback)。
const NATION_DEFAULT_HEX = "#6b7280";

// nation コード → 表示メタ。label は country vocab を SSoT に解決するため、
// 主要 4 か国以外の nation コードでも名前が解決する (旧: 4 か国のみ非 null だった gap の修正)。
export function nationMeta(nation: string | null | undefined): NationMeta | null {
  if (!nation) return null;
  const style = NATION_STYLE[nation.toLowerCase()];
  return {
    label: vocabLabel("country", nation.toUpperCase()) || nation,
    hex: style?.hex ?? NATION_DEFAULT_HEX,
    vendor: style?.vendor ?? "",
  };
}
