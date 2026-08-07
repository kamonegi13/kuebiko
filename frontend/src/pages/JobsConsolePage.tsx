// 統一バックグラウンドジョブ運用コンソール — マスター詳細型。
//
// 上に 24h タイムライン (全ジョブを 1 軸に集約) を全幅で固定し、その直下に「選択中ジョブの
// 詳細/編集パネル」を 1 枚据える。旧版のカテゴリ別カード縦積みは廃止。クリック=選択→直下パネルが
// その場で更新 (scrollIntoView なし = ページを飛ばさない)。ドラッグは時刻変更のみ (選択を誘発しない)。
//
// ガードレール (中核要件・維持):
//   - 重要ジョブ停止中の常設警告バナー (disabled_important)
//   - scheduler 停止バナー
//   - critical OFF は影響 (disable_impact) を出す確認モーダル → confirm:true / important OFF も確認 / optional 即時
//   - danger_note は非ブロッキングの amber inline 警告 (詳細パネル内)
//   - timeline の drag は 5 分 snap → pending (適用/取消) を経て確定 (fat-finger 防止)

import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, Check, CheckCircle2, Clock, Loader2, PauseCircle } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pageContainer, PageHeader } from "../components/Page";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import {
  listJobs, rescheduleJob, toggleJob,
  type JobView,
} from "../api/jobs";
import { runHealth } from "./jobs/categories";
import { JobTimeline } from "./jobs/JobTimeline";
import { JobDetailPanel } from "./jobs/JobDetailPanel";
import { HostWatchdogBanner } from "../components/HostWatchdogCard";

const JOBS_QUERY_KEY = ["jobs-console"] as const;

// ---- confirmation modal (portal, Drawer と同じ overlay idiom) -----------
interface ConfirmState {
  job: JobView;
  tone: "critical" | "important";
}

