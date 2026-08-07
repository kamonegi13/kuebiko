// マスター詳細型の「詳細パネル」。タイムライン直下の定位置に 1 枚だけ据え、選択中ジョブを表示・編集する。
// 旧版のカテゴリ別カード縦積みを廃止し、ここに全ガードレール (toggle 確認 / danger_note /
// 5 分 snap の drag 確定は timeline 側) を集約。scrollIntoView は使わない (ページを飛ばさない)。

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, History, Loader2, Play } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { formatJst, formatJstShort, relativeFromNow } from "../../utils/date";
import {
  JobApiError, listJobRuns, rescheduleJob, runJobNow,
  type JobProtection, type JobRunRecord, type JobView, type SchedulePatch,
} from "../../api/jobs";
import { categoryForId, runHealth, runHealthColor } from "./categories";
import { vocabLabel } from "../../hooks/useVocab";

// ラベルは backend vocab ("job_kind" / "job_protection", vocabLabel) を SSoT に解決する。
// 色 (cls) は UI 固有のスタイルなのでローカルに保持。
const PROTECTION_CLS: Record<JobProtection, string> = {
  critical: "bg-critical-soft text-critical border border-critical/40",
  important: "bg-warning-soft text-warning border border-warning/40",
  optional: "bg-surface-3 text-fg-subtle border border-border-subtle",
};

const DOW_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "毎日" },
  { value: "mon", label: "月" },
  { value: "tue", label: "火" },
  { value: "wed", label: "水" },
  { value: "thu", label: "木" },
  { value: "fri", label: "金" },
  { value: "sat", label: "土" },
  { value: "sun", label: "日" },
];

export interface JobDetailPanelProps {
  job: JobView | null;
  readOnly: boolean;
  // 即時実行の可否。公開 instance でも認証済み (Tier1) なら full instance へ proxy されて
  // 実行できるため、readOnly (write 全般の可否) とは別軸で受け取る。
  canRun: boolean;
  runAvailable: boolean;
  togglePending: boolean;
  onToggle: (nextEnabled: boolean) => void; // 親の requestToggle (ガードレール込み)
  onChanged: () => void; // invalidate
}

export function JobDetailPanel({
  job, readOnly, canRun, runAvailable, togglePending, onToggle, onChanged,
}: JobDetailPanelProps) {
  if (!job) {
    return (
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-6 text-center text-sm text-fg-subtle">
        タイムラインのジョブを選択すると、ここで詳細確認とスケジュール編集ができます。
      </div>
    );
  }
  // job.id を key にして選択切替でフォーム state を作り直す (前ジョブの入力を持ち越さない)。
  return (
    <PanelBody
      key={job.id}
      job={job}
      readOnly={readOnly}
      canRun={canRun}
      runAvailable={runAvailable}
      togglePending={togglePending}
      onToggle={onToggle}
      onChanged={onChanged}
    />
  );
}

