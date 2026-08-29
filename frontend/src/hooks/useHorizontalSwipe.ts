// 左右フリックで「前へ / 次へ」を切り替える hook (期間の切替など)。
//
// useSwipeToClose と違い **指に追従させない** — 切り替える対象が入れ替わるだけで、
// 引きずる面が無い。判定の作法 (方向確定を touchmove まで保留 / 横スクロール領域から
// 操作を奪わない / 最後の指が離れたときだけ確定 / touchcancel は副作用なし中断) は
// swipeGeometry に揃える。
import { useEffect, useRef, type RefObject } from "react";

import {
  DIRECTION_LOCK_PX,
  DIRECTION_LOCK_RATIO,
  startsInsideScrollRoom,
} from "./swipeGeometry";

/** 切り替えを確定する移動量。誤爆を避けるため、閉じる操作より大きめに取る。 */
const COMMIT_PX = 60;

export interface UseHorizontalSwipeOptions {
  /** 左へ払った = 次へ */
  onNext: () => void;
  /** 右へ払った = 前へ */
  onPrev: () => void;
  disabled?: boolean;
}

type Phase = "idle" | "pending" | "tracking" | "rejected";

export function useHorizontalSwipe<T extends HTMLElement>(
  options: UseHorizontalSwipeOptions,
): RefObject<T> {
  const ref = useRef<T>(null);
  // ⚠ 呼び出し側の関数は毎レンダー変わる。ref 経由で読まないと、
  //   effect が張り直されて操作の途中で listener が消える。
  const onNextRef = useRef(options.onNext);
  const onPrevRef = useRef(options.onPrev);
  onNextRef.current = options.onNext;
  onPrevRef.current = options.onPrev;
  const disabled = options.disabled ?? false;

  useEffect(() => {
    const el = ref.current;
    if (!el || disabled) return;

    let phase: Phase = "idle";
    let x0 = 0;
    let y0 = 0;

    const reset = () => {
      phase = "idle";
    };

    const onStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) {
        phase = "rejected";
        return;
      }
      // 横スクロールできる領域の上で始まったなら、そちらに譲る (どちら向きでも)。
      if (startsInsideScrollRoom(e.target as Element | null, el)) {
        phase = "rejected";
        return;
      }
      phase = "pending";
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
    };

    const onMove = (e: TouchEvent) => {
      if (phase === "rejected" || phase === "idle") return;
      if (e.touches.length !== 1) {
        phase = "rejected";
        return;
      }
      const dx = e.touches[0].clientX - x0;
      const dy = e.touches[0].clientY - y0;
      if (phase === "pending") {
        if (Math.abs(dx) < DIRECTION_LOCK_PX && Math.abs(dy) < DIRECTION_LOCK_PX) return;
        // 縦優勢ならスクロールに返す。一度決めたら touchend まで変えない。
        phase = Math.abs(dx) > DIRECTION_LOCK_RATIO * Math.abs(dy) ? "tracking" : "rejected";
      }
    };

    const onEnd = (e: TouchEvent) => {
      // 最後の指が離れたときだけ確定する (多指操作の途中で発火させない)。
      if (e.touches.length !== 0) return;
      if (phase !== "tracking") return reset();
      const dx = (e.changedTouches[0]?.clientX ?? x0) - x0;
      reset();
      if (dx <= -COMMIT_PX) onNextRef.current();
      else if (dx >= COMMIT_PX) onPrevRef.current();
    };

    // touchcancel は **副作用なしで中断**する (システムに割り込まれた操作を
    // 切り替えとして確定させない)。
    const onCancel = () => reset();

    el.addEventListener("touchstart", onStart, { passive: true });
    el.addEventListener("touchmove", onMove, { passive: true });
    el.addEventListener("touchend", onEnd, { passive: true });
    el.addEventListener("touchcancel", onCancel, { passive: true });
    return () => {
      el.removeEventListener("touchstart", onStart);
      el.removeEventListener("touchmove", onMove);
      el.removeEventListener("touchend", onEnd);
      el.removeEventListener("touchcancel", onCancel);
    };
  }, [disabled]);

  return ref as RefObject<T>;
}