function ConfirmModal({ state, pending, onCancel, onConfirm }: {
  state: ConfirmState;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { job, tone } = state;
  const isCritical = tone === "critical";
  const accentBorder = isCritical ? "border-critical" : "border-warning";
  const accentText = isCritical ? "text-critical" : "text-warning";
  const confirmBtn = isCritical
    ? "bg-critical text-bg hover:bg-critical/90"
    : "bg-warning text-bg hover:bg-warning/90";

  return createPortal(
    <div role="dialog" aria-modal="true" aria-label="ジョブ停止の確認" className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div onClick={onCancel} className="absolute inset-0 bg-black/50 backdrop-blur-sm animate-fade-in" />
      <div className={`relative w-full max-w-md bg-bg border ${accentBorder} rounded-lg shadow-2xl animate-fade-in`}>
        <div className={`flex items-center gap-2 px-4 py-3 border-b ${accentBorder}/40`}>
          <AlertTriangle className={`h-4 w-4 shrink-0 ${accentText}`} />
          <h3 className={`m-0 text-sm font-bold ${accentText}`}>
            {isCritical ? "最重要ジョブを停止しますか?" : "重要ジョブを停止しますか?"}
          </h3>
        </div>
        <div className="p-4 space-y-3">
          <div className="text-sm text-fg font-semibold">{job.title}</div>
          <div className="bg-surface-2 border border-border-subtle rounded-md px-3 py-2 text-xs text-fg-muted leading-relaxed">
            <span className="text-fg-subtle uppercase text-[10px] font-semibold tracking-wider block mb-1">停止による影響</span>
            {job.disable_impact || "影響情報なし"}
          </div>
          <div className="flex items-center justify-end gap-2 pt-1">
            <button onClick={onCancel} disabled={pending} className="bg-surface-2 border border-border-subtle text-fg-muted px-3 py-1.5 rounded text-xs font-semibold hover:text-fg disabled:opacity-50">
              キャンセル
            </button>
            <button onClick={onConfirm} disabled={pending} className={`px-3 py-1.5 rounded text-xs font-semibold inline-flex items-center gap-1.5 disabled:opacity-50 ${confirmBtn}`}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              停止する
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// 次回実行が最も近い cron ジョブ (初期選択の既定)。
function soonestCronId(jobs: JobView[]): string | null {
  let best: string | null = null;
  let bestT = Infinity;
  for (const j of jobs) {
    if (j.schedule_type !== "cron" || !j.next_run_at) continue;
    const t = new Date(j.next_run_at).getTime();
    if (!Number.isNaN(t) && t < bestT) { bestT = t; best = j.id; }
  }
  return best ?? (jobs[0]?.id ?? null);
}

// 艦隊ヘルス集計: 停止 (P=!enabled) を最優先分類し、有効な中で running/failed/ok を数える。
// 軸に出ない reactive や off-axis も含め全 jobs を走査する。
interface FleetHealth {
  ok: number;
  failed: number;
  running: number;
  stopped: number;
  failedJobs: JobView[]; // バナー用 (直近エラーの enabled ジョブ)
}

function computeFleetHealth(jobs: JobView[]): FleetHealth {
  let ok = 0, failed = 0, running = 0, stopped = 0;
  const failedJobs: JobView[] = [];
  for (const j of jobs) {
    if (!j.enabled) { stopped++; continue; }
    const h = runHealth(j.last_run?.status);
    if (h === "failed") { failed++; failedJobs.push(j); }
    else if (h === "running") running++;
    else ok++; // ok / none (未実行含む) は「正常側」として数える
  }
  return { ok, failed, running, stopped, failedJobs };
}

// ヘッダの艦隊ヘルス要約 (正常 / エラー / 実行中 / 停止)。表示のみ (クリックフィルタ無し)。
function FleetHealthSummary({ health }: { health: FleetHealth }) {
  return (
    <span className="inline-flex items-center gap-2.5 text-xs">
      <span className="inline-flex items-center gap-1 text-success" title="正常 (直近成功 / 未実行)">
        <CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> {health.ok}
      </span>
      <span className={`inline-flex items-center gap-1 ${health.failed > 0 ? "text-critical" : "text-fg-subtle"}`} title="直近エラー">
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden /> {health.failed}
      </span>
      <span className={`inline-flex items-center gap-1 ${health.running > 0 ? "text-accent" : "text-fg-subtle"}`} title="実行中">
        <Loader2 className={`h-3.5 w-3.5 ${health.running > 0 ? "animate-spin" : ""}`} aria-hidden /> {health.running}
      </span>
      <span className={`inline-flex items-center gap-1 ${health.stopped > 0 ? "text-warning" : "text-fg-subtle"}`} title="停止中 (無効)">
        <PauseCircle className="h-3.5 w-3.5" aria-hidden /> {health.stopped}
      </span>
    </span>
  );
}

// ---- page ---------------------------------------------------------------
export function JobsConsolePage() {
  const qc = useQueryClient();
  const flags = useRuntimeFlags();
  const read_only = flags.read_only;
  // Tier1 (Cloudflare Access 認証済み): 公開 instance でも即時実行だけは可能。
  // 実行主体は full instance (proxy) なので、こちらの scheduler 不在は関係ない。
  const canRun = !read_only || flags.authenticated;
  const { data, isLoading, isError, error } = useQuery({
    queryKey: JOBS_QUERY_KEY,
    queryFn: () => listJobs(),
    refetchInterval: 10_000,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const disabledImportantTitles = useMemo(() => {
    if (!data) return [];
    const byId = new Map(data.jobs.map((j) => [j.id, j.title] as const));
    return data.disabled_important.map((id) => byId.get(id) ?? id);
  }, [data]);

  const fleetHealth = useMemo(() => (data ? computeFleetHealth(data.jobs) : null), [data]);

  // 選択の解決: 明示選択 → 既定 (soonest cron)。選択 id が消えても既定に戻す。
  const resolvedSelectedId = useMemo(() => {
    if (!data) return null;
    if (selectedJobId && data.jobs.some((j) => j.id === selectedJobId)) return selectedJobId;
    return soonestCronId(data.jobs);
  }, [data, selectedJobId]);

  const selectedJob = useMemo(
    () => data?.jobs.find((j) => j.id === resolvedSelectedId) ?? null,
    [data, resolvedSelectedId],
  );

  const toggleMut = useMutation({
    mutationFn: ({ id, enabled, confirm }: { id: string; enabled: boolean; confirm?: boolean }) =>
      toggleJob(id, enabled, confirm),
    onSuccess: () => { invalidate(); setConfirmState(null); },
  });

  // timeline drag→適用: hour/minute のみ変更。
  const rescheduleMut = useMutation({
    mutationFn: ({ id, hour, minute }: { id: string; hour: number; minute: number }) =>
      rescheduleJob(id, { schedule_type: "cron", hour, minute }),
    onSuccess: () => invalidate(),
  });

  // toggle 要求: OFF かつ critical/important は確認 gate。それ以外は即時。
  const requestToggle = (job: JobView, nextEnabled: boolean) => {
    if (nextEnabled) { toggleMut.mutate({ id: job.id, enabled: true }); return; }
    if (job.protection === "critical") { setConfirmState({ job, tone: "critical" }); return; }
    if (job.protection === "important") { setConfirmState({ job, tone: "important" }); return; }
    toggleMut.mutate({ id: job.id, enabled: false });
  };

  const confirmToggleOff = () => {
    if (!confirmState) return;
    toggleMut.mutate({ id: confirmState.job.id, enabled: false, confirm: true });
  };

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <PageHeader
        title={read_only ? "ジョブ管理 (閲覧専用)" : "ジョブ管理"}
        subtitle={
          read_only
            ? canRun
              ? "認証済み。即時実行のみ可能です (ON/OFF・再スケジュールはローカル画面から)。"
              : "閲覧専用モード。ON/OFF・再スケジュール・即時実行は無効化されています。"
            : "全バックグラウンドジョブを 24h タイムラインで一望。マーカーをクリックすると直下パネルで詳細確認・編集、ドラッグで時刻変更できます。重要ジョブの停止はガードで保護されています。"
        }
        actions={
          <div className="flex items-center gap-3 flex-wrap justify-end">
            {fleetHealth && <FleetHealthSummary health={fleetHealth} />}
            <span className="text-fg-subtle text-xs inline-flex items-center gap-1.5">
              <Clock className="h-3.5 w-3.5" />
              {data ? `${data.jobs.length} ジョブ · 10s 自動更新` : "読み込み中"}
            </span>
          </div>
        }
      />

      {/* ホスト側の異常のみ診断表示 (設定は 設定 → 接続)。タイムラインの穴の理由がその場で分かる */}
      <HostWatchdogBanner />

      {data && !data.scheduler_available && !canRun && (
        <div className="bg-warning-soft border border-warning rounded-md px-4 py-3 text-warning text-sm flex items-center gap-1.5">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          スケジューラが停止中です (閲覧専用画面、またはスケジューラ本体が停止)。次回実行予定の表示・即時実行は利用できません。
        </div>
      )}

      {disabledImportantTitles.length > 0 && (
        <div className="bg-critical-soft border border-critical rounded-md px-4 py-3 text-critical text-sm flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <span>
            <strong>{disabledImportantTitles.length} 個の重要ジョブが停止中:</strong>{" "}
            {disabledImportantTitles.join(", ")}
          </span>
        </div>
      )}

      {/* 直近エラー バナー (disabled_important とは別観点。両方出るなら縦に並ぶ) */}
      {fleetHealth && fleetHealth.failed > 0 && (
        <div className="bg-critical-soft border border-critical rounded-md px-4 py-3 text-critical text-sm flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <span>
            <strong>{fleetHealth.failed} ジョブが直近エラー:</strong>{" "}
            {fleetHealth.failedJobs.map((j) => j.title).join(", ")}
          </span>
        </div>
      )}

      {isError && (
        <div className="bg-critical-soft border border-critical rounded-md px-4 py-3 text-critical text-sm flex items-center gap-1.5">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          ジョブ一覧の取得に失敗しました: {error instanceof Error ? error.message : "unknown error"}
        </div>
      )}

      {isLoading && (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => <div key={i} className="h-24 bg-surface-2 animate-shimmer rounded-lg" />)}
        </div>
      )}

      {/* 上: 24h タイムライン (master) */}
      {data && (
        <JobTimeline
          jobs={data.jobs}
          readOnly={read_only}
          schedulerAvailable={data.scheduler_available}
          selectedJobId={resolvedSelectedId}
          onSelectJob={setSelectedJobId}
          onApplyReschedule={(job, hour, minute) => rescheduleMut.mutate({ id: job.id, hour, minute })}
        />
      )}

      {/* 直下: 選択中ジョブの詳細/編集 (detail、タイムライン直下の定位置)。
          選択を変えてもこの位置は動かない (scrollIntoView しない = ページを飛ばさない)。 */}
      {data && (
        <JobDetailPanel
          job={selectedJob}
          readOnly={read_only}
          canRun={canRun}
          runAvailable={read_only ? flags.authenticated : data.scheduler_available}
          togglePending={toggleMut.isPending && toggleMut.variables?.id === selectedJob?.id}
          onToggle={(next) => selectedJob && requestToggle(selectedJob, next)}
          onChanged={invalidate}
        />
      )}



      {confirmState && (
        <ConfirmModal
          state={confirmState}
          pending={toggleMut.isPending}
          onCancel={() => { setConfirmState(null); toggleMut.reset(); }}
          onConfirm={confirmToggleOff}
        />
      )}
    </div>
  );
}
