// 事象ニュース widget — 直近の事象を「独立媒体数つき」で並べる。
// 記事数を裏取りとして見せない (docs/event_news_design.md §3-3)。単独報は
// 「1 媒体のみ」と明示する — I&W では未裏取りの初報こそ最重要でありうるため。
//
// 見出しの click は **その場でドロワーを開く** (一覧ページへ飛ばさない)。
// widget から読み始めて、必要なら原記事ドロワーへ進む、が読み手の動線。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchEventNews } from "../../../api/eventnews";
import { Drawer } from "../../../components/Drawer";
import { vocabLabel } from "../../../hooks/useVocab";
import { LevelBadge, migrateImportanceToLevelFilter } from "../../../components/news/facets";
import { EventNewsDetailBody, SourceChip } from "../../eventnews/EventNewsDetail";
import { WidgetCard, Loading, Empty, WidgetError, cfgNum, cfgStr, type WidgetProps } from "../shared";

const TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

export function EventNewsWidget({ config }: WidgetProps) {
  const per = cfgNum(config, "per", 6);
  // 重要度 6 段階 (2026-10-04)。保存済み widget 設定の旧 "importance"
  // ("high,medium" 既定) は level_filter が未設定のときだけ移行する。
  const levelFilterRaw = cfgStr(config, "level_filter", "");
  const legacyImportance = cfgStr(config, "importance", "");
  const levelFilter = (levelFilterRaw || migrateImportanceToLevelFilter(legacyImportance) || "notable") as
    "" | "top" | "notable" | "relevant";
  const [openId, setOpenId] = useState<string | null>(null);
  const { data, isError } = useQuery({
    queryKey: ["dash-eventnews", levelFilter, per],
    queryFn: () =>
      fetchEventNews({ limit: Math.max(per * 2, 20), level_filter: levelFilter || undefined }),
    refetchInterval: 5 * 60_000,
  });
  const items = (data?.items ?? []).slice(0, per);

  return (
    <WidgetCard title="事象ニュース" href="/app/eventnews" linkLabel="すべて →">
      {isError ? <WidgetError /> : !data ? <Loading /> : items.length === 0 ? (
        <Empty>まだ事象がありません。</Empty>
      ) : (
        <ul className="space-y-2">
          {items.map((it) => (
            <li key={it.id} className="leading-snug">
              <button
                onClick={() => setOpenId(it.id)}
                className="block w-full text-left text-[14.5px] font-semibold leading-[1.55] text-fg hover:text-accent hover:underline"
                title="事象の詳細を開く"
              >
                {it.headline}
              </button>
              <div className="flex flex-wrap items-center gap-2 text-[12px] mt-0.5">
                <span className={TONE[it.importance] ?? "text-fg-subtle"}>
                  {vocabLabel("importance", it.importance)}
                </span>
                <LevelBadge level={it.level} />
                <SourceChip item={it} />
                {it.status === "updated" && (
                  <span className="px-1 rounded bg-accent/15 text-accent">更新</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <Drawer
        isOpen={openId !== null}
        onClose={() => setOpenId(null)}
        // ⚠ ドロワーの題に見出しを入れない。中身の側にも見出しが出るので
        //    同じ文が 2 回並ぶ (公開ページは「記事」と総称にしている)。
        title="事象ニュース"
        widthClass="md:w-[46rem]"
        mobileGutter
        swipeToClose
      >
        {openId && <EventNewsDetailBody id={openId} />}
      </Drawer>
    </WidgetCard>
  );
}
