// 地図下の日次件数推移 (stacked bar)。チャートlib非依存の div ベース (レスポンシブ + 軽量)。
// 国別/セクター別の上位系列 + その他を日ごとに積み上げ、合成と推移を同時に見せる。

import type { TrendResponse } from "../../api/geo";
import { sectorColor } from "./sectorColors";

// 国別系列の色パレット (セクターは sectorColor を使う)。
const COUNTRY_PALETTE = [
  "#6b88ff", "#f5a623", "#4ecdc4", "#e06b6b", "#a78bfa", "#7bc47f",
];
const OTHER_COLOR = "#4a5260";

function colorFor(groupBy: "country" | "sector", key: string, idx: number): string {
  if (key === "_other") return OTHER_COLOR;
  if (groupBy === "sector") return sectorColor(key);
  return COUNTRY_PALETTE[idx % COUNTRY_PALETTE.length];
}

function dayLabel(iso: string): string {
  // "2026-06-19" → "6/19"
  const [, m, d] = iso.split("-");
  return `${Number(m)}/${Number(d)}`;
}

// 中心移動平均で日次の疎なギザつきを平滑化 (折れ線用)。radius=3 で 7 日窓。端は利用可能分で平均。
function movingAvg(counts: number[], radius = 3): number[] {
  return counts.map((_, i) => {
    let sum = 0;
    let n = 0;
    for (let j = Math.max(0, i - radius); j <= Math.min(counts.length - 1, i + radius); j++) {
      sum += counts[j];
      n += 1;
    }
    return n ? sum / n : 0;
  });
}

interface TrendChartProps {
  data: TrendResponse;
  // line=系列別トレンド (既定) / stacked=総量+構成
  mode?: "line" | "stacked";
  height?: number;
  // 凡例で非表示にした系列キー (永続化のため親が保持)、トグルは親へ委譲。
  hiddenKeys?: string[];
  onToggleSeries?: (key: string) => void;
  // 折れ線を 7 日移動平均で平滑化 (疎な日次のギザつきを抑える)。
  smooth?: boolean;
}

export function TrendChart({
  data,
  mode = "line",
  height = 84,
  hiddenKeys = [],
  onToggleSeries,
  smooth = false,
}: TrendChartProps) {
  const { buckets, series, group_by } = data;
  // 凡例クリックで系列を表示/非表示。色は元の系列順で固定 (可視性で色がずれないように)。
  const hidden = new Set(hiddenKeys);
  const colorByKey = new Map(series.map((s, idx) => [s.key, colorFor(group_by, s.key, idx)]));
  const toggle = (key: string) => onToggleSeries?.(key);

  if (buckets.length === 0 || series.length === 0) {
    return <div className="px-3 py-6 text-center text-xs text-fg-subtle">期間内のデータがありません</div>;
  }

  const vis = series.filter((s) => !hidden.has(s.key));
  const totals = buckets.map((_, i) => vis.reduce((s, ser) => s + (ser.counts[i] ?? 0), 0));
  const max = Math.max(1, ...totals);
  // 折れ線の y スケールは単一系列の **生値** の最大 (積み上げ総量ではない)。平滑線 (移動平均) は
  // 生値ピーク以下に来るので生値基準で OK (株価チャートの MA が価格ピーク以下に来るのと同じ)。
  const lineMax = Math.max(1, ...vis.flatMap((s) => s.counts));
  const lastX = Math.max(1, buckets.length - 1);
  // x 軸ラベルは過密回避で最大 ~8 本に間引く。
  const labelStep = Math.max(1, Math.ceil(buckets.length / 8));

  return (
    <div className="flex flex-col gap-1">
      {/* 凡例 (クリックで表示/非表示) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-1 text-[11px] text-fg-muted">
        {series.map((ser) => {
          const off = hidden.has(ser.key);
          return (
            <button
              key={ser.key}
              onClick={() => toggle(ser.key)}
              className={`inline-flex items-center gap-1 ${off ? "opacity-35 line-through" : "hover:text-fg"}`}
              title={off ? "クリックで表示" : "クリックで非表示"}
            >
              <span
                className="inline-block h-2 w-2 rounded-sm"
                style={{ background: colorByKey.get(ser.key) }}
              />
              {ser.label}
              <span className="text-fg-subtle">{ser.counts.reduce((a, b) => a + b, 0)}</span>
            </button>
          );
        })}
      </div>

      {/* 折れ線 (系列別トレンド): viewBox 0..100 を幅にフィット、線幅は非スケール */}
      {mode === "line" && (
        <svg
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          width="100%"
          height={height}
          style={{ display: "block", overflow: "visible" }}
        >
          {vis.map((ser) => {
            const isOther = ser.key === "_other";
            const color = colorByKey.get(ser.key);
            const toPts = (vals: number[]) =>
              vals.map((c, i) => `${(i / lastX) * 100},${100 - (c / lineMax) * 100}`).join(" ");
            // 生の推移を主線 (実線・常に主役) にし、平滑線 (7日移動平均) は **控えめな点線** で
            // トレンドの当たりだけ重ねる。smooth OFF 時は生線のみ。
            return (
              <g key={ser.key}>
                <polyline
                  points={toPts(ser.counts)}
                  fill="none"
                  stroke={color}
                  strokeWidth={1.5}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  vectorEffect="non-scaling-stroke"
                  opacity={isOther ? 0.45 : 0.95}
                >
                  <title>{ser.label}</title>
                </polyline>
                {smooth && (
                  <polyline
                    points={toPts(movingAvg(ser.counts))}
                    fill="none"
                    stroke={color}
                    strokeWidth={1.2}
                    strokeDasharray="1 2.5"
                    strokeLinejoin="round"
                    strokeLinecap="round"
                    vectorEffect="non-scaling-stroke"
                    opacity={isOther ? 0.3 : 0.55}
                  >
                    <title>{ser.label} (7日ならし)</title>
                  </polyline>
                )}
              </g>
            );
          })}
        </svg>
      )}

      {/* 積み上げ棒 (総量 + 構成) */}
      {mode === "stacked" && (
      <div className="flex items-end gap-px" style={{ height }}>
        {buckets.map((b, i) => {
          const total = totals[i];
          const breakdown = vis
            .filter((s) => (s.counts[i] ?? 0) > 0)
            .map((s) => `${s.label}: ${s.counts[i]}`)
            .join("\n");
          return (
            <div
              key={b}
              className="group relative flex flex-1 flex-col-reverse"
              style={{ height: "100%" }}
              title={`${b}  計 ${total}\n${breakdown}`}
            >
              {vis.map((ser) => {
                const c = ser.counts[i] ?? 0;
                if (c === 0) return null;
                return (
                  <div
                    key={ser.key}
                    style={{
                      height: `${(c / max) * 100}%`,
                      background: colorByKey.get(ser.key),
                    }}
                    className="w-full first:rounded-t-sm"
                  />
                );
              })}
            </div>
          );
        })}
      </div>
      )}

      {/* x 軸日付ラベル (間引き) */}
      <div className="flex gap-px px-0 text-[10px] text-fg-subtle">
        {buckets.map((b, i) => (
          <div key={b} className="flex-1 text-center">
            {i % labelStep === 0 ? dayLabel(b) : ""}
          </div>
        ))}
      </div>
    </div>
  );
}
