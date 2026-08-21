// メイン画面のプルリフレッシュ hook。タッチデバイスのみ発火 (マウス操作は touch イベント
// 自体が発生しないため自然に無効)。返す containerRef を対象コンテナに付ける。
// 最上部 (getScrollTop() === 0) で下方向に引いたときだけ追従し、閾値 (既定 70px) を
// 超えて離すと onRefresh (既定 window.location.reload) を呼ぶ。
//
// この repo は document スクロール設計 (AppShell のコメント参照: sticky を壊さないため
// 個別コンテナを overflow-y-auto にしない) のため、既定の getScrollTop は
// containerRef.current.scrollTop ではなく呼び出し側が window.scrollY 等を渡せるよう
// 差し替え可能にしてある。
//
// pullDistance は React state で持つ (indicator の矢印→スピナー切替やアイコン回転など
// レンダリングに反映する必要があるため、useSwipeToClose と異なり DOM 直操作では済まない)。

import { useEffect, useRef, useState, type RefObject } from "react";

export const PULL_THRESHOLD_PX = 70;
const MAX_VISUAL_PULL_PX = 100; // indicator の伸びをここで頭打ちにする

export interface UsePullToRefreshOptions {
  /** true の間は判定を行わない (peek 表示中など、メイン側の PTR を止めたいとき) */
  disabled?: boolean;
  /** 閾値超で離したときに呼ぶ。既定は window.location.reload() */
  onRefresh?: () => void;
  /** 現在のスクロール位置を返す。既定は containerRef.current.scrollTop。
   * document スクロール設計のページでは `() => window.scrollY` を渡す。 */
  getScrollTop?: () => number;
}

export interface UsePullToRefreshState<T extends HTMLElement> {
  containerRef: RefObject<T>;
  /** 引いている最中の視覚量 (0 = 引いていない、最大 MAX_VISUAL_PULL_PX) */
  pullDistance: number;
  /** 閾値を超えていて、離せば reload される状態か (indicator を矢印→スピナーに切替) */
  readyToRelease: boolean;
}

function defaultRefresh(): void {
  window.location.reload();
}

export function usePullToRefresh<T extends HTMLElement>(
  options: UsePullToRefreshOptions = {},
): UsePullToRefreshState<T> {
  const containerRef = useRef<T>(null);
  const [pullDistance, setPullDistance] = useState(0);
  const onRefreshRef = useRef(options.onRefresh);
  onRefreshRef.current = options.onRefresh;
  const getScrollTopRef = useRef(options.getScrollTop);
  getScrollTopRef.current = options.getScrollTop;
  const disabled = options.disabled ?? false;

  useEffect(() => {
    const el = containerRef.current;
    if (!el || disabled) return;

    const scrollTop = () => (getScrollTopRef.current ? getScrollTopRef.current() : el.scrollTop);

    let tracking = false;
    let startY = 0;
    let dy = 0;

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1 || scrollTop() > 0) {
        tracking = false;
        return;
      }
      startY = e.touches[0].clientY;
      dy = 0;
      tracking = true;
    };

    const onTouchMove = (e: TouchEvent) => {
      if (!tracking) return;
      if (e.touches.length !== 1 || scrollTop() > 0) {
        tracking = false;
        dy = 0;
        setPullDistance(0);
        return;
      }
      const rawDy = e.touches[0].clientY - startY;
      if (rawDy <= 0) {
        dy = 0;
        setPullDistance(0);
        return;
      }
      // ブラウザネイティブのラバーバンド/pull-to-refresh と二重発火しないよう、
      // 自前で処理を握っている間は既定動作を止める (呼び出し側は
      // overscroll-behavior-y: contain も併用すること)
      e.preventDefault();
      dy = rawDy;
      setPullDistance(Math.min(MAX_VISUAL_PULL_PX, rawDy));
    };

    const onTouchEnd = (e: TouchEvent) => {
      if (!tracking) return;
      // 2本目の指がまだ触れている間 (e.touches に残りがある) は確定させない —
      // 最後の指が離れた (touches.length === 0) ときだけ reload を判定する。
      if (e.touches.length !== 0) return;
      tracking = false;
      const shouldRefresh = dy >= PULL_THRESHOLD_PX;
      setPullDistance(0);
      if (shouldRefresh) (onRefreshRef.current ?? defaultRefresh)();
    };

    // touchcancel は着信/OS 割込み等でブラウザがジェスチャを強制中断したときに発火する。
    // touchend と同一 handler にすると、閾値超えの最中に着信が来ただけで意図せず
    // reload してしまう。ここでは状態リセットのみ行い onRefresh は絶対に呼ばない
    // (useSwipeToClose の onTouchCancel と同じ思想)。
    const onTouchCancel = () => {
      tracking = false;
      dy = 0;
      setPullDistance(0);
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
      // mid-gesture の disabled 切替 / unmount でインジケータが引いた位置のまま
      // 固まらないようにする。
      setPullDistance(0);
    };
  }, [disabled]);

  return {
    containerRef,
    pullDistance,
    readyToRelease: pullDistance >= PULL_THRESHOLD_PX,
  };
}
