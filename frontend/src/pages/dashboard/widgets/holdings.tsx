// 蓄積 (knowledge holdings) + ソース系 widget。
// Holdings / SourceContribution (貢献度)。
// 注: SourceDiscovery (新規候補) は発見支援査定で撤去 (SNR 低・実使用ゼロ、2026-06-14)。

import { useQuery } from "@tanstack/react-query";
import { dashboardApi } from "../../../api/runs";
import { api } from "../../../api/client";
import { pagesApi } from "../../../api/pages";
import { WidgetCard, Loading, Empty, WidgetError, Holding, HBar, cfgNum, type WidgetProps } from "../shared";

// db_stats の生キー → 日本語ラベル (未知キーは生キーで fallback)
const HOLDING_LABEL: Record<string, string> = {
  articles: "記事", iocs: "IoC", actors: "アクター", entities: "エンティティ",
  embeddings: "埋め込み", runs: "実行", spotlights: "Spotlight", syntheses: "現況",
  feeds: "フィード", subscriptions: "購読",
};

export function HoldingsWidget() {
  const { data: dash } = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.summary(7), staleTime: 30_000 });
  const { data: snap } = useQuery({ queryKey: ["dash-snapshot"], queryFn: () => api.snapshot({ time: "30" }), staleTime: 60_000 });
  const dbStats = dash?.db_stats ?? {};
  return (
    <WidgetCard title="Intelligence Holdings" href="/app/subscriptions" linkLabel="ソース →">
      <div className="grid grid-cols-2 gap-2 text-sm">
        {snap?.actors_count !== undefined && <Holding label="追跡アクター" value={snap.actors_count} />}
        {Object.entries(dbStats).map(([k, v]) => <Holding key={k} label={HOLDING_LABEL[k] ?? k} value={v} />)}
      </div>
    </WidgetCard>
  );
}

// ── ソース貢献度 (投稿件数 top-N feed) ──
export function SourceContributionWidget({ config }: WidgetProps) {
  const per = cfgNum(config, "per", 8);
  const { data, isError } = useQuery({
    queryKey: ["dash-subscriptions"],
    queryFn: () => pagesApi.subscriptions(),
    staleTime: 10 * 60_000,
  });
  const rows = Object.values(data?.stats ?? {})
    .filter((s) => s.posted_count > 0)
    .sort((a, b) => b.posted_count - a.posted_count)
    .slice(0, per);
  const max = Math.max(...rows.map((r) => r.posted_count), 1);
  return (
    <WidgetCard title="ソース貢献度 (投稿数)" href="/app/subscriptions" linkLabel="購読管理 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : rows.length === 0 ? <Empty>投稿実績のあるソースがありません。</Empty> : (
        <div className="space-y-1.5">
          {rows.map((r) => (
            <HBar key={r.feed_title} label={r.feed_title} value={r.posted_count} max={max}
              tone={r.high_count > 0 ? "critical" : "accent"}
              suffix={r.high_count > 0 ? <span className="text-[9px] text-critical w-9 text-right shrink-0">H{r.high_count}</span> : <span className="w-9 shrink-0" />} />
          ))}
          <div className="text-[10px] text-fg-subtle text-right">累計投稿数 · 赤=重要度高の実績あり</div>
        </div>
      )}
    </WidgetCard>
  );
}
