// 統合 timeline の描画パーツ (背景レイヤー / 行 / エディタ / 凡例)。
// JobTimeline.tsx から切り出し (ファイル肥大の回避)。

import { useMemo } from "react";
import { Clock, Repeat, Zap } from "lucide-react";
import { categoryForId, markerColorForId, shortLabelForId } from "./categories";
import {
  DOW_KEYS, DOW_LABEL, MINUTES_PER_DAY, minutesToHhmm, minutesToRatio, type DowKey,
} from "./timeUtils";
import { buildMarkerTitle } from "./CronMarker";
import { RunStatusDot } from "./RunStatusDot";
import {
  type HeavyBar, type IntervalMarker, type ReactiveMarker, type Segment,
} from "./timelineModel";

// ---- 時刻ルーラー (3h ラベル + 1h 目盛り tick) --------------------------
// 1 時間の刻みはルーラー下端の小さな tick のみ。描画域 (ドラッグ領域) はクリーンに保つ。
export function AxisRuler() {
  return (
    <div className="relative h-5 select-none">
      {/* 1h 目盛り: ルーラー下端に小さな tick (3h は長め) */}
      {Array.from({ length: 25 }, (_, h) => h).map((h) => (
        <div
          key={`t-${h}`}
          className={`absolute bottom-0 w-px bg-border-subtle ${h % 3 === 0 ? "h-2" : "h-1"}`}
          style={{ left: `${(h / 24) * 100}%` }}
          aria-hidden
        />
      ))}
      {/* 3h ラベル */}
      {Array.from({ length: 9 }, (_, i) => i * 3).map((h) => (
        <div
          key={h}
          className={`absolute top-0 text-[10px] text-fg-subtle font-mono tnum ${
            h === 0 ? "" : h === 24 ? "-translate-x-full" : "-translate-x-1/2"
          }`}
          style={{ left: `${(h / 24) * 100}%` }}
        >
          {String(h).padStart(2, "0")}
        </div>
      ))}
    </div>
  );
}

// track 内の全高グリッド線 (背景、最後方)。控えめ・均一色。3h 境界のみ極薄で引く
// (1h 線は描画域に引かず、時刻はルーラーの tick で追う)。
export function HourGrid() {
  return (
    <>
      {[3, 6, 9, 12, 15, 18, 21].map((h) => (
        <div
          key={h}
          className="absolute inset-y-0 w-px bg-border-subtle/40"
          style={{ left: `${(h / 24) * 100}%` }}
          aria-hidden
        />
      ))}
    </>
  );
}

// ---- 重処理 Gantt バー (背景スパン) -------------------------------------
// [開始 = ジョブ時刻(=ピル枠左端 x), 幅 = max_runtime_minutes]。ピルと同じ可視性ルールで全曜日表示。
//   - active: solid amber + 開始 2px 左ボーダー (= 収集停止ゾーン)。重なると濃くなり資源競合を可視化。
//   - 非 active (選択曜日に稼働しない weekly): faint + dashed の ghost (収集は止めない、informational)。
export function HeavyGanttBars({ bars }: { bars: HeavyBar[] }) {
  return (
    <>
      {bars.map((b) => {
        const widthMin = Math.min(b.runtime, MINUTES_PER_DAY - b.start); // 軸右端でクランプ
        const cls = b.active
          ? "bg-warning/[0.10] border-l-2 border-warning/70"
          : "bg-warning/[0.05] border-l border-dashed border-warning/25";
        const title = b.active
          ? `重処理: ${b.label} — 開始 ${minutesToHhmm(b.start)} · 想定 ${b.runtime}分 (この間は収集停止)`
          : `重処理: ${b.label} — ${b.dowLabels || "他曜日"}のみ稼働 (選択曜日は非稼働)`;
        return (
          <div
            key={`hb-${b.jobId}`}
            className={`absolute inset-y-0 ${cls}`}
            title={title}
            style={{
              left: `${minutesToRatio(b.start) * 100}%`,
              width: `${(widthMin / MINUTES_PER_DAY) * 100}%`,
            }}
            aria-hidden
          />
        );
      })}
    </>
  );
}

// ---- now-line (ラベル付き) ----------------------------------------------
export function NowLine({ nowMin }: { nowMin: number }) {
  const nearRight = nowMin > MINUTES_PER_DAY - 90;
  return (
    <div
      className="absolute inset-y-0 w-px bg-accent z-10"
      style={{ left: `${minutesToRatio(nowMin) * 100}%` }}
      aria-hidden
    >
      <span className="absolute -top-0.5 left-1/2 -translate-x-1/2 w-1.5 h-1.5 rounded-full bg-accent" />
      <span
        className={`absolute -top-0.5 text-[9px] font-mono tnum text-bg bg-accent rounded px-1 leading-[1.4] whitespace-nowrap ${
          nearRight ? "right-1" : "left-1"
        }`}
      >
        現在 {minutesToHhmm(nowMin)}
      </span>
    </div>
  );
}

