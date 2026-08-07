// ホスト復旧 watchdog (OrbStack 無応答の自動復旧) の状態表示 + ON/OFF トグル。
//
// なぜ UI に出すのか: 監視の活動が見えないと「正常に見張っている」と「止まっている」を
// 区別できない。それはこのツールがずっと直してきた「沈黙の意味を決められない」状態
// そのものなので、watchdog 自身も可視化する。
//
// 制御の分担 (モバイル公開カードと同じ作法):
//   - 導入/削除: ターミナル (UI はコンテナ内なので launchctl を操作できない)
//   - 有効/無効: このトグル → data/ のフラグファイル → ホスト側が毎回読む
// macOS 固有のため、Linux サーバへ移した場合は導入しなければ何も動かない (installed=false)。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HeartPulse, Loader2 } from "lucide-react";
import { useState } from "react";
import { pagesApi } from "../api/pages";
import { formatJst } from "../utils/date";

const STATUS_META: Record<string, { text: string; cls: string }> = {
  healthy: { text: "正常", cls: "bg-success/15 text-success" },
  degraded: { text: "無応答を検知中", cls: "bg-warning/15 text-warning" },
  recovered: { text: "復旧しました", cls: "bg-accent-subtle text-accent" },
  recovery_failed: { text: "復旧に失敗", cls: "bg-critical/15 text-critical" },
  idle: { text: "待機中", cls: "bg-surface-3 text-fg-muted" },
};

export function HostWatchdogCard({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  const [showLog, setShowLog] = useState(false);
  const { data } = useQuery({
    queryKey: ["host-watchdog"],
    queryFn: pagesApi.hostWatchdogStatus,
    refetchInterval: 60_000,
  });

  const toggleMut = useMutation({
    mutationFn: (enable: boolean) =>
      enable ? pagesApi.hostWatchdogEnable() : pagesApi.hostWatchdogDisable(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["host-watchdog"] }),
  });

  const enabled = data?.enabled ?? false;
  const installed = data?.installed ?? false;
  const meta = STATUS_META[data?.status ?? ""] ?? null;

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <HeartPulse className="h-4 w-4 text-fg-muted" />
          <h3 className="m-0 text-sm font-semibold text-fg">ホスト復旧 watchdog</h3>
          {enabled && meta && (
            <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${meta.cls}`}>
              {meta.text}
            </span>
          )}
        </div>
        {!readOnly && installed && (
          <button
            onClick={() => toggleMut.mutate(!enabled)}
            disabled={toggleMut.isPending}
            className={[
              "rounded px-2.5 py-1 text-xs font-semibold inline-flex items-center gap-1.5",
              "border disabled:opacity-40",
              enabled
                ? "bg-surface-2 border-border-subtle text-fg-muted hover:bg-surface-3"
                : "bg-accent-subtle border-accent/40 text-accent hover:bg-accent/15",
            ].join(" ")}
          >
            {toggleMut.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {enabled ? "無効にする" : "有効にする"}
          </button>
        )}
      </div>

      <p className="m-0 text-[11px] text-fg-subtle leading-relaxed">
        スリープからの復帰に失敗して Docker が固まったとき、自動で復旧します。スリープ自体は
        止めません (収集は復帰後の追い付きで自己回復し、遅れるのは配信時刻だけです)。
      </p>

      {!installed && (
        <div className="bg-surface-2 rounded p-2.5 text-[11px] text-fg-muted space-y-1">
          <div className="text-fg">未導入です (この端末では動いていません)。</div>
          <div>
            導入するにはターミナルで次を実行してください (UI はコンテナ内のため
            LaunchAgent を登録できません):
          </div>
          <code className="block bg-surface-3 rounded px-2 py-1 font-mono text-[10.5px] break-all">
            bash scripts/install_orbstack_watchdog_launchagent.sh
          </code>
          <div>macOS 専用です。Linux サーバでは導入不要 (何も動きません)。</div>
        </div>
      )}

      {installed && (
        <div className="space-y-1.5 text-[11px]">
          <Row label="最終チェック">
            {data?.checked_at ? formatJst(data.checked_at) : "—"}
          </Row>
          {(data?.consecutive_failures ?? 0) > 0 && (
            <Row label="連続無応答">
              <span className="text-warning font-semibold">{data?.consecutive_failures} 回</span>
            </Row>
          )}
          {data?.last_recovery_at && (
            <Row label="最後の復旧">
              {formatJst(data.last_recovery_at)}
              <span className="text-fg-subtle ml-1.5">{data.last_recovery_result}</span>
            </Row>
          )}
          {data?.detail && <div className="text-fg-subtle">{data.detail}</div>}
          {!enabled && (
            <div className="text-fg-subtle">
              無効中です (導入済みですが監視していません)。
            </div>
          )}
          {(data?.log_tail?.length ?? 0) > 0 && (
            <button
              onClick={() => setShowLog((v) => !v)}
              className="text-accent hover:underline text-[11px]"
            >
              {showLog ? "履歴を隠す" : `履歴を表示 (${data?.log_tail.length} 行)`}
            </button>
          )}
          {showLog && (
            <pre className="bg-surface-2 rounded p-2 text-[10px] font-mono text-fg-muted overflow-x-auto max-h-48">
              {data?.log_tail.join("\n")}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2">
      <span className="text-fg-subtle w-24 shrink-0">{label}</span>
      <span className="text-fg min-w-0">{children}</span>
    </div>
  );
}

// ジョブ管理向けの診断バナー (2026-08-02)。
//
// 設定そのものは「設定 → 接続」に一本化した。ここに出すのは **タイムラインの穴を
// 説明する診断情報**だけ — ジョブが動かなかった時間帯を見たときに、その場で
// 「Docker が固まっていた / 自動復旧した」と分かることに価値がある。
// 正常時は何も描画しない (常設カードにすると本当に見るべき異常が埋もれる)。
export function HostWatchdogBanner() {
  const { data } = useQuery({
    queryKey: ["host-watchdog"],
    queryFn: pagesApi.hostWatchdogStatus,
    refetchInterval: 60_000,
  });

  if (!data?.installed || !data.enabled) return null;
  const abnormal =
    data.status === "degraded" || data.status === "recovery_failed" || data.status === "recovered";
  if (!abnormal) return null;

  const critical = data.status === "recovery_failed";
  const text =
    data.status === "degraded"
      ? `Docker が無応答です (${data.consecutive_failures} 回連続)。閾値に達すると自動復旧します — この間ジョブは実行されません。`
      : critical
        ? `ホストの自動復旧に失敗しました。手動確認が必要です (${data.detail})。`
        : `Docker 無応答から自動復旧しました (${formatJst(data.last_recovery_at)})。この時間帯のジョブは次回周期で追い付きます。`;

  return (
    <div
      className={[
        "rounded-md px-4 py-3 text-sm flex items-start gap-2 border",
        critical
          ? "bg-critical-soft border-critical text-critical"
          : "bg-warning-soft border-warning/50 text-warning",
      ].join(" ")}
    >
      <HeartPulse className="h-4 w-4 shrink-0 mt-0.5" />
      <span>
        {text}
        <a href="/app/config#connections" className="underline ml-1.5">
          設定を開く
        </a>
      </span>
    </div>
  );
}
