// 公開サイトのカテゴリ色 (節の見出しバッジ・記事のカテゴリバッジ)。
//
// 既存の categorical パレット (components/geo/sectorColors.ts) と同じ流儀で hex を持つ。
// **意味色 (critical / warning) と衝突させない** — あちらは重大度の符号で、
// カテゴリは分類でしかない。重大度が赤で出る画面に赤いカテゴリを混ぜると誤読を招く。
//
// 値は `PUBLIC_CATEGORIES` (backend) の key。未知の key は中立グレーに落として
// 「色が付かない = 分類外」を正直に示す (勝手に色を割り当てない)。

// ⚠ hex を直接返さず **CSS 変数**を返す。実値は src/index.css がテーマごとに持つ。
// 暗い面で読める明るい色は白い紙の上では 1.4〜2.2:1 にしかならず、
// カテゴリ名を色で描いている以上そのままでは読めない (2026-08-28 実測)。
const CATEGORY_COLORS: Record<string, string> = {
  vuln: "var(--cat-vuln)", // 脆弱性: 青
  incident_breach: "var(--cat-incident_breach)", // 侵害・インシデント: 桃
  threat: "var(--cat-threat)", // マルウェア・APT: 橙
  geopolitical: "var(--cat-geopolitical)", // 地政学: 黄緑
};

/** 分類外を表す中立色。 */
export const CATEGORY_NEUTRAL = "var(--cat-neutral)";

export function categoryColor(key: string): string {
  return CATEGORY_COLORS[key] ?? CATEGORY_NEUTRAL;
}