// ---- 曜日セレクタ (segmented control) ----------------------------------
export function DowSelector({ selected, onChange }: { selected: DowKey; onChange: (d: DowKey) => void }) {
  return (
    <div className="inline-flex rounded-md border border-border-subtle overflow-hidden" role="group" aria-label="曜日選択">
      {DOW_KEYS.map((d) => {
        const active = d === selected;
        return (
          <button
            key={d}
            onClick={() => onChange(d)}
            aria-pressed={active}
            className={`w-7 h-7 text-xs font-semibold transition-colors ${
              active ? "bg-accent text-bg" : "bg-surface-2 text-fg-muted hover:text-fg hover:bg-surface-3"
            }`}
          >
            {DOW_LABEL[d]}
          </button>
        );
      })}
    </div>
  );
}

// ---- interval row (横帯 + 実行ノード + heavy 重なり gap) ----------------
// thin line を廃止。active 時間帯にカテゴリ色の低不透明度の横帯を敷き、収集を抑止する
// 区間 (active な heavy 処理と被る所) はバーを切り欠き (gap)。実行時刻に見やすいノードを置く。
// respects_analysis_window===true の収集ジョブのみ抑止。tick 抑止判定は backend と同じ
// overlap: [m, m + job.max_runtime_minutes] が heavy バー [start, start+runtime] と重なる。
export function IntervalRow({ marker, heavyBars, heavyBarSegments }: {
  marker: IntervalMarker;
  heavyBars: HeavyBar[]; // active な heavy バー (= 収集停止ゾーン)
  heavyBarSegments: Segment[]; // heavyBars を分区間化したもの (帯の切り欠き用)
}) {
  const { job, intervalMinutes, offsetMinutes } = marker;
  const suppresses = job.respects_analysis_window && heavyBars.length > 0;
  const collectionRuntime = Math.max(1, job.max_runtime_minutes || 5);
  const color = markerColorForId(job.id);

  // active 横帯の区間 (0..1440 から heavy 停止区間を差し引く)。抑止しないジョブは全域。
  const activeBands = useMemo(
    () => (suppresses ? subtractSegments(heavyBarSegments) : [{ from: 0, to: MINUTES_PER_DAY }]),
    [suppresses, heavyBarSegments],
  );
  // 実行ノード (heavy と被る発火は除く)。毎時=密、3時間毎=疎。
  const nodes = useMemo(() => {
    const out: number[] = [];
    const step = Math.max(5, intervalMinutes);
    for (let m = offsetMinutes; m < MINUTES_PER_DAY; m += step) {
      if (suppresses && overlapsHeavy(heavyBars, m, collectionRuntime)) continue;
      out.push(m);
    }
    return out;
  }, [intervalMinutes, offsetMinutes, suppresses, heavyBars, collectionRuntime]);

  return (
    <div className="relative w-full h-full">
      {/* active 横帯 (heavy 重なりで切り欠き) */}
      {activeBands.map((b, i) => (
        <div
          key={`band-${i}`}
          className={`absolute inset-y-[7px] rounded-sm ${color.dot} opacity-[0.16]`}
          style={{ left: `${minutesToRatio(b.from) * 100}%`, width: `${((b.to - b.from) / MINUTES_PER_DAY) * 100}%` }}
          aria-hidden
        />
      ))}
      {/* 実行ノード (小さめの塗り点) */}
      {nodes.map((m, i) => (
        <div
          key={`n-${i}`}
          className={`absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-1.5 h-1.5 rounded-full ${color.dot}`}
          style={{ left: `${minutesToRatio(m) * 100}%` }}
          aria-hidden
        />
      ))}
    </div>
  );
}

// ---- interval レーンのラベル (plot 外の左ガター列に置く) ------------------
// 以前は plot 内の x=0 に絶対配置していたため 00 時帯の帯/ノード/重処理バーを隠していた。
// ガター固定幅 + truncate で全レーンのラベル幅が揃う。「重処理中は停止」は tooltip と
// 詳細パネルに移し、ラベルは「略称 · 間隔」だけに保つ。
export function IntervalLaneLabel({ marker, isSelected, onSelectJob }: {
  marker: IntervalMarker;
  isSelected: boolean;
  onSelectJob: (id: string) => void;
}) {
  const { job, intervalMinutes, offsetMinutes } = marker;
  const color = markerColorForId(job.id);
  const Icon = categoryForId(job.id).icon;
  const short = shortLabelForId(job.id, job.title);
  const baseLabel = intervalMinutes % 60 === 0
    ? intervalMinutes === 60 ? "毎時" : `${intervalMinutes / 60}時間毎`
    : `${intervalMinutes}分毎`;
  return (
    <div className="flex h-full items-center justify-end pr-2">
      <button
        type="button"
        onClick={() => onSelectJob(job.id)}
        title={buildMarkerTitle(job, offsetMinutes)}
        className={`inline-flex max-w-full items-center gap-1 text-[10px] rounded px-1.5 py-0.5 border bg-surface-2 ${color.text} ${color.border} ${
          isSelected ? "ring-2 ring-accent-ring" : ""
        } ${!job.enabled ? "opacity-45 line-through decoration-1" : ""}`}
      >
        <Icon className="h-3 w-3 shrink-0" aria-hidden />
        <span className="font-semibold truncate">{short}</span>
        <span className="text-fg-subtle shrink-0">·</span>
        <span className="shrink-0">{baseLabel}</span>
        <RunStatusDot status={job.last_run?.status} positionClass="relative flex ml-0.5 shrink-0" />
      </button>
    </div>
  );
}

