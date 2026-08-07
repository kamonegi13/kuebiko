// 過去参照 (retrospection) + 運用 (ops) 系 widget。
// RecentIncidents / DailyPostsTrend / Health / RecentRuns。

import { useQuery } from "@tanstack/react-query";
import { dashboardApi } from "../../../api/runs";
import { pagesApi } from "../../../api/pages";
import { formatJst, formatJstCompact } from "../../../utils/date";
import {
  WidgetCard, Loading, Empty, WidgetError, ImportanceDot, StatusBadge, cfgStr, cfgNum, type WidgetProps,
} from "../shared";

// ── 直近インシデント (フラット list、重要度フィルタ) ──
export function RecentIncidentsWidget({ config }: WidgetProps) {
  const importance = cfgStr(config, "importance", ""); // ""=all
  const per = cfgNum(config, "per", 8);
  const { data, isError } = useQuery({
    queryKey: ["dash-history", importance],
    queryFn: () => pagesApi.history({ importance: importance || undefined, status: "posted", limit: 40 }),
    refetchInterval: 2 * 60_000,
  });
  const arts = (data?.articles ?? []).slice(0, per);
  const label = importance === "high" ? " (高)" : importance === "medium" ? " (中以上)" : "";
  const href = importance ? `/app/news?importance=${importance}` : "/app/news";
  return (
    <WidgetCard title={`直近インシデント${label}`} href={href} linkLabel="記事一覧 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : arts.length === 0 ? <Empty>該当インシデントなし。</Empty> : (
        <ul className="divide-y divide-border-subtle/50">
          {arts.map((a) => (
            <li key={a.id} className="flex items-start gap-2 py-2 first:pt-0">
              <ImportanceDot importance={a.importance} className="mt-[6px] w-1.5 h-1.5" />
              <div className="flex-1 min-w-0">
                <a href={`/app/article/${encodeURIComponent(a.article_id)}`}
                  className="block text-[13px] font-medium leading-snug text-fg hover:text-accent hover:underline line-clamp-2" title={a.title}>{a.title}</a>
                <div className="text-[10px] text-fg-subtle flex gap-2 mt-1">
                  <span className="truncate">{a.feed_title}</span>
                  {(a.published_at ?? a.created_at) && (
                    <span className="shrink-0 ml-auto tnum" title={a.published_at ? "公開時刻" : "取得時刻 (公開時刻不明)"}>
                      {formatJstCompact(a.published_at ?? a.created_at)}
                    </span>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}

// ── 日次投稿推移 (棒グラフ) ──
export function DailyPostsTrendWidget({ config }: WidgetProps) {
  const days = cfgNum(config, "days", 14);
  const { data, isError } = useQuery({ queryKey: ["dashboard-trend", days], queryFn: () => dashboardApi.summary(days), refetchInterval: 60_000 });
  const series = data?.summary.daily_posts ?? [];
  const max = Math.max(...series.map(([, n]) => n), 1);
  const total = series.reduce((s, [, n]) => s + n, 0);
  return (
    <WidgetCard title="日次投稿推移" href="/app/history" linkLabel="履歴 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : series.length === 0 ? <Empty>投稿データなし。</Empty> : (
        <div className="space-y-1">
          <div className="flex items-end gap-px h-24">
            {series.map(([date, n]) => (
              <div key={date} className="flex-1 flex flex-col justify-end items-center group" title={`${date}: ${n}件`}>
                <span className="text-[9px] text-fg-subtle tnum opacity-0 group-hover:opacity-100">{n}</span>
                <div className="w-full bg-accent/60 hover:bg-accent rounded-sm" style={{ height: `${Math.max(2, (n / max) * 88)}px` }} />
              </div>
            ))}
          </div>
          <div className="text-[10px] text-fg-subtle text-right">直近{days}日 · 計 {total} 件</div>
        </div>
      )}
    </WidgetCard>
  );
}

// ── 死活監視 (Ollama / Discord / IMAP 疎通) ──
export function HealthWidget() {
  const { data, isError } = useQuery({ queryKey: ["dash-health"], queryFn: () => pagesApi.healthStatus(), refetchInterval: 2 * 60_000 });
  const checks = data?.checks ?? [];
  const order: Record<string, number> = { error: 0, warning: 1, ok: 2 };
  const sorted = [...checks].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  return (
    <WidgetCard title="死活監視">
      {isError ? <WidgetError /> : !data ? <Loading /> : data.load_error ? (
        <Empty>設定読込エラー: {data.load_error}</Empty>
      ) : sorted.length === 0 ? <Empty>チェック項目なし。</Empty> : (
        <ul className="space-y-1">
          {sorted.map((c) => {
            const detail = c.detail;
            return (
              <li key={c.name} className="flex items-center gap-2 text-xs">
                <span className="font-semibold text-fg w-24 shrink-0 truncate" title={c.name}>{c.name}</span>
                <StatusBadge status={c.status} />
                <span className="text-fg-subtle truncate flex-1 min-w-0" title={detail}>{detail}</span>
              </li>
            );
          })}
        </ul>
      )}
    </WidgetCard>
  );
}

// ── 直近の実行 (run 一覧) ──
export function RecentRunsWidget({ config }: WidgetProps) {
  const per = cfgNum(config, "per", 6);
  const { data, isError } = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.summary(7), refetchInterval: 30_000 });
  const runs = (data?.recent_runs ?? []).slice(0, per);
  return (
    <WidgetCard title="直近の実行" href="/app/schedule" linkLabel="実行管理 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : runs.length === 0 ? <Empty>実行履歴なし。</Empty> : (
        <ul className="space-y-1">
          {runs.map((r) => (
            <li key={r.id} className="flex items-center gap-2 text-xs">
              <a href={`/app/run/${r.id}`} className="text-accent hover:underline tnum shrink-0">#{r.id}</a>
              <span className="text-fg font-mono truncate flex-1 min-w-0" title={r.pipeline}>{r.pipeline}</span>
              {r.posted > 0 && <span className="text-success tnum shrink-0" title="投稿">+{r.posted}</span>}
              <StatusBadge status={r.status} />
              <span className="text-fg-subtle shrink-0 w-16 text-right" title={formatJst(r.started_at)}>{formatJstCompact(r.started_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </WidgetCard>
  );
}
