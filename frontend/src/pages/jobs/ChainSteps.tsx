// 毎時チェーンの段の一覧 (詳細パネル内)。段は時刻を持たずタイムラインに出ないため、
// チェーンを選んだときにここで順番・状態・実行中の段を見せ、クリックで段の詳細へ移る。

import { ArrowLeft, Link2 } from "lucide-react";
import type { JobView } from "../../api/jobs";
import { relativeFromNow } from "../../utils/date";
import { jobHealth, runHealthColor } from "./categories";

export interface ChainStepsProps {
  chain: JobView;
  allJobs: JobView[];
  onSelectJob: (id: string) => void;
}

export function ChainSteps({ chain, allJobs, onSelectJob }: ChainStepsProps) {
  const byId = new Map(allJobs.map((j) => [j.id, j] as const));
  const steps = chain.steps ?? [];
  return (
    <div className="border border-border-subtle rounded-md p-2.5 space-y-1">
      <div className="text-[12px] uppercase tracking-wider text-fg-subtle font-semibold">
        段 (上から順に直列実行)
      </div>
      <ol className="m-0 p-0 list-none space-y-0.5">
        {steps.map((sid, i) => {
          const step = byId.get(sid);
          const health = step ? jobHealth(step) : "none";
          const color = runHealthColor(health);
          const isCurrent = chain.running_step === sid;
          return (
            <li key={sid}>
              <button
                type="button"
                onClick={() => onSelectJob(sid)}
                className={`w-full text-left flex items-center gap-2 px-2 py-1 rounded text-[13px] hover:bg-surface-2 ${isCurrent ? "bg-accent-subtle" : ""}`}
              >
                <span className="font-mono text-fg-subtle w-5 shrink-0">{i + 1}.</span>
                <span className={`w-2 h-2 rounded-full shrink-0 ${color.dot} ${health === "running" ? "animate-pulse" : ""}`} aria-hidden />
                <span className="text-fg truncate flex-1">{step?.title ?? sid}</span>
                <span className={`shrink-0 ${color.text}`}>
                  {health === "running"
                    ? "実行中"
                    : step?.last_run
                      ? relativeFromNow(step.last_run.last_run_at)
                      : "未実行"}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// 段を選んだときの案内。時刻・ON/OFF はチェーンが持つことを示し、チェーンへ戻る導線を置く。
export function ChainStepNotice({ job, onSelectJob }: { job: JobView; onSelectJob: (id: string) => void }) {
  if (!job.chain_id) return null;
  const chainId = job.chain_id;
  return (
    <div className="bg-surface-2 border border-border-subtle rounded px-2.5 py-1.5 text-[13px] text-fg-muted flex items-start gap-1.5">
      <Link2 className="h-3.5 w-3.5 shrink-0 mt-0.5" aria-hidden />
      <span className="flex-1">
        「{job.chain_title ?? chainId}」の段です。時刻と ON/OFF はチェーンで設定します
        (段を単独で有効にすると二重に実行されます)。手動実行はここからできます。
      </span>
      <button
        type="button"
        onClick={() => onSelectJob(chainId)}
        className="shrink-0 inline-flex items-center gap-1 text-accent hover:underline"
      >
        <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> チェーンへ
      </button>
    </div>
  );
}
