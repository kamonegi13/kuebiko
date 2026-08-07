// 状況認識 (situational awareness) 系 widget。
// StatusStrip / StandingAssessment / SynthesisSection / ImportanceMix。

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, X } from "lucide-react";
import { dashboardApi } from "../../../api/runs";
import { api } from "../../../api/client";
import { formatJst, formatJstCompact } from "../../../utils/date";
import { synthesisPeriodForWindow, useWidgetWindow } from "../overviewWindow";
import {
  INTEL, WidgetCard, WidgetShell, Loading, Empty, WidgetError, Stat, StatusBadge, HBar, cfgStr, type WidgetProps,
} from "../shared";
import { vocabLabel } from "../../../hooks/useVocab";

// ── システム状態 (ops、降格) ──
export function StatusStripWidget() {
  const { data, isError } = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.summary(7), refetchInterval: 30_000 });
  if (isError) return <WidgetShell><WidgetError /></WidgetShell>;
  if (!data) return <WidgetShell><span className="text-fg-subtle text-sm">読み込み中…</span></WidgetShell>;
  const s = data.summary;
  const dayAgo = Date.now() - 24 * 60 * 60 * 1000;
  const recent24h = data.recent_runs.filter((r) => r.started_at && new Date(r.started_at).getTime() > dayAgo);
  // 全件失敗 (pipeline がほぼ機能せず) = hard。1記事 LLM エラー等の部分失敗 = soft。
  const hardFailed = recent24h.filter((r) => r.status === "failed");
  const partialErr = recent24h.filter((r) => r.status !== "failed" && r.error_count > 0);
  const todayPosts = s.daily_posts[s.daily_posts.length - 1]?.[1] || 0;
  const hasFail = hardFailed.length > 0;
  const hasPartial = partialErr.length > 0;
  return (
    <div className="space-y-2">
      <div className={`rounded-lg border px-3 py-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-sm ${
        hasFail ? "bg-critical-soft border-critical" : "bg-surface-1 border-border-subtle"
      }`}>
        <span className={`inline-flex items-center gap-1.5 font-semibold ${hasFail ? "text-critical" : hasPartial ? "text-warning" : "text-success"}`}>
          {hasFail ? (
            <><X className="h-3.5 w-3.5" /> 直近24h 失敗 {hardFailed.length}</>
          ) : hasPartial ? (
            <><AlertTriangle className="h-3.5 w-3.5" /> 部分エラー {partialErr.reduce((n, r) => n + r.error_count, 0)} 件 (投稿は継続)</>
          ) : (
            <><Check className="h-3.5 w-3.5" /> 処理 正常</>
          )}
        </span>
        <Stat label="今日投稿" value={todayPosts} />
        <Stat label="抽出失敗率" value={`${(s.extract_failure_rate * 100).toFixed(1)}%`} danger={s.extract_failure_rate > 0.1} />
        {typeof s.stump_body_rate === "number" && (
          <Stat label="切り株率" value={`${(s.stump_body_rate * 100).toFixed(1)}%`} danger={s.stump_body_rate > 0.15} />
        )}
        <Stat label="次回実行" value={formatJstCompact(data.next_run_at)} />
        <a href="/app/schedule" className="ml-auto text-accent text-xs hover:underline">運用詳細 →</a>
      </div>
      {(hasFail || hasPartial) && (
        <div className={`border rounded-lg p-2 space-y-1 ${hasFail ? "bg-critical-soft border-critical/40" : "bg-warning-soft border-warning/40"}`}>
          {[...hardFailed, ...partialErr].slice(0, 4).map((r) => (
            <div key={r.id} className="flex items-center gap-2 text-xs">
              <a href={`/app/run/${r.id}`} className="text-accent hover:underline tnum">#{r.id}</a>
              <span className="text-fg font-mono">{r.pipeline}</span>
              <StatusBadge status={r.status} />
              <span className="ml-auto text-fg-subtle">{formatJst(r.started_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 現況評価 (synthesis headline) ──
export function StandingAssessmentWidget({ config }: WidgetProps) {
  // 対象期間は概観の共有窓に連動 (24h→日次 / 7日→週次 / 30・90日→月次)。
  // 遷移先も同じ写像で period_type を渡し、widget の表示と着地画面を一致させる。
  const period = synthesisPeriodForWindow(useWidgetWindow(config));
  const { data, isError } = useQuery({ queryKey: ["dash-synthesis", period], queryFn: () => api.synthesis(period), refetchInterval: 5 * 60_000 });
  return (
    <WidgetCard title="現況評価" href={`${INTEL}#period_type=${period}`} linkLabel="状況総括 全文 →">
      {isError ? (
        <WidgetError />
      ) : !data || !data.has_data || !data.latest ? (
        <Empty>{vocabLabel("period_type", period)}の状況総括はまだありません。</Empty>
      ) : (
        <div className="space-y-1.5">
          <div className="text-fg font-semibold text-[15px] leading-snug">{data.latest.headline || "(見出しなし)"}</div>
          <div className="text-[11px] text-fg-subtle">
            {vocabLabel("period_type", data.period_type)} · {formatJstCompact(data.latest.generated_at)} 生成 · {data.latest.article_count} 件分析
          </div>
        </div>
      )}
    </WidgetCard>
  );
}

// ── synthesis セクション (CoG / 波及 / 連鎖 / 比重 / PIR) を選んで表示 ──
const SECTION_META: Record<string, { label: string; field: "cog_section" | "spillover_section" | "chain_section" | "weight_section" | "pir_section" }> = {
  cog: { label: "重心", field: "cog_section" },
  spillover: { label: "波及", field: "spillover_section" },
  chain: { label: "連鎖", field: "chain_section" },
  weight: { label: "比重", field: "weight_section" },
  pir: { label: "PIR 評価", field: "pir_section" },
};
export function SynthesisSectionWidget({ config }: WidgetProps) {
  const which = cfgStr(config, "section", "cog");
  const shared = useWidgetWindow(config);
  // period 未設定/auto は共有窓に連動、明示 (daily/weekly/monthly) は固定。
  const raw = cfgStr(config, "period", "auto");
  const period = (raw === "daily" || raw === "weekly" || raw === "monthly"
    ? raw
    : synthesisPeriodForWindow(shared)) as "daily" | "weekly" | "monthly";
  const meta = SECTION_META[which] ?? SECTION_META.cog;
  const { data, isError } = useQuery({ queryKey: ["dash-synthesis", period], queryFn: () => api.synthesis(period), refetchInterval: 5 * 60_000 });
  const text = data?.latest?.[meta.field] ?? "";
  return (
    <WidgetCard title={meta.label} href={`${INTEL}#period_type=${period}`} linkLabel="Synthesis →">
      {isError ? (
        <WidgetError />
      ) : !data || !data.has_data ? (
        <Empty>{vocabLabel("period_type", period)}の状況総括はまだありません。</Empty>
      ) : text.trim() === "" ? (
        <Empty>この区間に該当セクションがありません。</Empty>
      ) : (
        <div className="text-sm text-fg whitespace-pre-wrap break-words leading-relaxed">{text}</div>
      )}
    </WidgetCard>
  );
}

// ── 重要度分布 (今日 / 期間の high/medium/low) ──
export function ImportanceMixWidget() {
  const { data, isError } = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.summary(7), refetchInterval: 60_000 });
  const imp = data?.summary.importance ?? {};
  const rows: [string, number, "critical" | "warning" | "accent"][] = [
    ["high", imp.high ?? 0, "critical"],
    ["medium", imp.medium ?? 0, "warning"],
    ["low", imp.low ?? 0, "accent"],
  ];
  const max = Math.max(...rows.map(([, v]) => v), 1);
  const total = rows.reduce((s, [, v]) => s + v, 0);
  return (
    <WidgetCard title="サイバー脅威 重要度分布 (7日)" href="/app/news?since=168" linkLabel="記事一覧 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : total === 0 ? <Empty>投稿データなし。</Empty> : (
        <div className="space-y-1.5">
          {rows.map(([key, value, tone]) => (
            <HBar key={key} label={vocabLabel("importance", key)} value={value} max={max} tone={tone}
              suffix={<span className="text-[10px] text-fg-subtle w-9 text-right shrink-0">{total > 0 ? `${Math.round((value / total) * 100)}%` : ""}</span>} />
          ))}
          <div className="text-[10px] text-fg-subtle text-right">直近7日 · 脅威カテゴリ計 {total} 件 (地政/研究は除外)</div>
        </div>
      )}
    </WidgetCard>
  );
}
