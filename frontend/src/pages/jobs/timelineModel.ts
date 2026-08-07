// 統合 24h timeline の純ロジック/型 (描画に依存しないもの)。
// JobTimeline.tsx から切り出し (ファイル肥大の回避 + テスト容易性)。

import type { JobView } from "../../api/jobs";
import {
  DOW_LABEL, firesOnDow, hhmmToMinutes, MINUTES_PER_DAY, parseDowField, type DowKey,
} from "./timeUtils";

// パッキングで使う marker の近似幅 (分)。pill は概ね ~64px、24h を ~960px と仮定 → ~96 分。
// 実幅は track 幅で変わるが、決定論のため固定近似で衝突判定する (視覚上の余白込みで安全側)。
export const MARKER_WIDTH_MIN = 96;

// ---- marker モデル ------------------------------------------------------
export interface PointMarker {
  job: JobView;
  minutes: number; // 軸上の位置 (分、drag/pending 反映後)
  active: boolean; // 選択曜日に発火するか (weekly の非該当日は false)
  isWeekly: boolean;
  isMonthly: boolean;
  draggable: boolean;
}

export interface IntervalMarker {
  job: JobView;
  intervalMinutes: number;
  offsetMinutes: number;
}

export interface ReactiveMarker {
  job: JobView;
}

export interface PendingDrag {
  jobId: string;
  minutes: number; // snap 済みの着地時刻 (分)
}

// 重処理 Gantt バー: [開始 = ジョブ時刻, 幅 = max_runtime_minutes]。
// active バーは「収集停止ゾーン」でもある (収集発火がこの run 区間に重なると抑止される)。
// 非 active (選択曜日に稼働しない weekly) は informational な ghost バー (収集は止めない)。
export interface HeavyBar {
  jobId: string;
  label: string;
  start: number; // 開始分 (= ピル枠左端と同じ x)
  runtime: number; // 幅 (分) = max_runtime_minutes
  active: boolean; // 選択曜日に稼働するか (ピルの可視性ルールと一致させる)
  dowLabels: string; // weekly の稼働曜日 (例 "月火")、daily/monthly は空
}

// 分区間 [from, to)。
export interface Segment { from: number; to: number }

// heavy cron ジョブが選択曜日に発火するか (帯の曜日フィルタ)。weekly は該当曜日のみ、
// monthly/daily は曜日概念がないので常時 true。
export function activeOnSelectedDay(job: JobView, selected: DowKey): boolean {
  const dowSet = parseDowField(job.day_of_week);
  if (dowSet.size > 0) return dowSet.has(selected);
  return true;
}

// ---- ジョブ振り分け ------------------------------------------------------
export interface SplitMarkers {
  points: PointMarker[];
  intervals: IntervalMarker[];
  // 定常キュー処理 (JobDef.upkeep=true の interval)。専用 swimlane を持たず
  // 「定常処理」集約 1 行に畳む (補助ジョブ増加でタイムラインが点列に支配される問題の対処)。
  upkeepIntervals: IntervalMarker[];
  reactives: ReactiveMarker[];
}

export function splitMarkers(
  jobs: JobView[],
  selectedDow: DowKey,
  draggableBase: boolean, // !readOnly && schedulerAvailable
): SplitMarkers {
  const points: PointMarker[] = [];
  const intervals: IntervalMarker[] = [];
  const upkeepIntervals: IntervalMarker[] = [];
  const reactives: ReactiveMarker[] = [];
  for (const job of jobs) {
    if (job.schedule_type === "reactive") {
      reactives.push({ job });
    } else if (job.schedule_type === "interval") {
      const interval = job.interval_minutes ?? 60;
      const offset = ((job.offset_minutes ?? 0) % Math.max(1, interval) + interval) % interval;
      const marker = { job, intervalMinutes: interval, offsetMinutes: offset };
      (job.upkeep ? upkeepIntervals : intervals).push(marker);
    } else {
      const dowSet = parseDowField(job.day_of_week);
      const isWeekly = dowSet.size > 0;
      const isMonthly = !!job.day && job.day.trim() !== "";
      const active = isWeekly ? firesOnDow(job.day_of_week, selectedDow) : true;
      // draggable は曜日と無関係 (時刻のみ変更、day/day_of_week は保持)。
      points.push({
        job, minutes: hhmmToMinutes(job.hour, job.minute),
        active, isWeekly, isMonthly, draggable: draggableBase,
      });
    }
  }
  return { points, intervals, upkeepIntervals, reactives };
}

