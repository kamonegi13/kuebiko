// 横棒ランキング (ラベル + バー + 件数)。セクター/国/国籍などの KPI に汎用利用。
// taxonomy 色は color(inline style)で指定可。suffix に信頼度構成などの注記を付けられる。

import type { ReactNode } from "react";
import { ArrowUp, ArrowDown } from "lucide-react";

export interface RankRow {
  label: string;
  value: number; // バー幅の基準値 (share% でもカウントでも可)
  valueLabel?: string; // 数値の表示上書き (例: "38%")。未指定は value.toLocaleString()
  delta?: number; // 前期比 (符号付き)。指定時は ▲/▼ で「何が変化を駆動したか」を示す
  deltaSuffix?: string; // delta 単位 (例: "pp")。share 比較で「ポイント差」を明示
  deltaTitle?: string; // バッジ tooltip (既定「前期比」)
  color?: string; // taxonomy color (CSS color)。未指定は accent。
  href?: string;
  suffix?: ReactNode; // 信頼度構成・確度の注記など
}

// 前期比バッジ (上昇=赤系/下降=緑系は CTI 文脈で誤解を生むため中立色)。
function DeltaBadge({ delta, suffix, title = "前期比" }: { delta: number; suffix?: string; title?: string }) {
  if (delta === 0) return null;
  const up = delta > 0;
  return (
    <span className="inline-flex items-center text-[11px] tnum text-fg-subtle w-12 shrink-0" title={title}>
      {up ? <ArrowUp className="h-2.5 w-2.5" aria-hidden /> : <ArrowDown className="h-2.5 w-2.5" aria-hidden />}
      {Math.abs(delta)}{suffix}
    </span>
  );
}

interface RankBarsProps {
  items: RankRow[];
  max?: number; // 共通スケール用 (未指定は items の最大)
  emptyLabel?: string;
  labelWidth?: string; // tailwind 幅クラス (既定 w-28)
  // モバイル「狭く縦長」モード: ラベル+数値を行上に出しバーをフル幅下に。
  // 1 行に詰め込むとバーが潰れるため 2 行化し、生件数 suffix は省略。
  compact?: boolean;
}

const DEFAULT_BAR = "rgba(107,136,255,0.6)"; // accent/60

export function RankBars({ items, max, emptyLabel = "該当なし", labelWidth = "w-28", compact }: RankBarsProps) {
  if (items.length === 0) {
    return <div className="text-fg-subtle text-sm italic">{emptyLabel}</div>;
  }
  const m = max ?? Math.max(1, ...items.map((i) => i.value));

  if (compact) {
    return (
      <div className="space-y-2">
        {items.map((it, i) => {
          const pct = Math.max(2, (it.value / m) * 100);
          return (
            <div key={`${it.label}-${i}`} className="space-y-1">
              <div className="flex items-baseline gap-2 text-[12px]">
                {it.href ? (
                  <a href={it.href} className="min-w-0 flex-1 truncate text-fg-muted hover:text-accent" title={it.label}>{it.label}</a>
                ) : (
                  <span className="min-w-0 flex-1 truncate text-fg-muted" title={it.label}>{it.label}</span>
                )}
                <span className="tnum text-fg shrink-0">{it.valueLabel ?? it.value.toLocaleString()}</span>
                {it.delta != null && <DeltaBadge delta={it.delta} suffix={it.deltaSuffix} title={it.deltaTitle} />}
              </div>
              <div className="h-2.5 bg-surface-3 rounded-sm overflow-hidden">
                <div className="h-full rounded-sm" style={{ width: `${pct}%`, background: it.color ?? DEFAULT_BAR }} />
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {items.map((it, i) => {
        const pct = Math.max(2, (it.value / m) * 100);
        const label = it.href ? (
          <a href={it.href} className={`${labelWidth} shrink-0 truncate text-fg-muted hover:text-accent hover:underline`} title={it.label}>
            {it.label}
          </a>
        ) : (
          <span className={`${labelWidth} shrink-0 truncate text-fg-muted`} title={it.label}>
            {it.label}
          </span>
        );
        return (
          <div key={`${it.label}-${i}`} className="flex items-center gap-2.5 text-[12px]">
            {label}
            <div className="flex-1 h-3 bg-surface-3 rounded-sm overflow-hidden">
              <div className="h-full rounded-sm" style={{ width: `${pct}%`, background: it.color ?? DEFAULT_BAR }} />
            </div>
            <span className="tnum text-fg w-10 text-right shrink-0">{it.valueLabel ?? it.value.toLocaleString()}</span>
            {it.delta != null && <DeltaBadge delta={it.delta} suffix={it.deltaSuffix} title={it.deltaTitle} />}
            {it.suffix}
          </div>
        );
      })}
    </div>
  );
}
