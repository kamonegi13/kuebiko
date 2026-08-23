// 事象ニュース widget — 直近の事象を「独立媒体数つき」で並べる。
// 記事数を裏取りとして見せない (docs/event_news_design.md §3-3)。単独報は
// 「1 媒体のみ」と明示する — I&W では未裏取りの初報こそ最重要でありうるため。

import { useQuery } from "@tanstack/react-query";
import { fetchEventNews } from "../../../api/eventnews";
import { WidgetCard, Loading, Empty, WidgetError, cfgNum, cfgStr, type WidgetProps } from "../shared";

const TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

export function EventNewsWidget({ config }: WidgetProps) {
  const per = cfgNum(config, "per", 6);
  const importance = cfgStr(config, "importance", "high,medium");
  const { data, isError } = useQuery({
    queryKey: ["dash-eventnews", importance, per],
    queryFn: () => fetchEventNews(Math.max(per * 2, 20), importance || undefined),
    refetchInterval: 5 * 60_000,
  });
  const items = (data?.items ?? []).slice(0, per);

  return (
    <WidgetCard title="事象ニュース" href="/app/eventnews" linkLabel="すべて →">
      {isError ? <WidgetError /> : !data ? <Loading /> : items.length === 0 ? (
        <Empty>まだ事象がありません。</Empty>
      ) : (
        <ul className="space-y-2">
          {items.map((it) => {
            const solo = it.independent_sources <= 1;
            return (
              <li key={it.id} className="leading-snug">
                <a href={`/app/eventnews#${it.id}`} className="text-[13px] text-fg hover:text-accent">
                  {it.headline}
                </a>
                <div className="flex flex-wrap items-center gap-2 text-[10px] mt-0.5">
                  <span className={TONE[it.importance] ?? "text-fg-subtle"}>{it.importance}</span>
                  <span className={solo ? "text-warning" : "text-fg-subtle"}>
                    {solo ? "1 媒体のみ・未裏取り" : `独立 ${it.independent_sources} 媒体`}
                  </span>
                  {it.state_media_count > 0 && (
                    <span className="text-critical">国営 {it.state_media_count}</span>
                  )}
                  {it.status === "updated" && (
                    <span className="px-1 rounded bg-accent/15 text-accent">更新</span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </WidgetCard>
  );
}
