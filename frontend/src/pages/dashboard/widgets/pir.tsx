// PIR (Priority Intelligence Requirements) 系 widget。
// PirCoverage (充足/ギャップ) / PirSpotlight (縦断 narrative)。

import { useQuery } from "@tanstack/react-query";
import { pirApi } from "../../../api/pir";
import { spotlightApi, type SpotlightPeriod } from "../../../api/spotlight";
import { formatJstCompact } from "../../../utils/date";
import { vocabLabel } from "../../../hooks/useVocab";
import { WidgetCard, Loading, Empty, WidgetError, type WidgetProps } from "../shared";

export function PirCoverageWidget() {
  const { data, isError } = useQuery({ queryKey: ["pir-dashboard"], queryFn: () => pirApi.dashboardOverview(), refetchInterval: 60_000 });
  const items = data?.items ?? [];
  const sorted = [...items].sort((a, b) => {
    if (a.match_count_7d === 0 && b.match_count_7d !== 0) return -1;
    if (b.match_count_7d === 0 && a.match_count_7d !== 0) return 1;
    return b.match_count_7d - a.match_count_7d;
  });
  return (
    <WidgetCard title="PIR 充足 / ギャップ" href="/app/pir" linkLabel="PIR →">
      {isError ? <WidgetError /> : !data ? <Loading /> : sorted.length === 0 ? <Empty>PIR データなし。</Empty> : (
        <div className="space-y-0.5">
          {sorted.map((it) => {
            const maxSpark = Math.max(...it.sparkline_7d, 1);
            const status = it.match_count_7d === 0 ? "zero" : it.match_count_7d < 3 ? "low" : "active";
            const dotColor = status === "zero" ? "bg-warning" : status === "low" ? "bg-fg-subtle" : "bg-accent";
            return (
              <a key={it.pir_id} href={`/app/pir/${encodeURIComponent(it.pir_id)}`}
                className="flex items-center gap-2 px-1 py-0.5 rounded hover:bg-surface-2 no-underline group" title={it.title}>
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotColor}`} aria-label={status} />
                <span className="flex-1 min-w-0 truncate text-sm text-fg group-hover:text-accent-hover">{it.title}</span>
                <div className="inline-flex items-end gap-px h-4 w-10 shrink-0" aria-label="7-day sparkline">
                  {it.sparkline_7d.map((n, i) => (
                    <div key={i} className={`w-1 rounded-sm ${n === 0 ? "bg-surface-3" : "bg-accent/60"}`}
                      style={{ height: `${Math.max(2, (n / maxSpark) * 16)}px` }} />
                  ))}
                </div>
                <span className={`tnum text-sm font-semibold w-7 text-right ${status === "zero" ? "text-warning" : "text-fg"}`}>{it.match_count_7d}</span>
              </a>
            );
          })}
          <div className="mt-1 text-[10px] text-fg-subtle text-right">直近7日の該当件数 · 0件=収集の空白</div>
        </div>
      )}
    </WidgetCard>
  );
}

// ── PIR Spotlight: spotlight 有効な PIR の縦断 narrative ──
// 可読性: 展開式にせず「見えたまま読める」。冗長を削り (headline/outlook を各2行に
// バウンド)、PIR 名をラベル化 + 件数を右肩へ、アイテム境界を強めて塊で見えるように。
// 全文は Synthesis ドリルに委ねる (per-item 展開の手間を排除)。
export function PirSpotlightWidget({ mobile }: WidgetProps) {
  // Spotlight は週次のみ生成 (cron=月曜09:00 weekly)。日次は別機能の PIR Daily Focus、
  // 月次 spotlight は未スケジュール。よって週次固定とし、空になる期間選択肢は出さない。
  const period: SpotlightPeriod = "weekly";
  const { data, isError } = useQuery({
    queryKey: ["dash-spotlight", period],
    queryFn: () => spotlightApi.list(period),
    refetchInterval: 10 * 60_000,
  });
  const items = data?.items ?? [];
  return (
    <WidgetCard title={`PIR Spotlight (${vocabLabel("period_type", period)})`} href="/app/intel/synthesis" linkLabel="Synthesis →">
      {isError ? <WidgetError /> : !data ? <Loading /> : items.length === 0 ? (
        <Empty>{vocabLabel("period_type", period)} の Spotlight はまだありません。</Empty>
      ) : (
        <div className="divide-y divide-border-subtle">
          {items.map((sp) => (
            <div key={sp.pir_id} className="py-3 first:pt-0 last:pb-0">
              {/* L1 PIR ラベル + 件数 (走査アンカー)。accent バーで「見出し」を明示 */}
              <div className="flex items-baseline gap-2 mb-1">
                <span className="w-0.5 h-3.5 rounded-full bg-accent/60 shrink-0" aria-hidden />
                <span className="flex-1 min-w-0 truncate text-[12px] font-semibold text-accent-hover" title={sp.pir_title}>
                  {sp.pir_title}
                </span>
                <span className="shrink-0 text-[11px] text-fg-subtle tnum" title="該当件数">{sp.article_count} 件</span>
              </div>
              {/* L2 BLUF (主役): 少し大きく濃く、2 行でバウンド */}
              <div className="text-fg font-medium text-[14px] leading-snug line-clamp-2">{sp.headline}</div>
              {/* L3 分析 (補助): 淡く小さく、mobile は省略 / desktop は 2 行 */}
              {!mobile && sp.outlook && (
                <div className="text-[12px] text-fg-muted leading-relaxed line-clamp-2 mt-1">{sp.outlook}</div>
              )}
              {/* L4 メタ (最弱) */}
              <div className="text-[10px] text-fg-subtle mt-1.5">{formatJstCompact(sp.generated_at)}</div>
            </div>
          ))}
        </div>
      )}
    </WidgetCard>
  );
}
