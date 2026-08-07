// 汎用 loading spinner と LLM 推論用 progress bar。
// 「フリーズか動いているか分からない」UX 問題への対処 (背景にある all wizards / panels で利用)。

import { useEffect, useState } from "react";

// ---------- 小型 ring spinner (button 内 / inline) ----------

interface SpinnerProps {
  size?: "xs" | "sm" | "md";
  className?: string;
}

export function Spinner({ size = "sm", className = "" }: SpinnerProps) {
  const dim = size === "xs" ? "w-3 h-3" : size === "sm" ? "w-3.5 h-3.5" : "w-5 h-5";
  return (
    <span
      role="status"
      aria-label="読み込み中"
      className={`inline-block ${dim} border-2 border-current border-r-transparent rounded-full animate-spin ${className}`}
    />
  );
}

// ---------- button 内 text + spinner inline ----------

interface ButtonSpinnerProps {
  label: string;
  size?: "xs" | "sm" | "md";
}

export function ButtonSpinner({ label, size = "sm" }: ButtonSpinnerProps) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <Spinner size={size} />
      <span>{label}</span>
    </span>
  );
}

// ---------- 長 wait 用 indeterminate bar + elapsed counter ----------
// 推測の estimated time は出さない: ループする gradient bar で「動いてる」のみを示す。
// elapsed は単なる経過秒の transparent な参考値。

interface LLMProgressProps {
  /** 表示 label (例: "gemma4:31b think=True で selector 提案中") */
  label: string;
  /** active=false にすると counter 停止 (mutation 終了時) */
  active?: boolean;
}

export function LLMProgress({ label, active = true }: LLMProgressProps) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!active) return;
    setElapsed(0);
    const start = Date.now();
    const i = setInterval(() => {
      setElapsed((Date.now() - start) / 1000);
    }, 100);
    return () => clearInterval(i);
  }, [active]);

  return (
    <div className="bg-surface-2 border border-accent/30 rounded p-3 space-y-2">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="inline-flex items-center gap-1.5 text-fg">
          <Spinner size="xs" />
          <span>{label}</span>
        </span>
        <span className="tnum font-mono text-fg-subtle">{elapsed.toFixed(1)}s</span>
      </div>
      {/* indeterminate sliding gradient (index.css の .progress-bar 既存定義を利用) */}
      <div className="w-full bg-surface-3 rounded-full h-1.5 overflow-hidden">
        <div className="progress-bar h-full w-full" />
      </div>
    </div>
  );
}

// ---------- 短時間 fetch 用 pulse skeleton (リスト / panel 内コンテンツ) ----------

export function PulseSkeleton({ lines = 3, className = "" }: { lines?: number; className?: string }) {
  return (
    <div className={`space-y-2 ${className}`}>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="h-3 bg-surface-2 rounded animate-pulse"
          style={{ width: `${85 - i * 8}%` }}
        />
      ))}
    </div>
  );
}
