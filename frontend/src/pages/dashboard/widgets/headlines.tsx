// 最新ヘッドライン widget。config.axis で PMESII軸 / PIR分類 を切替、config.per で件数。

import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { pirApi } from "../../../api/pir";
import { WidgetCard, Loading, Empty, WidgetError, HeadlineLine, HeadlineGroup, cfgStr, cfgNum, type WidgetProps } from "../shared";

export function LatestHeadlinesWidget({ config }: WidgetProps) {
  const axis = cfgStr(config, "axis", "pmesii");
  const per = cfgNum(config, "per", 2);
  return axis === "pir" ? <PirHeadlines per={per} /> : <PmesiiHeadlines per={per} />;
}

function PmesiiHeadlines({ per }: { per: number }) {
  const { data, isError } = useQuery({ queryKey: ["dash-pmesii"], queryFn: () => api.pmesii("30"), refetchInterval: 5 * 60_000 });
  const cards = data?.cards ?? [];
  return (
    <WidgetCard title="最新ヘッドライン (PMESII軸別)" href="/app/news" linkLabel="記事 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : cards.length === 0 ? <Empty>PMESII データなし。</Empty> : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-5 gap-y-3">
          {cards.map((c) => (
            <HeadlineGroup key={c.axis_id} label={c.display}
              badge={
                c.is_degraded ? (
                  <span className="text-[9px] px-1 rounded bg-critical-soft text-critical" title="収集量が平常の2割未満に低下 — 事象がないのではなく取りこぼしの可能性">供給劣化</span>
                ) : c.is_spike ? (
                  <span className="text-[9px] px-1 rounded bg-warning-soft text-warning">急増</span>
                ) : undefined
              }>
              {c.recent_incidents.length === 0 ? <div className="text-fg-subtle text-[11px] italic pl-1">—</div> : (
                <ul className="space-y-1">
                  {c.recent_incidents.slice(0, per).map((it, i) => (
                    <HeadlineLine key={it.url || i} title={it.title} url={it.url} importance={it.importance} feed={it.feed_title} />
                  ))}
                </ul>
              )}
            </HeadlineGroup>
          ))}
        </div>
      )}
    </WidgetCard>
  );
}

function PirHeadlines({ per }: { per: number }) {
  const { data, isError } = useQuery({ queryKey: ["pir-dashboard"], queryFn: () => pirApi.dashboardOverview(), refetchInterval: 60_000 });
  const items = (data?.items ?? [])
    .filter((it) => (it.headlines?.length ?? 0) > 0)
    .sort((a, b) => b.match_count_7d - a.match_count_7d);
  return (
    <WidgetCard title="最新ヘッドライン (PIR別)" href="/app/pir" linkLabel="PIR →">
      {isError ? <WidgetError /> : !data ? <Loading /> : items.length === 0 ? (
        <Empty>直近の PIR マッチ記事がありません。</Empty>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-5 gap-y-3">
          {items.map((it) => (
            <HeadlineGroup key={it.pir_id}
              label={it.title}
              badge={<span className="text-[9px] text-fg-subtle">(直近7日 {it.match_count_7d}件)</span>}>
              <ul className="space-y-1">
                {(it.headlines ?? []).slice(0, per).map((h, i) => (
                  <HeadlineLine key={h.url || i} title={h.title} url={h.url} importance={h.importance} feed={h.feed_title} />
                ))}
              </ul>
            </HeadlineGroup>
          ))}
        </div>
      )}
    </WidgetCard>
  );
}
