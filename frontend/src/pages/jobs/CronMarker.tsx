// cron point marker (daily / weekly / monthly) と tooltip 生成。
//
// アンカー: ピルの外枠 (border) 左端 = 時刻 x。つまり「枠の先頭 = 時刻」。中身は
// [カテゴリ lucide アイコン][短い略称][時刻][曜日/日 badge]。右端付近は左反転で見切れ防止。
//
// [重要] ドラッグ飛びバグ対策: 親が window pointermove で移動を検知したら shouldSuppressClick() が
//   true を返す。onClick はそれを読んで選択を抑止する。→ ドラッグ = 時刻変更のみ。
//   選択/スクロールは誘発しない (marker button から pointer が外れても親側で確実に検知できる)。

import { AlertTriangle, Check, X } from "lucide-react";
import type { JobView } from "../../api/jobs";
import { formatJst, relativeFromNow } from "../../utils/date";
import { categoryForId, markerColorForId, shortLabelForId, type MarkerColor } from "./categories";
import { DOW_LABEL, minutesToHhmm, minutesToRatio, parseDowField } from "./timeUtils";
import type { PendingDrag, PointMarker } from "./timelineModel";
import { RunStatusDot } from "./RunStatusDot";
import { vocabLabel } from "../../hooks/useVocab";

// 枠が軸右端付近の時、pill 概算幅 (分) を超えたら左反転する閾値。略称込みで広めに。
const FLIP_THRESHOLD_MIN = 1440 - 180;

export interface CronMarkerProps {
  marker: PointMarker;
  isSelected: boolean;
  onSelectJob: (id: string) => void;
  dragging: PendingDrag | null;
  pending: PendingDrag | null;
  onStartDrag: (jobId: string, minutes: number, e: React.PointerEvent) => void;
  // 直前の pointer 操作がドラッグ (移動あり) だったら true。click 抑止に使う (呼ぶと消費/リセット)。
  shouldSuppressClick: () => boolean;
  // 着地点で (自ジョブ以外の) 重処理バーと区間 overlap するか。適用前の衝突警告に使う。
  overlapHint: (minutes: number, selfJobId: string) => string | null;
  onApplyPending: (job: JobView) => void;
  onCancelPending: () => void;
}

