import { useQuery } from "@tanstack/react-query";
import { Archive } from "lucide-react";
import { fetchMirrorMeta } from "../api/mirrorStatic";
import { formatJstDate, relativeFromNow } from "../utils/date";
import { SiteSwitch } from "./SiteSwitch";

/** 公開サイト (標準) の入口。写しビルド (VITE_MIRROR=1) 時に build_mirror.sh が
 *  .env の PUBLIC_SITE_ORIGIN から注入する。未設定なら導線を無効表示にする。 */
const PUBLIC_URL: string = import.meta.env.VITE_PUBLIC_ORIGIN || "";

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
    // 帯の時刻自体が古くなる。開きっぱなしでも書き出しに追随させる。
    staleTime: 5 * 60 * 1000,
    refetchInterval: 10 * 60 * 1000,
  });
  if (!mirror) return null;

  // 素性が読めないときも黙らない。**写しなのに写しと言えない**状態が一番危ない。
  if (isError || !data) {
    return (
      <div className="flex items-center gap-2 px-4 py-2 text-[13px] bg-warning-soft border-b border-warning/40 text-fg">
        <Archive size={15} className="shrink-0 text-warning" />
        <span>いつ時点の情報かを取得できませんでした</span>
        <div className="ml-auto">
          <SiteSwitch current="advanced" otherOrigin={PUBLIC_URL} />
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-[13px] bg-warning-soft border-b border-warning/40 text-fg">
      <Archive size={15} className="shrink-0 text-warning" />
      {/* 読み手が要るのは「写しである」ことではなく **いつの情報か**
          (2026-08-29 利用者指摘)。仕組みではなく中身を先に言う。 */}
      <span className="font-semibold">{formatJstDate(data.generated_at)} 現在の情報</span>
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
      <div className="ml-auto">
        <SiteSwitch current="advanced" otherOrigin={PUBLIC_URL} />
      </div>
    </div>
  );
}