// ---- 貪欲 interval packing (dodge 行割当) --------------------------------
// x (分) 昇順に走査し、各 marker を「直前 marker の右端 < この marker の左端」な
// 最初の行に置く。無ければ新行。決定論的で ~3-5 行に収束する。
// marker は leading (枠左端 = 時刻) アンカーなので占有区間は [minutes, minutes + width]。
export function packPoints(markers: PointMarker[]): PointMarker[][] {
  const sorted = [...markers].sort((a, b) => a.minutes - b.minutes || a.job.id.localeCompare(b.job.id));
  const rows: PointMarker[][] = [];
  const rowRightEdge: number[] = []; // 各行の最終 marker の右端 (分)
  for (const m of sorted) {
    const left = m.minutes;
    const right = m.minutes + MARKER_WIDTH_MIN;
    let placed = false;
    for (let r = 0; r < rows.length; r++) {
      if (rowRightEdge[r] < left) {
        rows[r].push(m);
        rowRightEdge[r] = right;
        placed = true;
        break;
      }
    }
    if (!placed) {
      rows.push([m]);
      rowRightEdge.push(right);
    }
  }
  return rows;
}

// ---- 重処理 Gantt バー導出 ----------------------------------------------
// heavy かつ cron かつ hour!=null な全ジョブ (任意曜日) について、
// [開始 = live 表示分, 幅 = max_runtime_minutes] のバーを作る。ピルと同じ可視性ルールに合わせ、
// 非該当曜日でも ghost バーを出す (active フラグで styling / 抑止対象を分岐)。
export function deriveHeavyBars(
  jobs: JobView[],
  selectedDow: DowKey,
  displayMinutesOf: (job: JobView, base: number) => number,
): HeavyBar[] {
  const out: HeavyBar[] = [];
  for (const job of jobs) {
    if (!job.heavy) continue;
    if (job.schedule_type !== "cron") continue;
    if (job.hour == null) continue;
    const start = displayMinutesOf(job, hhmmToMinutes(job.hour, job.minute));
    const runtime = Math.max(1, job.max_runtime_minutes || 5);
    const dowSet = parseDowField(job.day_of_week);
    const dowLabels = [...dowSet].map((d) => DOW_LABEL[d]).join("");
    out.push({
      jobId: job.id,
      label: job.title,
      start,
      runtime,
      active: activeOnSelectedDay(job, selectedDow),
      dowLabels,
    });
  }
  return out;
}

// 収集発火 [start, start+runtime] が active な heavy バー [b.start, b.start+b.runtime] と
// 区間 overlap するか (backend の抑止判定と同じ)。overlap する heavy バーの label を返す。
export function overlappingHeavyBar(
  bars: HeavyBar[],
  start: number,
  runtime: number,
  selfJobId?: string,
): string | null {
  const end = start + runtime;
  for (const b of bars) {
    if (selfJobId && b.jobId === selfJobId) continue;
    const bEnd = b.start + b.runtime;
    if (start < bEnd && b.start < end) return b.label; // 区間が重なる
  }
  return null;
}

// heavy バー群を [from, to) の Segment 配列へ (interval レーンの帯の切り欠き計算用)。
export function heavyBarSegments(bars: HeavyBar[]): Segment[] {
  return bars.map((b) => ({
    from: Math.max(0, b.start),
    to: Math.min(MINUTES_PER_DAY, b.start + b.runtime),
  }));
}
