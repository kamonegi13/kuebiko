// 期間 (time) を左右フリックで 1 つずつ動かす。
//
// 期間の切替を持つ画面 (国家情勢 / 脅威アクター) で同じ動きにする。
// 現況の期間 (daily/weekly/monthly) は別の語彙なので、そちらは SynthesisTab が持つ。
import { TIMES } from "../components/Shell";
import { useFilters } from "../state/filters";
import { useHorizontalSwipe } from "./useHorizontalSwipe";

export function usePeriodSwipe<T extends HTMLElement>() {
  const time = useFilters((s) => s.time);
  const setFilter = useFilters((s) => s.setFilter);
  const step = (d: 1 | -1) => {
    const i = TIMES.findIndex((t) => t.v === time);
    // 端では止まる (巡回させない — 端まで来たことが分からなくなる)。
    const next = TIMES[Math.min(TIMES.length - 1, Math.max(0, i + d))];
    if (next && next.v !== time) setFilter("time", next.v);
  };
  return useHorizontalSwipe<T>({ onNext: () => step(1), onPrev: () => step(-1) });
}
