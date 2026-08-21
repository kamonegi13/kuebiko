// 右フリックでパネルを閉じる汎用 hook (モバイル記事ピークの誤操作根治の一部)。
// 返す ref を対象要素 (パネル本体) に付けると、touchstart/touchmove/touchend を直接
// addEventListener し、水平優勢と判定した間だけ el.style.transform で指に追従させる。
// (React state 経由の再レンダーは 60fps 追従にオーバーヘッドが乗るため、DOM を直接操作する)
//
// 横スクロール衝突の回避: タッチ開始点の祖先に「横スクロール可能かつ scrollLeft > 0
// (= まだ左へスクロールできる余地がある)」要素があれば、このジェスチャは開始しない
// (表の overflow-x-auto — CLAUDE.md の横スクロール規約 — から操作を奪わない)。
// scrollLeft === 0 (もう左にはスクロールできない) ならこの要素の上で始まったタッチでも
// 通常どおり判定する (奪う余地が無いため)。
//
// 方向確定はタップ許容のため touchmove まで保留し (DIRECTION_LOCK_PX 未満は静観)、
// 一度「水平優勢 かつ 右向き」と確定したら touchend まで固定する (逆方向へ戻っても
// 縦スクロールへは戻さない — 一般的なタッチライブラリの direction-lock と同じ設計)。

import { useEffect, useRef, type RefObject } from "react";

const DIRECTION_LOCK_PX = 6; // これ未満の移動では方向をまだ確定しない
const DIRECTION_LOCK_RATIO = 2; // |dx| > RATIO * |dy| を「水平優勢」とみなす
const CLOSE_RATIO_THRESHOLD = 0.3; // パネル幅に対する移動量の閾値 (30%)
const CLOSE_VELOCITY_PX_MS = 0.5; // 離す瞬間の速度閾値 (px/ms)
const SNAP_BACK_TRANSITION_MS = 200;

export interface UseSwipeToCloseOptions {
  /** 閾値を超えて指を離したときに呼ぶ */
  onClose: () => void;
  /** true の間は判定・追従を一切行わない (パネル非表示時など)。default false */
  disabled?: boolean;
}

type DragPhase = "idle" | "pending" | "swiping" | "rejected";

function isHorizontallyScrollable(el: Element): boolean {
  if (el.scrollWidth <= el.clientWidth) return false;
  const overflowX = window.getComputedStyle(el).overflowX;
  return overflowX === "auto" || overflowX === "scroll";
}

/** タッチ開始点から boundary まで祖先を辿り、「横スクロール可能かつ左へスクロール
 * 余地がある (scrollLeft > 0)」要素があるか調べる。 */
function startsInsideScrollRoom(target: Element | null, boundary: Element): boolean {
  let node: Element | null = target;
  while (node && node !== boundary) {
    if (isHorizontallyScrollable(node) && node.scrollLeft > 0) return true;
    node = node.parentElement;
  }
  return false;
}

export function useSwipeToClose<T extends HTMLElement>(
  options: UseSwipeToCloseOptions,
): RefObject<T> {
  const ref = useRef<T>(null);
  const onCloseRef = useRef(options.onClose);
  onCloseRef.current = options.onClose;
  const disabled = options.disabled ?? false;

  useEffect(() => {
    const el = ref.current;
    if (!el || disabled) return;

    let phase: DragPhase = "idle";
    let startX = 0;
    let startY = 0;
    let panelWidth = 0;
    let lastX = 0;
    let lastTime = 0;
    let velocity = 0; // px/ms、正 = 右向き

    const applyFollow = (dx: number) => {
      el.style.transition = "";
      el.style.transform = `translateX(${dx}px)`;
    };

    const snapBack = () => {
      el.style.transition = `transform ${SNAP_BACK_TRANSITION_MS}ms ease-out`;
      el.style.transform = "translateX(0px)";
    };

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) {
        // swiping 中に2本目の指が触れた場合、素通しで idle に落とすと transform が
        // 追従した位置のまま凍結残留する。視覚的に元へ戻してから中断する。
        if (phase === "swiping") snapBack();
        phase = "idle";
        return;
      }
      const touch = e.touches[0];
      if (startsInsideScrollRoom(e.target as Element | null, el)) {
        phase = "rejected";
        return;
      }
      startX = touch.clientX;
      startY = touch.clientY;
      lastX = startX;
      lastTime = Date.now();
      velocity = 0;
      panelWidth = el.getBoundingClientRect().width || el.offsetWidth;
      phase = "pending";
    };

    const onTouchMove = (e: TouchEvent) => {
      if (phase === "idle" || phase === "rejected") return;
      if (e.touches.length !== 1) return;
      const touch = e.touches[0];
      const dx = touch.clientX - startX;
      const dy = touch.clientY - startY;

      if (phase === "pending") {
        if (Math.abs(dx) < DIRECTION_LOCK_PX && Math.abs(dy) < DIRECTION_LOCK_PX) return;
        const horizontal = Math.abs(dx) > DIRECTION_LOCK_RATIO * Math.abs(dy);
        if (!horizontal || dx <= 0) {
          phase = "rejected";
          return;
        }
        phase = "swiping";
      }

      if (phase !== "swiping") return;
      // ページ/backdrop のスクロールや OS のジェスチャに奪われないよう明示的に止める
      // (このリスナは passive:false で登録する)
      e.preventDefault();

      const now = Date.now();
      const dt = now - lastTime;
      if (dt > 0) velocity = (touch.clientX - lastX) / dt;
      lastX = touch.clientX;
      lastTime = now;

      applyFollow(Math.max(0, dx));
    };

    const onTouchEnd = (e: TouchEvent) => {
      // 2本目の指がまだ触れている間 (e.touches に残りがある) は主指のジェスチャを
      // 確定させない — 最後の指が離れた (touches.length === 0) ときだけ判定する。
      if (e.touches.length !== 0) return;
      if (phase !== "swiping") {
        phase = "idle";
        return;
      }
      const dx = Math.max(0, lastX - startX);
      const ratio = panelWidth > 0 ? dx / panelWidth : 0;
      const shouldClose = ratio > CLOSE_RATIO_THRESHOLD || velocity > CLOSE_VELOCITY_PX_MS;
      phase = "idle";
      if (shouldClose) {
        onCloseRef.current();
        return;
      }
      snapBack();
    };

    const onTouchCancel = () => {
      if (phase === "swiping") snapBack();
      phase = "idle";
    };

    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd, { passive: true });
    el.addEventListener("touchcancel", onTouchCancel, { passive: true });

    return () => {
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("touchcancel", onTouchCancel);
      el.style.transition = "";
      el.style.transform = "";
    };
  }, [disabled]);

  return ref;
}
