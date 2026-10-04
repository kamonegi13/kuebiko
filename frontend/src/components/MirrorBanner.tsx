import { useQuery } from "@tanstack/react-query";
import { fetchMirrorMeta } from "../api/mirrorStatic";
import { formatJstCompact } from "../utils/date";
import { SiteSwitch } from "./SiteSwitch";

/** 公開サイト (標準) の入口。写しビルド (VITE_MIRROR=1) 時に build_mirror.sh が
 *  .env の PUBLIC_SITE_ORIGIN から注入する。未設定なら導線を無効表示にする。 */
const PUBLIC_URL: string = import.meta.env.VITE_PUBLIC_ORIGIN || "";

/** 写し (アドバンスド) の上部の帯。公開サイト (標準) と同じ「kuebiko サイバー脅威ニュース」の
 *  表示に、**いつ時点の情報か** と標準 / アドバンスドの切り替えを同じ段に並べる (2026-10-04 利用者指示)。
 *
 *  ⚠ 時点は必ず出す — 古いことではなく、古いと分からないことが危険 (2026-08-29)。
 *  取得できないときも黙らず「時点不明」と出す。ライブ (運用画面) では何も描かない。 */
export function MirrorSwitchBar() {
  const mirror = import.meta.env.VITE_MIRROR === "1";
  const { data, isError } = useQuery({
    queryKey: ["mirror-meta"],
    queryFn: fetchMirrorMeta,
    enabled: mirror,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 10 * 60 * 1000,
  });
  if (!mirror) return null;

  const stamp = isError || !data ? "時点不明" : `${formatJstCompact(data.generated_at)} 現在`;
  return (
    <div className="flex items-baseline gap-2.5 px-4 pt-3 pb-2 border-b border-border">
      <a href="/app" className="text-[17px] font-bold tracking-tight text-fg hover:text-accent">
        kuebiko
      </a>
      <span className="text-[13px] text-fg-subtle">サイバー脅威ニュース</span>
      <span className={`text-[13px] ${isError || !data ? "text-warning" : "text-fg-muted"}`}>{stamp}</span>
      <div className="ml-auto">
        <SiteSwitch current="advanced" otherOrigin={PUBLIC_URL} />
      </div>
    </div>
  );
}
