// 大数字 KPI タイル: ラベル + 大きな数値 + 前日比(方向のみ・中立色) + ミニ推移 + 注記。
// delta の色は good/bad を含意しない (CTI では「増加=悪」もあり得るため中立 text-fg-muted)。

import type { ReactNode } from "react";
import { ArrowUp, ArrowDown, ChevronRight } from "lucide-react";
import { Sparkline } from "./Sparkline";

type Tone = "default" | "critical" | "warning" | "success" | "accent";

interface KpiTileProps {
  label: string;
  value: number | string;
  delta?: number | null; // 前日比など (符号付き)。0/未指定なら非表示
  deltaTitle?: string; // delta の意味 (既定「前日比」)。高重要度等は「前期比」
  trend?: number[]; // ミニ推移
  tone?: Tone;
  href?: string;
  note?: string; // 「観測/報道 件数」等の誠実ラベル
  icon?: ReactNode;
  // モバイル「狭く縦長」モード: padding/数値を縮小し sparkline を省略 (省スペース)。
  compact?: boolean;
}

const VALUE_COLOR: Record<Tone, string> = {
  default: "text-fg",
  critical: "text-critical",
  warning: "text-warning",
  success: "text-success",
  accent: "text-accent",
};

export function KpiTile({ label, value, delta, deltaTitle = "前日比", trend, tone = "default", href, note, icon, compact }: KpiTileProps) {
  const up = (delta ?? 0) > 0;
  const down = (delta ?? 0) < 0;
  const body = (
    <div className={`bg-surface-1 border border-border-subtle rounded-lg h-full flex flex-col group-hover:border-border-emphasized transition-colors ${compact ? "p-2.5 gap-0.5" : "p-4 gap-1.5"}`}>
      <div className="flex items-center justify-between gap-2">
        <span className={`inline-flex items-center gap-1.5 text-fg-muted truncate ${compact ? "text-[11px]" : "text-xs"}`} title={label}>
          {icon}
          {label}
        </span>
        {href && !compact && <ChevronRight className="h-3.5 w-3.5 text-fg-subtle shrink-0" aria-hidden />}
      </div>
      <div className="flex items-end gap-1.5">
        <span className={`leading-none font-semibold tnum ${VALUE_COLOR[tone]} ${compact ? "text-xl" : "text-2xl"}`}>
          {typeof value === "number" ? value.toLocaleString() : value}
        </span>
        {delta != null && delta !== 0 && (
          <span className="inline-flex items-center text-xs font-semibold tnum text-fg-muted" title={deltaTitle}>
            {up ? <ArrowUp className="h-3 w-3" aria-hidden /> : down ? <ArrowDown className="h-3 w-3" aria-hidden /> : null}
            {Math.abs(delta)}
          </span>
        )}
      </div>
      {!compact && trend && trend.length > 0 && <Sparkline data={trend} width={140} height={16} className="text-accent/40 w-full" />}
      {note && <span className={`text-fg-subtle truncate ${compact ? "text-[10px]" : "text-[11px]"}`}>{note}</span>}
    </div>
  );
  return href ? (
    <a href={href} className="group block h-full no-underline">
      {body}
    </a>
  ) : (
    <div className="group h-full">{body}</div>
  );
}