export function CronMarker(props: CronMarkerProps) {
  const {
    marker, isSelected, onSelectJob, dragging, pending, onStartDrag,
    shouldSuppressClick, overlapHint, onApplyPending, onCancelPending,
  } = props;
  const { job, active, isWeekly, isMonthly, draggable } = marker;

  const isDragging = dragging?.jobId === job.id;
  const isPending = pending?.jobId === job.id;
  const displayMin = marker.minutes; // 既に drag/pending 反映済み

  const color: MarkerColor = markerColorForId(job.id);
  const Icon = categoryForId(job.id).icon;
  const short = shortLabelForId(job.id, job.title);
  const clash = overlapHint(displayMin, job.id);

  const dowBadge = isWeekly
    ? [...parseDowField(job.day_of_week)].map((d) => DOW_LABEL[d]).join("")
    : null;
  const dayBadge = isMonthly ? `${job.day!.trim()}日` : null;
  const dimmed = !job.enabled || job.is_paused === true;
  const notToday = isWeekly && !active;
  const flip = displayMin >= FLIP_THRESHOLD_MIN;

  const left = `${minutesToRatio(displayMin) * 100}%`;

  return (
    <>
      <button
        type="button"
        onClick={() => {
          // ドラッグ後の合成 click は選択を誘発させない (ページ飛び防止)。
          if (shouldSuppressClick()) return;
          onSelectJob(job.id);
        }}
        onPointerDown={(e) => {
          if (!draggable) return;
          if (e.button !== 0) return;
          onStartDrag(job.id, displayMin, e);
        }}
        title={buildMarkerTitle(job, displayMin)}
        aria-label={`${job.title} ${minutesToHhmm(displayMin)}`}
        // leading-frame アンカー: 枠左端 = 時刻 x。右端付近は右→左へ展開。
        className={`absolute top-1/2 -translate-y-1/2 z-20 inline-flex items-center gap-0.5 rounded-full border pl-1 pr-1.5 py-0.5 text-[9.5px] font-mono tnum leading-none transition-shadow bg-surface-2 ${color.border} ${color.text} ${
          isSelected ? "ring-2 ring-accent-ring" : ""
        } ${active ? "" : "border-dashed"} ${notToday ? "opacity-70" : ""} ${
          draggable ? "cursor-grab active:cursor-grabbing touch-none" : "cursor-pointer"
        } ${isDragging ? "shadow-lg" : ""} ${dimmed ? "opacity-45 line-through decoration-1" : ""} ${
          flip ? "-translate-x-full flex-row-reverse pl-1.5 pr-1" : ""
        }`}
        style={{ left }}
      >
        <Icon className="h-2.5 w-2.5 shrink-0" aria-hidden />
        <span className="font-sans font-semibold">{short}</span>
        <span className="text-fg-muted">{minutesToHhmm(displayMin)}</span>
        {dowBadge && (
          <span className={`px-1 rounded ${active ? "bg-accent text-bg" : "bg-surface-3 text-fg-muted"}`}>
            {dowBadge}
          </span>
        )}
        {dayBadge && <span className="px-1 rounded bg-surface-3 text-fg-muted">{dayBadge}</span>}
        {/* 直近状態 (failed/running のみ表示)。ピル右上角に絶対配置 (flip 時は左上)。 */}
        <RunStatusDot
          status={job.last_run?.status}
          positionClass={`absolute -top-1 z-30 flex ${flip ? "-left-1" : "-right-1"}`}
        />
      </button>

      {/* pending: 適用/取消 の inline 制御 + 重処理 衝突警告 */}
      {isPending && (
        <div className={`absolute z-30 top-full ${flip ? "-translate-x-full" : ""}`} style={{ left }}>
          <div className="inline-flex items-center gap-1 bg-bg border border-border-emphasized rounded shadow-lg px-1 py-0.5">
            <span className="text-[10px] font-mono text-fg tnum px-1">{minutesToHhmm(pending!.minutes)}</span>
            {clash && (
              <span className="text-[9.5px] text-warning inline-flex items-center gap-0.5" title={`重処理と重複: ${clash}`}>
                <AlertTriangle className="h-3 w-3" aria-hidden /> 重処理重複
              </span>
            )}
            <button
              onClick={() => onApplyPending(job)}
              title="適用"
              aria-label="時刻変更を適用"
              className="w-5 h-5 inline-flex items-center justify-center rounded bg-accent text-bg hover:bg-accent-hover"
            >
              <Check className="h-3 w-3" aria-hidden />
            </button>
            <button
              onClick={onCancelPending}
              title="取消"
              aria-label="時刻変更を取消"
              className="w-5 h-5 inline-flex items-center justify-center rounded bg-surface-2 border border-border-subtle text-fg-muted hover:text-fg"
            >
              <X className="h-3 w-3" aria-hidden />
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// tooltip: title / 時刻 / 次回 / 最終結果 / 想定処理時間(heavy) / 収集抑止対象 / danger_note。
export function buildMarkerTitle(job: JobView, displayMin: number): string {
  const parts = [job.title, `時刻 ${minutesToHhmm(displayMin)} JST`];
  if (job.next_run_at) parts.push(`次回 ${formatJst(job.next_run_at)}`);
  if (job.last_run) parts.push(`前回 ${vocabLabel("run_status", job.last_run.status)} (${relativeFromNow(job.last_run.last_run_at)})`);
  if (job.heavy) parts.push(`想定処理時間 ${job.max_runtime_minutes}分 (この間は収集停止)`);
  if (job.respects_analysis_window) parts.push("収集ジョブ (重処理中は自動停止)");
  if (job.danger_note) parts.push(`注意: ${job.danger_note}`);
  return parts.join("\n");
}
