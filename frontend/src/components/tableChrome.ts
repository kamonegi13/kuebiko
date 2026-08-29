// 表の見出し行の共通様式。
//
// ⭐ **行数の多い表では見出しを固定する。** アクター辞書は 262 行 16 画面あり、
// スクロールするとどの列が何か分からなくなっていた (2026-08-29 実測)。
// 同じ className が 6 箇所に直書きされていたので 1 つに寄せる — 散らしたままだと
// sticky を足すのに 6 箇所直すことになり、必ず取り残す。
//
// ⚠ sticky が効くのは **ページ全体がスクロールする** 表だけ。表を内側で
// スクロールさせている場合 (overflow-y-auto の中) は、その要素が天井になる。

/** 表の見出し行 (固定しない)。短い表・カード内の表向け。 */
export const THEAD = "bg-surface-2 text-fg-muted text-[12.5px] uppercase tracking-wider";

/** 表の見出し行 (**表の中で**固定)。TABLE_SCROLL と対で使う。
 *
 *  ⚠ ページ上端への固定は **効かない**。表は横スクロール規約で
 *  `overflow-x-auto` に包まれており、CSS は片方の軸を auto にすると
 *  もう片方も auto にするため、その div が縦の scrollport になる。
 *  中身が縦にあふれない div は一度もスクロールしないので、sticky は
 *  貼り付く先を失って一緒に流れていく (2026-08-29 実測: 2000px スクロールで
 *  y=-1710px)。**表の高さを区切って、その中で固定する**のが確実。
 */
export const THEAD_STICKY = `${THEAD} md:sticky md:top-0 md:z-10`;

/** 行数の多い表の入れ物。**PC だけ** 高さを区切り、中で縦横にスクロールさせる。
 *
 *  ⚠ モバイルでは高さを区切らない。ページも表も縦にスクロールする入れ子になり、
 *  指が中の箱に取られて外側が動かせなくなる。狭い画面では列の大半が
 *  `hidden sm:table-cell` で隠れており、列の取り違えも起きにくい。
 *  結果として THEAD_STICKY が効くのも PC だけ (モバイルは貼り付く先が無い)。 */
export const TABLE_SCROLL =
  "overflow-x-auto md:overflow-auto md:max-h-[70vh] md:rounded-lg md:border md:border-border-subtle";