function PanelBody({ job, readOnly, canRun, runAvailable, togglePending, onToggle, onChanged }: {
  job: JobView;
  readOnly: boolean;
  canRun: boolean;
  runAvailable: boolean;
  togglePending: boolean;
  onToggle: (nextEnabled: boolean) => void;
  onChanged: () => void;
}) {
  const [flash, setFlash] = useState<string | null>(null);
  const showFlash = (m: string) => { setFlash(m); setTimeout(() => setFlash(null), 2500); };
  const isReactive = job.kind === "reactive";
  const Icon = categoryForId(job.id).icon;
  const protCls = PROTECTION_CLS[job.protection];

  const runMut = useMutation({
    mutationFn: () => runJobNow(job.id),
    onSuccess: () => { onChanged(); showFlash("即時実行を開始しました"); },
    onError: (e: unknown) => showFlash(e instanceof Error ? `失敗: ${e.message}` : "実行に失敗"),
  });

  const nextRun = job.next_run_at
    ? <span title={formatJst(job.next_run_at)}>{formatJstShort(job.next_run_at)} <span className="text-fg-subtle">({relativeFromNow(job.next_run_at)})</span></span>
    : job.is_paused
      ? <span className="text-warning">一時停止中</span>
      : <span className="text-fg-subtle">—</span>;

  return (
    <div className={`bg-surface-1 border rounded-lg p-3 md:p-4 space-y-3 ${job.enabled ? "border-accent/40" : "border-border-subtle opacity-90"}`}>
      {/* header */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <Icon className={`h-4 w-4 shrink-0 ${categoryForId(job.id).accentText}`} aria-hidden />
            <span className="text-sm font-semibold text-fg break-all">{job.title}</span>
            <span className="text-[10px] uppercase bg-surface-3 text-fg-subtle px-1.5 py-0.5 rounded font-mono tracking-wide">{vocabLabel("job_kind", job.kind)}</span>
            <span className={`text-[10px] uppercase px-1.5 py-0.5 rounded font-mono tracking-wide ${protCls}`}>{vocabLabel("job_protection", job.protection)}</span>
            {job.heavy && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-warning-soft text-warning border border-warning/40 inline-flex items-center gap-1">
                <AlertTriangle className="h-3 w-3" aria-hidden /> 重処理 {job.max_runtime_minutes}分
              </span>
            )}
            {job.respects_analysis_window && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-3 text-fg-muted border border-border-subtle">
                重処理中は自動停止 (終了後に再開)
              </span>
            )}
          </div>
          <p className="m-0 text-[11.5px] text-fg-muted leading-snug">{job.description}</p>
        </div>
        {!readOnly && <Toggle enabled={job.enabled} pending={togglePending} onChange={onToggle} />}
      </div>

      {/* 左=実行履歴 (サイド) / 右=設定 (一続き)。履歴を設定の間に挟ませない。
          モバイルは 1 カラムで設定を上・履歴を下に (編集をしやすく)。 */}
      <div className="grid grid-cols-1 lg:grid-cols-[15rem_minmax(0,1fr)] gap-x-4 gap-y-3">
        {/* 実行履歴: デスクトップ左サイド / モバイル下 */}
        <div className="order-2 lg:order-1">
          <RunHistory jobId={job.id} />
        </div>

        {/* 設定 (meta → danger → 実行 → スケジュール編集 を一続きに): モバイル上 / デスクトップ右 */}
        <div className="order-1 lg:order-2 min-w-0 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-[11px]">
            <Meta label="スケジュール"><span className="font-mono text-fg">{job.schedule_label}</span></Meta>
            <Meta label="次回 (JST)">
              {isReactive ? <span className="text-fg-subtle">状況に応じて自動実行</span> : <span className="font-mono text-fg">{nextRun}</span>}
            </Meta>
          </div>

          {job.danger_note && (
            <div className="bg-warning-soft border border-warning/50 rounded px-2.5 py-1.5 text-[11px] text-warning flex items-start gap-1.5">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" /> {job.danger_note}
            </div>
          )}

          {/* actions — 即時実行は Tier1 (認証済みの公開 instance でも可)、
              ON/OFF と再スケジュールはローカル full instance 専用 (Tier2) */}
          {canRun && (
            <div className="flex items-center gap-2 flex-wrap">
              {!isReactive && (
                <button
                  onClick={() => runMut.mutate()}
                  disabled={runMut.isPending || !runAvailable}
                  title={runAvailable ? "今すぐ実行" : "スケジューラ停止中"}
                  className="bg-surface-2 border border-border-subtle text-accent hover:bg-accent-subtle rounded px-2.5 py-1 text-xs font-semibold inline-flex items-center gap-1.5 disabled:opacity-40"
                >
                  {runMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                  今すぐ実行
                </button>
              )}
              {flash && <span className="text-[11px] text-success">{flash}</span>}
            </div>
          )}

          {/* 常時表示のスケジュール編集フォーム (折りたたみ廃止) */}
          {!readOnly && (
            <ScheduleEditor job={job} onSaved={(msg) => { onChanged(); showFlash(msg); }} />
          )}
        </div>
      </div>
    </div>
  );
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-surface-2 rounded px-2.5 py-1.5 min-w-0">
      <div className="text-fg-subtle uppercase text-[9.5px] font-semibold tracking-wider mb-0.5">{label}</div>
      <div className="truncate">{children}</div>
    </div>
  );
}

