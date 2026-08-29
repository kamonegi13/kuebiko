// 見出しの階層。
//
// ⚠ 実測 (2026-08-29): 92 箇所の見出しに **40 種類** の様式があった。
// 見た目が揃っていないだけでなく、同じ大きさで階層の違うものが並んでいる。
// 新しく書くときは必ずこの 3 段のどれかを使う。既存を機械的に置換しない
// — 階層が違うものを同じ様式にすると、かえって読めなくなる。
//
//   1. ページ題   PAGE_TITLE      ページに 1 つだけ
//   2. 節         <SectionHeading> 縦バー + 罫線。話題の切り替わりを示す
//   3. 小見出し   SUBHEAD         節の中の区切り

/** ページ題。ページに 1 つだけ。 */
export const PAGE_TITLE = "m-0 text-xl font-bold text-fg tracking-tight";

/** 小見出し。節の中の区切りで、罫線は引かない。 */
export const SUBHEAD = "m-0 text-[13px] font-semibold text-fg-muted tracking-wide";
