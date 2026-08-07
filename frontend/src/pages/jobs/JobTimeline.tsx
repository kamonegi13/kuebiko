// 統合 24h timeline (ジョブ運用コンソールの hero)。
//
// 「1 本の共有 24h 軸」に全ジョブを marker として載せ、重ならないよう最小行数へ auto-dodge する。
//   - cron point (daily/weekly/monthly) : 枠左端 (先頭) を発火時刻に合わせた pill。drag で時刻変更可。
//   - interval : 横帯 + 実行ノード。専用行。収集は active な重処理と被る発火だけ抜ける (tick gap)。
//   - reactive : 時刻を持たないので軸外の専用ゾーンに「イベント駆動 (時刻非依存)」で明示。
//
// 背景レイヤー (marker の後ろ):
//   - 1 時間グリッド (淡い縦線) + 3h 太めラベル。
//   - 重処理 Gantt バー (= 収集停止ゾーン): heavy cron ジョブの [開始=時刻, 幅=max_runtime_minutes] を
//     jobs から動的導出。枠左端と同じ x から始まり、幅で処理時間差が一目瞭然。重なると濃くなり資源競合を
//     可視化。収集発火がこの区間に被ると抑止される (固定の夜間解析帯は廃止)。drag/リスケ/選択曜日に追随。
//   - now-line: 現在 JST 分の accent 縦線 + 「現在 HH:MM」ラベル。
//
// drag は 5 分 snap → PENDING (適用/取消) を経て確定。weekly/monthly も曜日に関係なく時刻 drag 可 (day 系は保持)。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { JobView } from "../../api/jobs";
import {
  currentJstDow, currentJstMinutes, minutesToRatio, ratioToMinutes, snapMinutes, type DowKey,
} from "./timeUtils";
import { shortLabelForId } from "./categories";
import { CronMarker } from "./CronMarker";
import {
  AxisRuler, DowSelector, HeavyGanttBars, HourGrid,
  IntervalLaneLabel, IntervalRow, Legend, NowLine, ReactiveZone,
} from "./timelineParts";
import {
  deriveHeavyBars, heavyBarSegments, overlappingHeavyBar, packPoints, splitMarkers,
  type PendingDrag,
} from "./timelineModel";

const ROW_HEIGHT = 26; // px, 1 dodge 行の高さ
const ROW_GAP = 4; // px, dodge 行間
const AXIS_HEIGHT = 20; // px, 下部余白
// interval レーンのラベル列 (plot 外の左ガター)。ラベルを plot 内 x=0 に重ねると
// 00 時帯の実行ノード/帯が隠れるため、軸自体をこの幅だけ右から始める。
const LABEL_GUTTER_PX = 140;

export interface JobTimelineProps {
  jobs: JobView[];
  readOnly: boolean;
  schedulerAvailable: boolean;
  selectedJobId: string | null;
  onSelectJob: (id: string) => void;
  onApplyReschedule: (job: JobView, hour: number, minute: number) => void;
}