// ---- 実行履歴リスト -----------------------------------------------------
// listJobRuns を 10s ポーリング (running→succeeded の遷移が見えるように)。job.id 変更で再取得。
function RunHistory({ jobId }: { jobId: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["job-runs", jobId],
    queryFn: () => listJobRuns(jobId, 20),
    refetchInterval: 10_000,
  });

  return (
    <div className="bg-surface-2/30 border border-border-subtle rounded-md p-2.5 space-y-1.5">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-fg-subtle font-semibold">
        <History className="h-3 w-3" aria-hidden /> 実行履歴
      </div>
      {/* 固定高のスクロール枠。件数に依らず常にこの高さ (超過分だけ内部スクロール) → パネル高さ統一。 */}
      <div className="h-64 lg:h-80 overflow-y-auto pr-1">
        {isLoading ? (
          <div className="space-y-1">
            {[1, 2, 3].map((i) => <div key={i} className="h-6 bg-surface-2 animate-shimmer rounded" />)}
          </div>
        ) : isError ? (
          <div className="text-[11px] text-fg-subtle">履歴を取得できませんでした。</div>
        ) : !data || data.runs.length === 0 ? (
          <div className="text-[11px] text-fg-subtle">実行履歴なし</div>
        ) : (
          <div className="space-y-0.5">
            {data.runs.map((run, i) => <RunHistoryRow key={`${run.started_at}-${i}`} run={run} />)}
          </div>
        )}
      </div>
    </div>
  );
}

function RunHistoryRow({ run }: { run: JobRunRecord }) {
  const health = runHealth(run.status);
  const color = runHealthColor(health);
  const dur = durationLabel(run.started_at, run.finished_at, health);
  const metrics = metricsLabel(run);
  const detailIsError = health === "failed";

  return (
    <div className="flex items-start gap-2 text-[11px] py-0.5">
      <span className={`w-2 h-2 rounded-full shrink-0 mt-1 ${color.dot} ${health === "running" ? "animate-pulse" : ""}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`font-mono ${color.text}`} title={formatJst(run.started_at)}>{formatJstShort(run.started_at)}</span>
          <span className="text-fg-subtle">{relativeFromNow(run.started_at)}</span>
          {dur && <span className="text-fg-subtle">· {dur}</span>}
          {metrics && <span className="text-fg-muted font-mono">· {metrics}</span>}
        </div>
        {run.detail && (
          <div className={`truncate text-[10px] ${detailIsError ? "text-critical" : "text-fg-subtle"}`} title={run.detail}>
            {run.detail}
          </div>
        )}
      </div>
    </div>
  );
}

// 所要時間ラベル (finished-started)。running は「実行中」、算出不能 (bespoke 等) は空。
function durationLabel(startedAt: string, finishedAt: string | null, health: string): string {
  if (health === "running") return "実行中";
  if (!finishedAt) return "";
  const s = new Date(startedAt).getTime();
  const f = new Date(finishedAt).getTime();
  if (Number.isNaN(s) || Number.isNaN(f) || f < s) return "";
  const sec = Math.round((f - s) / 1000);
  if (sec < 60) return `${sec}秒`;
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  return rem === 0 ? `${min}分` : `${min}分${rem}秒`;
}

// pipeline メトリクス (posted/total_fetched/error_count が非 null のときのみ)。
function metricsLabel(run: JobRunRecord): string {
  const parts: string[] = [];
  if (run.total_fetched != null) parts.push(`取得 ${run.total_fetched}`);
  if (run.posted != null) parts.push(`投稿 ${run.posted}`);
  if (run.error_count != null) parts.push(`エラー ${run.error_count}`);
  return parts.join(" · ");
}

function Toggle({ enabled, pending, onChange }: { enabled: boolean; pending: boolean; onChange: (next: boolean) => void }) {
  return (
    <button
      role="switch"
      aria-checked={enabled}
      aria-label={enabled ? "有効 (クリックで停止)" : "無効 (クリックで有効化)"}
      disabled={pending}
      onClick={() => onChange(!enabled)}
      className={`relative shrink-0 w-11 h-6 rounded-full transition-colors disabled:opacity-50 ${enabled ? "bg-success" : "bg-surface-3 border border-border-subtle"}`}
    >
      <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-bg shadow transition-transform ${enabled ? "translate-x-5" : ""}`}>
        {pending && <Loader2 className="h-3.5 w-3.5 animate-spin text-fg-muted absolute inset-0 m-auto" />}
      </span>
    </button>
  );
}

