import { useQuery } from "@tanstack/react-query";
import { Archive } from "lucide-react";
import { fetchMirrorMeta } from "../api/mirrorStatic";
import { formatJstDate, relativeFromNow } from "../utils/date";

/** 「これは写しである」を常時出す帯。
 *
 *  ⚠ **これが無いと写しは危険になる。** ライブと同じ見た目のまま古い情報を出すと、
 *  いつの状態を見ているか判断できない。Mac に到達できないときの継続が目的なので、
 *  「古い」こと自体は問題ではない — **古いと分からないこと**が問題。
 *
 *  写しビルドでのみ描く。ライブでは何も出さない (VITE_MIRROR で切り替わる)。
 */
export function MirrorBanner() {
  const mirror = import.meta.env.VITE_MIRROR === "1";
  const { data, isError } = useQuery({
    queryKey: ["mirror-meta"],
    queryFn: fetchMirrorMeta,
    enabled: mirror,
    staleTime: Infinity,
  });
  if (!mirror) return null;

  // 素性が読めないときも黙らない。**写しなのに写しと言えない**状態が一番危ない。
  if (isError || !data) {
    return (
      <div className="flex items-center gap-2 px-4 py-2 text-[13px] bg-warning-soft border-b border-warning/40 text-fg">
        <Archive size={15} className="shrink-0 text-warning" />
        <span>これは保存された写しです (いつ時点かを取得できませんでした)</span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-[13px] bg-warning-soft border-b border-warning/40 text-fg">
      <Archive size={15} className="shrink-0 text-warning" />
      <span className="font-semibold">
        {formatJstDate(data.generated_at)} 時点の写し
      </span>
      <span className="text-fg-muted">{relativeFromNow(data.generated_at)}</span>
      <span className="text-fg-muted">
        記事 {data.counts.articles.toLocaleString()} 件 / 事象{" "}
        {data.counts.eventnews.toLocaleString()} 件 (直近 {data.window_days} 日)
      </span>
      {!data.with_bodies && (
        // 「取得に失敗した」と「そもそも写していない」は別物。混同すると
        // 壊れていると思って調べに行くことになる。
        <span className="text-fg-muted">記事本文は含まれません</span>
      )}
    </div>
  );
}