export function JobTimeline({
  jobs, readOnly, schedulerAvailable,
  selectedJobId, onSelectJob, onApplyReschedule,
}: JobTimelineProps) {
  const [selectedDow, setSelectedDow] = useState<DowKey>(() => currentJstDow());

  // now-line を 1 分ごとに前進させる。
  const [nowMin, setNowMin] = useState(() => currentJstMinutes());
  useEffect(() => {
    const t = setInterval(() => setNowMin(currentJstMinutes()), 60_000);
    return () => clearInterval(t);
  }, []);

  // drag 中/pending 状態。trackRef は listener 内から最新 track 幅を読むため。
  const trackRef = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<PendingDrag | null>(null);
  const [pending, setPending] = useState<PendingDrag | null>(null);
  // ドラッグで実際に移動が起きたか (合成 click の抑止に使う。ページ飛びバグの根治)。
  const dragMovedRef = useRef(false);
  // 掴んだ点と枠左端の px 差。これを差し引くことで「掴んだ瞬間に左端がカーソルへ吸着して
  // ジャンプする」のを防ぎ、掴んだ位置を保ったまま滑らかに動かす。
  const grabOffsetRef = useRef(0);
  const shouldSuppressClick = useCallback((): boolean => {
    if (dragMovedRef.current) {
      dragMovedRef.current = false; // 一度消費したらリセット
      return true;
    }
    return false;
  }, []);

  // cursor x → 分。掴み位置オフセットを差し引き枠左端基準に射影 → 5 分 snap。
  const minutesFromClientX = useCallback((clientX: number): number => {
    const el = trackRef.current;
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    const ratio = (clientX - grabOffsetRef.current - rect.left) / Math.max(1, rect.width);
    return snapMinutes(ratioToMinutes(ratio));
  }, []);

  // pointer drag: track 幅に対する x で分を算出 → snap (枠左端基準)。release で pending 化。
  useEffect(() => {
    if (!dragging) return;
    const startMin = dragging.minutes;
    const move = (e: PointerEvent) => {
      const m = minutesFromClientX(e.clientX);
      if (m !== startMin) dragMovedRef.current = true; // 実移動を記録
      setDragging((cur) => (cur ? { ...cur, minutes: m } : cur));
    };
    const up = (e: PointerEvent) => {
      const m = minutesFromClientX(e.clientX);
      // 移動が無ければ pending を作らず drag を畳む (= 純粋な click として扱う)。
      if (dragMovedRef.current) setPending({ jobId: dragging.jobId, minutes: m });
      setDragging(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [dragging, minutesFromClientX]);

  const draggableBase = !readOnly && schedulerAvailable;
  const { points, intervals, upkeepIntervals, reactives } = useMemo(
    () => splitMarkers(jobs, selectedDow, draggableBase),
    [jobs, selectedDow, draggableBase],
  );
  // 定常処理 (upkeep) は既定で集約 1 行に畳む。展開すると従来の swimlane を出す。
  const [showUpkeep, setShowUpkeep] = useState(false);
  // 集約行のサマリ文言 (例: "PIR判定 毎時:45 · 翻訳 毎時:15 · 自動復旧 30分毎:12")
  const upkeepSummary = useMemo(
    () =>
      upkeepIntervals
        .map((iv) => {
          const short = shortLabelForId(iv.job.id, iv.job.title);
          const off = String(iv.offsetMinutes).padStart(2, "0");
          const sched =
            iv.intervalMinutes === 60 ? `毎時:${off}` : `${iv.intervalMinutes}分毎:${off}`;
          return `${short} ${sched}`;
        })
        .join(" · "),
    [upkeepIntervals],
  );

  // point marker の表示位置 (drag/pending 反映) を織り込む。
  const displayMinutesOf = useCallback(
    (job: JobView, base: number): number => {
      if (dragging?.jobId === job.id) return dragging.minutes;
      if (pending?.jobId === job.id) return pending.minutes;
      return base;
    },
    [dragging, pending],
  );

  const packedRows = useMemo(
    () => packPoints(points.map((p) => ({ ...p, minutes: displayMinutesOf(p.job, p.minutes) }))),
    [points, displayMinutesOf],
  );

  // 重処理 Gantt バー (全 heavy cron ジョブ = 任意曜日)。drag/曜日に追随。
  // 描画は全バー (active=solid / 非active=ghost)、収集抑止/overlap は active バーのみが対象。
  const heavyBars = useMemo(
    () => deriveHeavyBars(jobs, selectedDow, displayMinutesOf),
    [jobs, selectedDow, displayMinutesOf],
  );
  const activeHeavyBars = useMemo(() => heavyBars.filter((b) => b.active), [heavyBars]);
  // interval レーンの帯の切り欠き用に active バーを分区間化。
  const heavyBarSegs = useMemo(() => heavyBarSegments(activeHeavyBars), [activeHeavyBars]);

  // drag 着地点が別 (active) heavy バーと区間 overlap するか (適用前ヒント)。
  const overlapHint = useCallback(
    (minutes: number, selfJobId: string): string | null => {
      const job = jobs.find((j) => j.id === selfJobId);
      const runtime = job?.heavy ? Math.max(1, job.max_runtime_minutes || 5) : 5;
      return overlappingHeavyBar(activeHeavyBars, minutes, runtime, selfJobId);
    },
    [activeHeavyBars, jobs],
  );

  const applyPending = (job: JobView) => {
    if (!pending) return;
    onApplyReschedule(job, Math.floor(pending.minutes / 60), pending.minutes % 60);
    setPending(null);
  };

  // 総行数 = point dodge 行 + interval 行 + 定常処理 (集約ヘッダ 1 行 + 展開時の個別行)。
  // reactive は軸外ゾーンなので行数に含めない。
  const upkeepHeaderRows = upkeepIntervals.length > 0 ? 1 : 0;
  const upkeepBodyRows = showUpkeep ? upkeepIntervals.length : 0;
  const totalRows = packedRows.length + intervals.length + upkeepHeaderRows + upkeepBodyRows;
  const bodyHeight = totalRows * ROW_HEIGHT + Math.max(0, totalRows - 1) * ROW_GAP;
  const intervalRowBase = packedRows.length;
  const upkeepHeaderIndex = packedRows.length + intervals.length;
  const rowY = (rowIndex: number): number => rowIndex * (ROW_HEIGHT + ROW_GAP);

  return (
    <section className="bg-surface-1 border border-border-subtle rounded-lg p-3 md:p-4 space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="m-0 text-sm font-bold text-fg">24 時間スケジュール (JST)</h3>
          <p className="m-0 text-[11.5px] text-fg-muted mt-0.5">
            全ジョブを 1 本の軸に集約。丸いラベルの左端が実行時刻。黄色バーは重い処理の想定所要 (幅=時間) で、収集はこの重処理に被る分だけ自動停止します。毎日/毎週の予定は左右ドラッグで時刻変更 (5 分刻み・適用前に確認)、週次も任意の曜日から掴めます。
          </p>
        </div>
        <DowSelector selected={selectedDow} onChange={setSelectedDow} />
      </div>

      {/* ルーラーはガター分だけ右から始める (plot と x 座標を一致させる) */}
      <div className="flex">
        <div className="shrink-0" style={{ width: LABEL_GUTTER_PX }} aria-hidden />
        <div className="flex-1 min-w-0"><AxisRuler /></div>
      </div>

      {/* 左=interval ラベル列 (plot 外) / 右=統合軸 (グリッド/帯/バー/now-line + marker rows) */}
      <div className="flex">
        <div className="relative shrink-0" style={{ width: LABEL_GUTTER_PX, height: bodyHeight + AXIS_HEIGHT }}>
          {intervals.map((iv, i) => (
            <div key={`ilabel-${iv.job.id}`} className="absolute inset-x-0" style={{ top: rowY(intervalRowBase + i), height: ROW_HEIGHT }}>
              <IntervalLaneLabel
                marker={iv}
                isSelected={selectedJobId === iv.job.id}
                onSelectJob={onSelectJob}
              />
            </div>
          ))}
          {/* 定常処理 (upkeep) の集約ヘッダ + 展開時の個別ラベル */}
          {upkeepIntervals.length > 0 && (
            <div className="absolute inset-x-0" style={{ top: rowY(upkeepHeaderIndex), height: ROW_HEIGHT }}>
              <div className="flex h-full items-center justify-end pr-2">
                <button
                  type="button"
                  onClick={() => setShowUpkeep((v) => !v)}
                  title="定常キュー処理 (時刻に運用上の意味がない補助ジョブ)。クリックで展開/折畳"
                  className="inline-flex items-center gap-1 text-[10px] rounded px-1.5 py-0.5 border bg-surface-2 text-fg-subtle border-border-subtle hover:text-fg transition-colors"
                >
                  {showUpkeep ? (
                    <ChevronDown className="h-3 w-3" aria-hidden />
                  ) : (
                    <ChevronRight className="h-3 w-3" aria-hidden />
                  )}
                  定常処理・{upkeepIntervals.length}件
                </button>
              </div>
            </div>
          )}
          {showUpkeep &&
            upkeepIntervals.map((iv, i) => (
              <div key={`ulabel-${iv.job.id}`} className="absolute inset-x-0" style={{ top: rowY(upkeepHeaderIndex + 1 + i), height: ROW_HEIGHT }}>
                <IntervalLaneLabel
                  marker={iv}
                  isSelected={selectedJobId === iv.job.id}
                  onSelectJob={onSelectJob}
                />
              </div>
            ))}
        </div>
        <div
          ref={trackRef}
          className="relative flex-1 min-w-0 border border-border-subtle rounded bg-surface-2/40"
          style={{ height: bodyHeight + AXIS_HEIGHT }}
        >
          <HourGrid />
          <HeavyGanttBars bars={heavyBars} />
          <NowLine nowMin={nowMin} />

          {/* point marker rows (packed) */}
          {packedRows.map((row, ri) => (
            <div key={`prow-${ri}`} className="absolute inset-x-0" style={{ top: rowY(ri), height: ROW_HEIGHT }}>
              {row.map((pm) => (
                <CronMarker
                  key={pm.job.id}
                  marker={pm}
                  isSelected={selectedJobId === pm.job.id}
                  onSelectJob={onSelectJob}
                  dragging={dragging}
                  pending={pending}
                  onStartDrag={(jobId, minutes, e) => {
                    e.preventDefault();
                    setPending(null);
                    dragMovedRef.current = false;
                    // 掴んだ点と枠左端 (=時刻 x) の px 差を記録 → ドラッグ中はこの差を保つ。
                    const el = trackRef.current;
                    if (el) {
                      const rect = el.getBoundingClientRect();
                      const frameLeftX = rect.left + minutesToRatio(minutes) * rect.width;
                      grabOffsetRef.current = e.clientX - frameLeftX;
                    } else {
                      grabOffsetRef.current = 0;
                    }
                    setDragging({ jobId, minutes });
                  }}
                  shouldSuppressClick={shouldSuppressClick}
                  overlapHint={overlapHint}
                  onApplyPending={applyPending}
                  onCancelPending={() => setPending(null)}
                />
              ))}
            </div>
          ))}

          {/* interval rows (帯 + 実行ノードのみ — ラベルは左ガター列) */}
          {intervals.map((iv, i) => (
            <div key={`irow-${iv.job.id}`} className="absolute inset-x-0" style={{ top: rowY(intervalRowBase + i), height: ROW_HEIGHT }}>
              <IntervalRow
                marker={iv}
                heavyBars={activeHeavyBars}
                heavyBarSegments={heavyBarSegs}
              />
            </div>
          ))}

          {/* 定常処理: 折畳時はサマリ 1 行 (点列は描かない)、展開時は従来の swimlane */}
          {upkeepIntervals.length > 0 && !showUpkeep && (
            <div className="absolute inset-x-0" style={{ top: rowY(upkeepHeaderIndex), height: ROW_HEIGHT }}>
              <button
                type="button"
                onClick={() => setShowUpkeep(true)}
                title="展開して個別の実行タイミングを表示"
                className="flex h-full w-full items-center px-2 text-[10px] text-fg-subtle hover:text-fg-muted text-left"
              >
                <span className="truncate">{upkeepSummary}</span>
              </button>
            </div>
          )}
          {showUpkeep &&
            upkeepIntervals.map((iv, i) => (
              <div key={`urow-${iv.job.id}`} className="absolute inset-x-0" style={{ top: rowY(upkeepHeaderIndex + 1 + i), height: ROW_HEIGHT }}>
                <IntervalRow
                  marker={iv}
                  heavyBars={activeHeavyBars}
                  heavyBarSegments={heavyBarSegs}
                />
              </div>
            ))}
        </div>
      </div>

      {/* reactive 専用ゾーン (時間軸から外す) */}
      <ReactiveZone markers={reactives} selectedJobId={selectedJobId} onSelectJob={onSelectJob} />

      <Legend />
    </section>
  );
}
