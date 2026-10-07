// 蓄積 (knowledge holdings) + ソース系 widget。
// Holdings / SourceContribution (貢献度)。
// 注: SourceDiscovery (新規候補) は発見支援査定で撤去 (SNR 低・実使用ゼロ、2026-06-14)。

import { useQuery } from "@tanstack/react-query";
import { dashboardApi } from "../../../api/runs";
import { api } from "../../../api/client";
import { pagesApi } from "../../../api/pages";
import { WidgetCard, Loading, Empty, WidgetError, Holding, HBar, cfgNum, type WidgetProps } from "../shared";

// db_stats のキー → 表示 (2026-10-08 利用者承認)。キーは src/storage/repo_knowledge.py の db_stats と対。
// ここに無いキーは出さない (生のキー名を読者に見せない)。
// - file_size_bytes は出さない: 本番 (PostgreSQL) ではなく使っていない旧 SQLite の大きさで誤り
// - run_logs は出さない: 処理の記録の行数で、蓄積の指標ではない
// - 拡張 (公開版) では分析の蓄積だけ (収集した記事・追跡アクター)。実行回数や URL 数は運用の内部の数字
export const HOLDING_ITEMS: { key: string; label: string; opsOnly: boolean }[] = [
  { key: "articles", label: "収集した記事", opsOnly: false },
  { key: "article_embeddings", label: "意味検索の対象", opsOnly: true },
  { key: "dedup_seen_urls", label: "確認済みの URL", opsOnly: true },
  { key: "runs", label: "処理の実行", opsOnly: true },
];

export function holdingItems(dbStats: Record<string, number>, mirror: boolean): { label: string; value: number }[] {
  return HOLDING_ITEMS.filter((it) => (mirror ? !it.opsOnly : true) && dbStats[it.key] !== undefined).map((it) => ({
    label: it.label,
    value: dbStats[it.key],
  }));
}

export function HoldingsWidget() {
  const { data: dash } = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.summary(7), staleTime: 30_000 });
  const { data: snap } = useQuery({ queryKey: ["dash-snapshot"], queryFn: () => api.snapshot({ time: "30" }), staleTime: 60_000 });
  const dbStats = dash?.db_stats ?? {};
  const mirror = import.meta.env.VITE_MIRROR === "1";
  return (
    // 拡張には購読ソースの画面が無いので、見出しのリンクは運用画面だけ
    <WidgetCard title="Intelligence Holdings" href={mirror ? undefined : "/app/subscriptions"} linkLabel="ソース →">
      <div className="grid grid-cols-2 gap-2 text-sm">
        {snap?.actors_count !== undefined && <Holding label="追跡アクター" value={snap.actors_count} />}
        {holdingItems(dbStats, mirror).map((it) => <Holding key={it.label} label={it.label} value={it.value} />)}
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
              suffix={r.high_count > 0 ? <span className="text-[11.5px] text-critical w-9 text-right shrink-0">H{r.high_count}</span> : <span className="w-9 shrink-0" />} />
          ))}
          <div className="text-[12px] text-fg-subtle text-right">累計投稿数 · 赤=重要度高の実績あり</div>
        </div>
      )}
    </WidgetCard>
  );
}
