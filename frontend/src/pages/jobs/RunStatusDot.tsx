// 直近 last_run 状態の小さなドット。CronMarker / IntervalRow / ReactiveZone で共有し、
// タイムライン上で「どのジョブが直近エラー / 実行中か」を一目で分かるようにする。
// failed = critical 塗り + AlertTriangle、running = accent の pulse、ok/none = 非表示 (ノイズ削減)。

import { AlertTriangle } from "lucide-react";
import { runHealth, runHealthColor } from "./categories";

export interface RunStatusDotProps {
  status?: string | null;
  // 絶対配置クラス (ピル右上角など)。チップ inline 用途では省略。
  positionClass?: string;
}

export function RunStatusDot({ status, positionClass }: RunStatusDotProps) {
  const health = runHealth(status);
  if (health !== "failed" && health !== "running") return null;
  const color = runHealthColor(health);
  const base = positionClass ?? "relative inline-flex";

  if (health === "failed") {
    return (
      <span
        className={`${base} items-center justify-center`}
        title="前回: 失敗"
        aria-label="前回失敗"
      >
        <span className={`w-2 h-2 rounded-full ${color.dot} inline-flex items-center justify-center`}>
          <AlertTriangle className="h-1.5 w-1.5 text-bg" aria-hidden />
        </span>
      </span>
    );
  }
  // running
  return (
    <span className={`${base} items-center justify-center`} title="実行中" aria-label="実行中">
      <span className={`w-2 h-2 rounded-full ${color.dot} animate-pulse`} />
    </span>
  );
}