// 0..1440 から与えられた segments を差し引いた「残り」区間を返す (横帯の切り欠き計算)。
function subtractSegments(cut: Segment[]): Segment[] {
  const sorted = [...cut].filter((s) => s.to > s.from).sort((a, b) => a.from - b.from);
  const out: Segment[] = [];
  let cursor = 0;
  for (const s of sorted) {
    const from = Math.max(0, s.from);
    const to = Math.min(MINUTES_PER_DAY, s.to);
    if (from > cursor) out.push({ from: cursor, to: from });
    cursor = Math.max(cursor, to);
  }
  if (cursor < MINUTES_PER_DAY) out.push({ from: cursor, to: MINUTES_PER_DAY });
  return out;
}

// 収集発火 [m, m+runtime] が heavy バー [start, start+runtime] のいずれかと重なるか。
function overlapsHeavy(bars: HeavyBar[], m: number, runtime: number): boolean {
  const end = m + runtime;
  for (const b of bars) {
    if (m < b.start + b.runtime && b.start < end) return true;
  }
  return false;
}

// ---- reactive 専用ゾーン (時間軸から外す) -------------------------------
// auto-trigger 等は時刻を持たない。x 位置で時刻を暗示しないよう、軸の外の帯に
// 「イベント駆動 (時刻非依存)」と明示して並べる。
export function ReactiveZone({ markers, selectedJobId, onSelectJob }: {
  markers: ReactiveMarker[];
  selectedJobId: string | null;
  onSelectJob: (id: string) => void;
}) {
  if (markers.length === 0) return null;
  return (
    <div className="flex items-center gap-2 flex-wrap bg-surface-2/60 border border-dashed border-border-subtle rounded-md px-2.5 py-1.5 text-[10px]">
      <span className="inline-flex items-center gap-1 text-fg-muted font-semibold shrink-0">
        <Zap className="h-3.5 w-3.5 text-warning" aria-hidden />
        状況に応じて自動実行 (時刻非依存)
      </span>
      <span className="text-fg-subtle shrink-0">RSS の取込が急増したときに実行。定刻を持たないため軸外に表示。</span>
      {markers.map((rc) => {
        const { job } = rc;
        const isSelected = selectedJobId === job.id;
        const dh = job.debounce_hours;
        return (
          <button
            key={job.id}
            type="button"
            onClick={() => onSelectJob(job.id)}
            title={buildMarkerTitle(job, 0)}
            className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 border bg-surface-1 border-border-subtle text-fg-muted ${
              isSelected ? "ring-2 ring-accent-ring text-fg" : ""
            } ${!job.enabled ? "opacity-45 line-through decoration-1" : ""}`}
          >
            <span className="truncate max-w-[180px]">{job.title}</span>
            {dh != null && <span className="text-fg-faint shrink-0">連続実行の抑制 {dh}時間</span>}
            <RunStatusDot status={job.last_run?.status} positionClass="relative flex" />
          </button>
        );
      })}
    </div>
  );
}

// ---- 凡例 ---------------------------------------------------------------
export function Legend() {
  return (
    <div className="flex items-center gap-3 flex-wrap text-[10px] text-fg-subtle pt-1 border-t border-border-subtle">
      <LegendDot cls="bg-accent" text="収集" />
      <LegendDot cls="bg-success" text="配信" />
      <LegendDot cls="bg-accent-hover" text="分析・総括" />
      <LegendDot cls="bg-warning" text="学習・辞書" />
      <LegendDot cls="bg-fg-muted" text="保守" />
      <LegendDot cls="bg-fg-subtle" text="定常処理" />
      <span className="inline-flex items-center gap-1">
        <Repeat className="h-3 w-3" aria-hidden /> 定間隔
      </span>
      <span className="inline-flex items-center gap-1">
        <Zap className="h-3 w-3 text-warning" aria-hidden /> 状況に応じて実行
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="w-2.5 h-2.5 bg-warning/[0.14] border-l-2 border-warning/70" aria-hidden /> 重処理帯 (幅=想定時間・この間は収集停止)
      </span>
      <span className="inline-flex items-center gap-1">
        <Clock className="h-3 w-3 text-accent" aria-hidden /> 現在 (JST)
      </span>
    </div>
  );
}

function LegendDot({ cls, text }: { cls: string; text: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`w-2 h-2 rounded-full ${cls}`} aria-hidden /> {text}
    </span>
  );
}
