// Grok 統合カード (2026-08-15): 購読ソース画面の常設は 1 行サマリのみとし、
// 詳細 (IMAP / セッション / タスク定義) は他 feed と同じ Drawer 形式で開く。
// 3 カード横並びで画面が散らかる問題への対応 (利用者指摘)。

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Zap } from "lucide-react";
import { useState } from "react";
import { grokApi } from "../api/grok";
import { grokMailApi } from "../api/grokMail";
import { Drawer } from "./Drawer";
import { GrokMailCard } from "./GrokMailCard";
import { GrokSessionCard } from "./GrokSessionCard";
import { GrokTasksCard } from "./GrokTasksCard";

type DotLevel = "ok" | "warning" | "error";

function StatusDot({ level, label }: { level: DotLevel; label: string }) {
  const color =
    level === "ok" ? "bg-success" : level === "error" ? "bg-critical" : "bg-warning";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-fg-muted">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {label}
    </span>
  );
}

export function GrokHubCard({ readOnly }: { readOnly: boolean }) {
  const [open, setOpen] = useState(false);

  // 各詳細カードと同じ queryKey を使う (react-query がキャッシュを共有し二重取得しない)
  const mailHealth = useQuery({
    queryKey: ["grok-mail-health"],
    queryFn: grokMailApi.health,
    refetchInterval: 5 * 60_000,
  });
  const session = useQuery({
    queryKey: ["grok-session"],
    queryFn: () => grokApi.sessionStatus(),
    refetchInterval: 60_000,
  });
  const tasks = useQuery({
    queryKey: ["grok-tasks"],
    queryFn: () => grokApi.tasks(),
    enabled: !readOnly, // 公開面では denylist により 403
  });

  const mailLevel: DotLevel = mailHealth.data?.status ?? "warning";

  const s = session.data;
  const sessionExpired =
    s?.last_verify?.status === "session_expired" || s?.last_run?.session_expired === true;
  const sessionLevel: DotLevel =
    !s?.state?.exists || sessionExpired
      ? "error"
      : s?.last_verify?.status === "ok"
        ? "ok"
        : "warning";

  const lastRun = s?.last_run ?? null;
  const runLevel: DotLevel =
    lastRun == null ? "warning" : lastRun.status === "succeeded" ? "ok" : "error";

  const taskCount = tasks.data?.tasks.length ?? null;

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => e.key === "Enter" && setOpen(true)}
        className="rounded-lg border border-border-subtle p-3 flex items-center gap-4 flex-wrap cursor-pointer hover:bg-surface-2 transition-colors"
      >
        <span className="inline-flex items-center gap-2 font-bold text-fg text-sm">
          <Zap className="h-4 w-4 text-fg-muted" />
          Grok (X 経由収集)
        </span>
        <StatusDot level={mailLevel} label="メール受信" />
        <StatusDot level={sessionLevel} label="セッション" />
        <StatusDot
          level={runLevel}
          label={`直近実行${lastRun ? `: 取得 ${lastRun.total_fetched}` : ""}`}
        />
        {!readOnly && taskCount != null && (
          <span className="text-xs text-fg-muted">タスク {taskCount} 件 · Hourly</span>
        )}
        <span className="ml-auto inline-flex items-center gap-1 text-xs text-fg-subtle">
          詳細 <ChevronRight className="h-3.5 w-3.5" />
        </span>
      </div>

      <Drawer isOpen={open} onClose={() => setOpen(false)} title="Grok (X 経由収集)">
        <div className="space-y-3">
          <GrokMailCard readOnly={readOnly} />
          <GrokSessionCard />
          <GrokTasksCard readOnly={readOnly} />
        </div>
      </Drawer>
    </>
  );
}
