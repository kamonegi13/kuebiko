import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { intentLabel } from "../../utils/diamond";
import { vocabLabel } from "../../hooks/useVocab";
import type { CorrelationPair, EntityTrend, ForecastResponse } from "../../api/types";

// scope+value を表示名に (intent は日本語ラベル、actor 等は value)。
function entityLabel(scope: string, value: string): string {
  if (scope === "intent") return intentLabel(value) || value;
  return value;
}

// label は vocab "forecast_direction" (vocabLabel) を SSoT に。ここは icon + 色 (tone) のみ保持。
const DIRECTION_STYLE: Record<string, { icon: string; tone: string }> = {
  increasing: { icon: "▲", tone: "text-critical" },
  decreasing: { icon: "▼", tone: "text-accent" },
  stable: { icon: "▶", tone: "text-fg-subtle" },
};

// 週次系列を小さな SVG スパークラインに (TimelineBars と同系統、自己完結)。
function Sparkline({ data, highlight }: { data: number[]; highlight?: boolean }) {
  const max = Math.max(1, ...data);
  const w = 84;
  const h = 24;
  const bw = data.length > 0 ? w / data.length : w;
  return (
    <svg width={w} height={h} className="shrink-0" role="img" aria-label="週次活動">
      {data.map((v, i) => {
        const bh = Math.max(1, (v / max) * (h - 2));
        const isLast = i === data.length - 1;
        return (
          <rect
            key={i}
            x={i * bw + 1}
            y={h - bh}
            width={Math.max(1, bw - 1.5)}
            height={bh}
            rx={1}
            className={isLast && highlight ? "fill-critical" : "fill-accent/60"}
          >
            <title>{`${v} 件`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function TrendRow({ t }: { t: EntityTrend }) {
  const dir = DIRECTION_STYLE[t.direction] || DIRECTION_STYLE.stable;
  return (
    <li className="flex items-center gap-3 px-3 py-2 hover:bg-surface-2 transition-colors">
      <a
        href={`/app/news?pivot_type=${encodeURIComponent(t.scope === "intent" ? "actor" : t.scope)}&pivot_value=${encodeURIComponent(t.value)}`}
        className="text-sm text-fg hover:text-accent font-medium truncate flex-1 min-w-0"
        title={`${entityLabel(t.scope, t.value)} を逆引き`}
      >
        {entityLabel(t.scope, t.value)}
      </a>
      {(t.matched_pir_ids?.length ?? 0) > 0 && (
        <span
          className="text-[10px] font-semibold text-accent bg-accent/15 rounded px-1.5 py-0.5 shrink-0"
          title={`該当 PIR: ${t.matched_pir_ids.join(", ")}`}
        >
          PIR{t.matched_pir_ids.length > 1 ? `×${t.matched_pir_ids.length}` : ""}
        </span>
      )}
      {t.is_spike && (
        <span
          className="text-[10px] font-semibold text-critical bg-critical/15 rounded px-1.5 py-0.5 shrink-0"
          title="平常時と比べてどれだけ多いか (標準偏差の倍数)"
        >
          平常比 z={t.z_score.toFixed(1)}
        </span>
      )}
      <span className={`text-xs shrink-0 ${dir.tone}`} title={vocabLabel("forecast_direction", t.direction)}>
        {dir.icon}
      </span>
      <span className="text-xs text-fg-subtle tnum shrink-0 w-8 text-right">{t.total}</span>
      <Sparkline data={t.weekly} highlight={t.is_spike} />
    </li>
  );
}

function Panel({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
      <div className="px-3 py-2 bg-surface-2 border-b border-border-subtle">
        <div className="text-fg text-sm font-semibold">{title}</div>
        {hint && <div className="text-fg-subtle text-[11px] mt-0.5">{hint}</div>}
      </div>
      {children}
    </div>
  );
}

function CorrelationList({ pairs }: { pairs: CorrelationPair[] }) {
  if (pairs.length === 0) {
    return <div className="px-3 py-4 text-fg-subtle text-sm text-center">強く連動する組み合わせなし</div>;
  }
  return (
    <ul className="divide-y divide-border-subtle">
      {pairs.map((p) => (
        <li key={`${p.a}-${p.b}`} className="flex items-center gap-2 px-3 py-2 text-sm">
          <span className="font-mono text-fg truncate">{p.a}</span>
          <span className="text-fg-subtle">↔</span>
          <span className="font-mono text-fg truncate">{p.b}</span>
          <span
            className={`ml-auto tnum text-xs font-semibold ${p.correlation >= 0 ? "text-accent" : "text-warning"}`}
            title="連動の強さ (-1〜1。1 に近いほど同時に増減する)"
          >
            連動 {p.correlation.toFixed(2)}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ForecastTab() {
  const { data, isLoading, error } = useQuery<ForecastResponse>({
    queryKey: ["forecast", 8],
    queryFn: () => api.forecast(8),
  });

  if (isLoading) {
    return <div className="text-fg-subtle text-sm p-6">予測を計算中…</div>;
  }
  if (error) {
    return <div className="text-critical text-sm p-6">エラー: {String(error)}</div>;
  }
  if (!data || !data.has_data) {
    return (
      <div className="text-fg-subtle text-sm p-10 text-center">
        予測に十分なデータがありません (記事の蓄積が必要です)。
      </div>
    );
  }

  const stats = data.indicator_stats;
  const statsV2 = data.indicator_stats_v2;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 px-1">
        <div>
          <h3 className="m-0 text-lg font-bold text-fg tracking-tight">将来予測</h3>
          <p className="text-fg-subtle text-xs mt-0.5">
            直近 {data.weeks} 週のアクター・意図の活動から、急な増加・傾向・連動を統計的に自動算出。
          </p>
        </div>
        {/* FC2: 指標的中率 — v2 (較正済み) を主表示、v1 は参考 (準トートロジーのため格下げ) */}
        <div
          className="bg-surface-1 border border-border-subtle rounded-lg px-3 py-2 text-center"
          title="週ごとに件数を正規化し、平常時と比べて統計的に有意な増加だけを「的中」と数えます。平常並みの継続は「一部的中」、下回った場合は「外れ」。旧方式は観測が平常値以上ならほぼ常に的中となるため参考表示です。"
        >
          <div className="text-[11px] text-fg-subtle">監視指標の的中率 (補正済み)</div>
          <div className="text-fg font-bold tnum">
            {statsV2 && statsV2.verified > 0
              ? `${(statsV2.hit_rate * 100).toFixed(0)}%`
              : "—"}
            {statsV2 && statsV2.verified > 0 && (
              <span className="text-fg-subtle text-xs font-normal ml-1">
                (的中{statsV2.hits} / 一部的中{statsV2.partials} / 外れ{statsV2.misses})
              </span>
            )}
          </div>
          <div className="text-[10px] text-fg-subtle tnum">
            旧方式(参考): {stats.verified > 0 ? `${(stats.hit_rate * 100).toFixed(0)}%` : "—"} ({stats.hits}/{stats.verified})
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        {/* FC3: spike alerts */}
        <Panel title="急上昇の警戒" hint="平常時と比べ急に動いた対象を統計的に検出">
          {data.spike_alerts.length === 0 ? (
            <div className="px-3 py-4 text-fg-subtle text-sm text-center">今週の急上昇なし</div>
          ) : (
            <ul className="divide-y divide-border-subtle">
              {data.spike_alerts.map((t) => (
                <TrendRow key={`${t.scope}-${t.value}`} t={t} />
              ))}
            </ul>
          )}
        </Panel>

        {/* FC2: 現行監視中の指標 */}
        <Panel title="監視中の指標" hint="急上昇から自動で立て、翌週に本当に続いたかを必ず検証する">
          {data.open_indicators.length === 0 ? (
            <div className="px-3 py-4 text-fg-subtle text-sm text-center">監視中の指標なし</div>
          ) : (
            <ul className="divide-y divide-border-subtle">
              {data.open_indicators.map((ind) => (
                <li key={ind.id} className="flex items-center gap-2 px-3 py-2 text-sm">
                  <span className="font-medium text-fg truncate flex-1">
                    {entityLabel(ind.scope, ind.target_value)}
                  </span>
                  <span className="text-[11px] text-fg-subtle">{vocabLabel("forecast_scope", ind.scope)}</span>
                  <span className="text-[10px] font-semibold text-warning bg-warning/15 rounded px-1.5 py-0.5">
                    {vocabLabel("forecast_direction", ind.direction)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        {/* FC4: actor トレンド */}
        <Panel title="アクター動向" hint="主要アクターの週ごとの活動の推移 (▲増加 ▼減少 ▶横ばい)">
          {data.actor_trends.length === 0 ? (
            <div className="px-3 py-4 text-fg-subtle text-sm text-center">データなし</div>
          ) : (
            <ul className="divide-y divide-border-subtle">
              {data.actor_trends.map((t) => (
                <TrendRow key={t.value} t={t} />
              ))}
            </ul>
          )}
        </Panel>

        {/* FC4: intent トレンド + FC5: 相関 */}
        <div className="space-y-4">
          <Panel title="意図の動向" hint="攻撃者の意図 (諜報/金銭/破壊…) の週ごとの推移">
            {data.intent_trends.length === 0 ? (
              <div className="px-3 py-4 text-fg-subtle text-sm text-center">データなし</div>
            ) : (
              <ul className="divide-y divide-border-subtle">
                {data.intent_trends.map((t) => (
                  <TrendRow key={t.value} t={t} />
                ))}
              </ul>
            )}
          </Panel>
          <Panel title="活動が同期するアクター" hint="活動の増減が同じタイミングで動くアクターの組み合わせ">
            <CorrelationList pairs={data.correlations} />
          </Panel>
        </div>
      </div>
    </div>
  );
}
