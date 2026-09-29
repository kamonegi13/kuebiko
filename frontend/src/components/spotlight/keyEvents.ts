// 段C (2026-09-29): Spotlight は毎日、直近 7 日を作り直す (窓 7 日・頻度 1 日)。
// 窓と頻度がずれるので、毎日読む人には「昨日も見た事象」が大半になる。生成物の形は
// 変えず (学習・評価の系列を壊さない)、表示で窓を頻度に合わせる — 新しい順に並べ、
// 直近 24 時間の事象を先に分けて出す。
import type { KeyEvent } from "../../api/spotlight";

export const RECENT_HOURS = 24;

function ts(e: KeyEvent): number {
  const t = Date.parse(e.published_at);
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

/** 新しい順に並べ、直近 ``hours`` 時間とそれ以前に分ける (日付不明はそれ以前)。 */
export function splitByRecency(
  events: KeyEvent[],
  nowMs: number,
  hours: number = RECENT_HOURS,
): { recent: KeyEvent[]; earlier: KeyEvent[] } {
  const since = nowMs - hours * 3_600_000;
  const sorted = [...events].sort((a, b) => ts(b) - ts(a));
  return {
    recent: sorted.filter((e) => ts(e) >= since),
    earlier: sorted.filter((e) => ts(e) < since),
  };
}
