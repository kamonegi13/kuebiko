// フリック判定の共通部品。**閉じる用と切替用で 2 つ持たない** —
// 横スクロール領域から操作を奪わない条件や方向確定の閾値がずれると、
// 片方だけ誤爆する形で表に出る。

/** これ未満の移動では方向をまだ確定しない (タップを奪わないため)。 */
export const DIRECTION_LOCK_PX = 6;
/** |dx| > RATIO * |dy| を「水平優勢」とみなす。 */
export const DIRECTION_LOCK_RATIO = 2;

export function isHorizontallyScrollable(el: Element): boolean {
  if (el.scrollWidth <= el.clientWidth) return false;
  const overflowX = window.getComputedStyle(el).overflowX;
  return overflowX === "auto" || overflowX === "scroll";
}

/** タッチ開始点から boundary まで祖先を辿り、「横スクロール可能かつ **その向きへ
 *  スクロールする余地がある**」要素があるか調べる。
 *
 *  ⚠ 表の overflow-x-auto (CLAUDE.md の横スクロール規約) から操作を奪わない。
 *  余地が無い端まで来ていれば、この要素の上でも通常どおり判定してよい。 */
export function startsInsideScrollRoom(
  target: Element | null,
  boundary: Element,
  direction: "left" | "right" | "both" = "both",
): boolean {
  let node: Element | null = target;
  while (node && node !== boundary) {
    if (isHorizontallyScrollable(node)) {
      const room =
        direction === "right"
          ? node.scrollLeft > 0
          : direction === "left"
            ? node.scrollLeft < node.scrollWidth - node.clientWidth - 1
            : node.scrollLeft > 0 ||
              node.scrollLeft < node.scrollWidth - node.clientWidth - 1;
      if (room) return true;
    }
    node = node.parentElement;
  }
  return false;
}
