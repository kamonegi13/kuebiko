// 公開版の地図ページ。
//
// ⚠ **地図は収集網の観測であって世界ではない**。国を特定できた件数と母集団を
// 必ず併記する (実測では公開記事の 56% に国が付いていない)。割合を隠すと
// 「これが世界の実態」と読まれる。数字は API が返すものをそのまま出す。

import { lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchPublicMap } from "../api/publicNews";

// 基図 (Natural Earth geojson 1.4MB) は地図を開いたときにだけ取りに行く
const PublicMap = lazy(() =>
  import("./PublicMap").then((m) => ({ default: m.PublicMap })),
);

export function PublicMapSection({ onCountry }: { onCountry: (iso: string) => void }) {
  const { data, isFetching, error } = useQuery({
    queryKey: ["public-map"],
    queryFn: () => fetchPublicMap(30),
    staleTime: 10 * 60 * 1000,
  });

  if (error) {
    return <p className="text-sm text-critical">地図を読み込めませんでした。</p>;
  }
  if (!data) {
    return <p className="text-sm text-fg-subtle">{isFetching ? "読み込み中…" : ""}</p>;
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h2 className="text-[13px] font-semibold text-fg-muted">被害国の分布</h2>
        <p className="text-[11px] leading-relaxed text-fg-subtle">
          直近 {data.window_days} 日の掲載記事 {data.total} 件のうち、被害国を特定できた{" "}
          <span className="text-fg-muted">{data.placed} 件</span>
          を地図にしています ({data.unplaced} 件は国を特定できず地図に出ていません)。
        </p>
      </div>

      <Suspense fallback={<div className="h-[52vh] min-h-[280px] rounded-lg bg-surface-2" />}>
        <PublicMap nodes={data.nodes} onCountryClick={onCountry} />
      </Suspense>

      <p className="text-[11px] leading-relaxed text-fg-subtle">{data.note}</p>

      <section className="space-y-2">
        <h3 className="text-[13px] font-semibold text-fg-muted">件数の多い国</h3>
        <ul className="space-y-1">
          {data.nodes.slice(0, 10).map((n) => (
            <li key={n.iso}>
              <button
                onClick={() => onCountry(n.iso)}
                className="w-full flex items-baseline gap-2 text-left text-[13px] hover:text-accent transition-colors"
              >
                <span className="text-fg">{n.label}</span>
                <span className="flex-1 border-b border-dotted border-border-subtle" />
                <span className="tnum text-fg-muted">{n.count}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
