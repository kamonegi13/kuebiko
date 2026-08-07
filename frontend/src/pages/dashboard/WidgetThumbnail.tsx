// ツールボックス用の模式サムネイル。widget の「見た目の型」を軽量 CSS で示す
// (フル screenshot でなく schematic。データ取得なし・常に最新)。種別は ThumbKind。

import type { ThumbKind } from "./shared";

// 横ライン (リスト行 / ラベルの stub)。
function Line({ w = "100%", tone = "subtle" }: { w?: string; tone?: "subtle" | "accent" | "fg" }) {
  const bg = tone === "accent" ? "bg-accent/60" : tone === "fg" ? "bg-fg-muted/50" : "bg-surface-3";
  return <div className={`h-1.5 rounded-sm ${bg}`} style={{ width: w }} />;
}

function HBar({ w, tone = "accent" }: { w: string; tone?: "accent" | "subtle" }) {
  return (
    <div className="flex items-center gap-1">
      <div className="h-1.5 w-3 rounded-sm bg-surface-3 shrink-0" />
      <div className={`h-1.5 rounded-sm ${tone === "accent" ? "bg-accent/60" : "bg-surface-3"}`} style={{ width: w }} />
    </div>
  );
}

function VBars({ heights }: { heights: number[] }) {
  return (
    <div className="flex items-end gap-[2px] h-full">
      {heights.map((h, i) => (
        <div key={i} className="flex-1 bg-accent/60 rounded-sm" style={{ height: `${h}%` }} />
      ))}
    </div>
  );
}

function KpiCell() {
  return (
    <div className="flex-1 bg-surface-3/60 rounded p-1 flex flex-col gap-1">
      <Line w="60%" />
      <div className="h-3 w-2/3 rounded-sm bg-accent/60" />
      <div className="flex items-end gap-[1px] h-2">
        {[40, 70, 50, 90, 60].map((h, i) => (
          <div key={i} className="flex-1 bg-accent/30 rounded-sm" style={{ height: `${h}%` }} />
        ))}
      </div>
    </div>
  );
}

function SparkRow() {
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1.5 w-1/3 rounded-sm bg-surface-3 shrink-0" />
      <div className="flex items-end gap-[1px] h-3 ml-auto w-1/3">
        {[30, 60, 40, 80, 55].map((h, i) => (
          <div key={i} className="flex-1 bg-accent/50 rounded-sm" style={{ height: `${h}%` }} />
        ))}
      </div>
    </div>
  );
}

function Body({ kind }: { kind: ThumbKind }) {
  switch (kind) {
    case "kpi":
      return (
        <div className="flex gap-1 h-full">
          <KpiCell />
          <KpiCell />
          <KpiCell />
        </div>
      );
    case "bars":
      return (
        <div className="flex flex-col justify-center gap-1.5 h-full">
          <HBar w="80%" />
          <HBar w="55%" />
          <HBar w="38%" />
          <HBar w="22%" tone="subtle" />
        </div>
      );
    case "sparkline":
      return (
        <div className="flex flex-col justify-center gap-1.5 h-full">
          <SparkRow />
          <SparkRow />
          <SparkRow />
        </div>
      );
    case "list":
      return (
        <div className="flex flex-col justify-center gap-1.5 h-full">
          {(["bg-critical/70", "bg-warning/70", "bg-surface-3", "bg-surface-3"] as const).map((tick, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <div className={`h-3 w-0.5 rounded-sm ${tick}`} />
              <Line w={`${90 - i * 12}%`} tone="fg" />
            </div>
          ))}
        </div>
      );
    case "status":
      return (
        <div className="flex flex-col justify-center gap-1.5 h-full">
          <div className="h-3 w-full rounded bg-success/30" />
          <div className="flex gap-1">
            <div className="h-2 w-8 rounded-sm bg-surface-3" />
            <div className="h-2 w-6 rounded-sm bg-surface-3" />
          </div>
        </div>
      );
    case "text":
      return (
        <div className="flex flex-col justify-center gap-1.5 h-full">
          <Line w="100%" tone="fg" />
          <Line w="96%" />
          <Line w="92%" />
          <Line w="55%" />
        </div>
      );
    case "trend":
      return (
        <div className="h-full py-1">
          <VBars heights={[40, 60, 35, 75, 50, 85, 45, 65, 55, 90]} />
        </div>
      );
    case "composition":
      return (
        <div className="flex flex-col justify-center gap-1.5 h-full">
          <Line w="40%" />
          <div className="flex h-3 w-full rounded-sm overflow-hidden">
            <div className="bg-critical/70" style={{ width: "30%" }} />
            <div className="bg-warning/70" style={{ width: "45%" }} />
            <div className="bg-surface-3" style={{ width: "25%" }} />
          </div>
        </div>
      );
    case "grid":
      return (
        <div className="grid grid-cols-2 gap-1.5 h-full content-center">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="flex flex-col gap-1">
              <Line w="70%" />
              <div className="h-2.5 w-1/2 rounded-sm bg-accent/50" />
            </div>
          ))}
        </div>
      );
    default:
      return <div className="h-full flex items-center justify-center text-fg-subtle text-[10px]">preview</div>;
  }
}

export function WidgetThumbnail({ kind }: { kind?: ThumbKind }) {
  return (
    <div className="bg-surface-2 border border-border-subtle rounded h-14 w-full p-2 overflow-hidden">
      {kind ? <Body kind={kind} /> : <Body kind="list" />}
    </div>
  );
}