// ---- inline schedule editor (曜日/実施日を dropdown 化) ------------------
const INP = "bg-surface-1 border border-border-subtle rounded px-2 py-1 text-xs tnum";

function ScheduleEditor({ job, onSaved }: { job: JobView; onSaved: (msg: string) => void }) {
  const [hour, setHour] = useState(job.hour ?? 0);
  const [minute, setMinute] = useState(job.minute ?? 0);
  const [dow, setDow] = useState(normalizeDow(job.day_of_week));
  const [day, setDay] = useState(job.day ?? "");
  const [intervalMinutes, setIntervalMinutes] = useState(job.interval_minutes ?? 60);
  const [offsetMinutes, setOffsetMinutes] = useState(job.offset_minutes ?? 0);
  const [debounceHours, setDebounceHours] = useState(job.debounce_hours ?? 6);
  const [error, setError] = useState<string | null>(null);
  const [warn, setWarn] = useState<string | null>(job.danger_note);
  const lastHm = useRef({ h: job.hour ?? 0, m: job.minute ?? 0 });

  // timeline の drag-apply 等で job の hour/minute が変わったら editor 値も追従。
  useEffect(() => {
    const nh = job.hour ?? 0;
    const nm = job.minute ?? 0;
    if (nh !== lastHm.current.h || nm !== lastHm.current.m) {
      setHour(nh);
      setMinute(nm);
      lastHm.current = { h: nh, m: nm };
    }
  }, [job.hour, job.minute]);

  const save = useMutation({
    mutationFn: () => {
      const patch: SchedulePatch = { schedule_type: job.schedule_type };
      if (job.schedule_type === "cron") {
        patch.hour = hour;
        patch.minute = minute;
        patch.day_of_week = dow; // 空文字 = 毎日 (backend が受ける)
        patch.day = day.trim();
      } else if (job.schedule_type === "interval") {
        patch.interval_minutes = intervalMinutes;
        patch.offset_minutes = offsetMinutes;
      } else {
        patch.debounce_hours = debounceHours;
      }
      return rescheduleJob(job.id, patch);
    },
    onSuccess: (res) => {
      setError(null);
      setWarn(res.danger_note);
      onSaved(`スケジュールを更新: ${res.schedule_label}`);
    },
    onError: (e: unknown) => {
      setError(e instanceof JobApiError ? e.detail : e instanceof Error ? e.message : "更新に失敗");
    },
  });

  return (
    <div className="border-t border-border-subtle pt-3 space-y-2.5">
      {job.schedule_type === "cron" && (
        <>
          <div className="flex items-center gap-2 text-xs flex-wrap">
            <span className="text-fg-muted w-16">時刻 (JST)</span>
            <input type="number" min={0} max={23} value={hour} onChange={(e) => setHour(clampInt(e.target.value, 0, 23))} className={`${INP} w-14 text-center`} aria-label="hour" />
            <span className="text-fg-muted">:</span>
            <input type="number" min={0} max={59} value={minute} onChange={(e) => setMinute(clampInt(e.target.value, 0, 59))} className={`${INP} w-14 text-center`} aria-label="minute" />
          </div>
          <div className="flex items-center gap-2 text-xs flex-wrap">
            <span className="text-fg-muted w-16">曜日</span>
            <select value={dow} onChange={(e) => setDow(e.target.value)} className={`${INP} w-28`} aria-label="曜日">
              {DOW_OPTIONS.map((o) => <option key={o.value || "daily"} value={o.value}>{o.label}</option>)}
            </select>
            <span className="text-fg-subtle text-[10px]">週次のみ (毎日=空)</span>
          </div>
          <div className="flex items-center gap-2 text-xs flex-wrap">
            <span className="text-fg-muted w-16">実施日</span>
            <input type="number" min={1} max={31} value={day} onChange={(e) => setDay(e.target.value === "" ? "" : String(clampInt(e.target.value, 1, 31)))} placeholder="毎日" className={`${INP} w-20 text-center`} aria-label="実施日 (day)" />
            <span className="text-fg-subtle text-[10px]">月次のみ (1–31 / 空=毎日)</span>
          </div>
        </>
      )}

      {job.schedule_type === "interval" && (
        <>
          <div className="flex items-center gap-2 text-xs flex-wrap">
            <span className="text-fg-muted w-16">間隔</span>
            <input type="number" min={5} max={1440} value={intervalMinutes} onChange={(e) => setIntervalMinutes(clampInt(e.target.value, 5, 1440))} className={`${INP} w-20`} aria-label="interval minutes" />
            <span className="text-fg-muted">分おき (最小 5)</span>
          </div>
          <div className="flex items-center gap-2 text-xs flex-wrap">
            <span className="text-fg-muted w-16">オフセット</span>
            <input type="number" min={0} max={59} value={offsetMinutes} onChange={(e) => setOffsetMinutes(clampInt(e.target.value, 0, 59))} className={`${INP} w-20`} aria-label="offset minutes" />
            <span className="text-fg-muted">分（毎時この分に開始）</span>
          </div>
        </>
      )}

      {job.schedule_type === "reactive" && (
        <div className="flex items-center gap-2 text-xs flex-wrap">
          <span className="text-fg-muted w-20">連続実行の抑制</span>
          <input type="number" min={0} max={168} value={debounceHours} onChange={(e) => setDebounceHours(clampInt(e.target.value, 0, 168))} className={`${INP} w-20`} aria-label="debounce hours" />
          <span className="text-fg-muted">時間（この間は連続実行しない）</span>
        </div>
      )}

      {error && (
        <div className="bg-critical-soft border border-critical rounded px-2.5 py-1.5 text-[11px] text-critical flex items-start gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" /> {error}
        </div>
      )}
      {warn && !error && (
        <div className="bg-warning-soft border border-warning/50 rounded px-2.5 py-1.5 text-[11px] text-warning flex items-start gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" /> {warn}
        </div>
      )}

      <button
        onClick={() => save.mutate()}
        disabled={save.isPending}
        className="bg-accent text-bg px-3 py-1 rounded text-xs font-semibold hover:bg-accent-hover disabled:opacity-40 inline-flex items-center gap-1.5"
      >
        {save.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
        スケジュールを適用
      </button>
    </div>
  );
}

// day_of_week を dropdown が扱える単一値へ正規化 (複数曜日は先頭を採用、不明は空=毎日)。
function normalizeDow(raw: string | null): string {
  if (!raw) return "";
  const first = raw.split(",")[0]?.trim().toLowerCase() ?? "";
  return DOW_OPTIONS.some((o) => o.value === first) ? first : "";
}

function clampInt(raw: string, min: number, max: number): number {
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) return min;
  return Math.min(max, Math.max(min, n));
}
