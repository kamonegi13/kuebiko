// 汎用 sparkline (ミニ棒グラフ)。ForecastTab / ThreatsTab の手書き SVG を統合・再利用。
// 色は currentColor 駆動 (親が text-accent 等で制御)。最終バーは強調。

interface SparklineProps {
  data: number[];
  width?: number;
  height?: number;
  // 最終バーを不透明 + 強調色で描く (最新の山を目立たせる)。
  highlightLast?: boolean;
  className?: string;
}

export function Sparkline({
  data,
  width = 72,
  height = 20,
  highlightLast = false,
  className = "",
}: SparklineProps) {
  if (data.length === 0) return null;
  const max = Math.max(1, ...data);
  const n = data.length;
  const gap = 1;
  const bw = Math.max(0.5, (width - gap * (n - 1)) / n);
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={className}
      aria-hidden
    >
      {data.map((v, i) => {
        const h = Math.max(1, (v / max) * height);
        const x = i * (bw + gap);
        const isLast = i === n - 1;
        return (
          <rect
            key={i}
            x={x}
            y={height - h}
            width={bw}
            height={h}
            rx={0.5}
            fill="currentColor"
            opacity={isLast && highlightLast ? 1 : isLast ? 0.9 : 0.5}
          />
        );
      })}
    </svg>
  );
}
