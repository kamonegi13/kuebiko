// 事象ニュース widget — 直近の事象を「独立媒体数つき」で並べる。
// 記事数を裏取りとして見せない (docs/event_news_design.md §3-3)。単独報は
// 「1 媒体のみ」と明示する — I&W では未裏取りの初報こそ最重要でありうるため。
//
// 見出しの click は **その場でドロワーを開く** (一覧ページへ飛ばさない)。
// widget から読み始めて、必要なら原記事ドロワーへ進む、が読み手の動線。
//
// config options は 記事フィード widget (widgets/articles.tsx) と共有
// (category/feed/jp/channel/深刻さ・関連性・政策地政学/表示/期間/件数、
// registry.tsx の ARTICLE_FEED_OPTIONS)。絞り込みの意味論・見出しの組み立ても
// 共有する (components/news/facets.tsx の buildFacetedTitle)。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchEventNews, type EventNewsQuery } from "../../../api/eventnews";
import { Drawer } from "../../../components/Drawer";
import { useChannelMeta } from "../../../components/channel";
import { vocabLabel, useVocabMap } from "../../../hooks/useVocab";
import {
  LevelBadge, buildFacetedTitle, severityFacetFromConfigStrings, severityFacetQueryParams, viewWidgetTitle,
  writeSeverityFacet, type SeverityFacetState,
} from "../../../components/news/facets";
import { useNewsViews } from "../../../components/news/useNewsViews";
import { resolveWidgetViewFilters } from "../../../components/news/widgetViewResolution";
import { toEventNewsPageHref } from "../../../components/news/views";
import { EventNewsDetailBody, SourceChip } from "../../eventnews/EventNewsDetail";
import { WidgetCard, Loading, Empty, WidgetError, cfgNum, cfgStr, type WidgetProps } from "../shared";

const TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

// widget 単体の既定 (registry の defaultConfig が付かない古い保存設定向けの最終 fallback)。
// 旧既定 level_filter="notable" と同じ「注意以上 + 政策・地政学を含める」。
const WIDGET_DEFAULT_SEVERITY = { minSeverity: "S2" as const, relevantOnly: false, includeStrategic: true };

interface EventNewsFilters {
  category: string;
  feed: string;
  channel: string;
  jp: "" | "targeted_affected" | "mentioned";
  per: number;
  sinceHours: number;
  wantSummary: boolean;
  severity: SeverityFacetState;
}

/** widget 保存設定 → 絞り込み値 (記事フィード widget と同じ config key)。
 *  コンポーネントの外に出して、render せずにテストできるようにする。 */
export function resolveEventNewsFilters(config: Record<string, unknown> | undefined): EventNewsFilters {
  const mode = cfgStr(config, "mode", "headline"); // headline | summary
  return {
    category: cfgStr(config, "category", ""),
    feed: cfgStr(config, "feed", ""),
    channel: cfgStr(config, "channel", ""),
    jp: cfgStr(config, "jp", "") as "" | "targeted_affected" | "mentioned",
    per: cfgNum(config, "per", 6),
    sinceHours: cfgNum(config, "since_hours", 0),
    wantSummary: mode === "summary",
    // 深刻さ・関連性・戦略上の重み (2026-10-04)。新 3 facet が無ければ旧 level_filter /
    // 更に古い importance ("high,medium" 既定) を移行する。
    severity: severityFacetFromConfigStrings(
      cfgStr(config, "min_severity", ""),
      cfgStr(config, "relevant_only", ""),
      cfgStr(config, "include_strategic", ""),
      cfgStr(config, "level_filter", ""),
      cfgStr(config, "importance", ""),
      WIDGET_DEFAULT_SEVERITY,
    ),
  };
}

/** fetchEventNews へ渡すクエリ ({@link resolveEventNewsFilters} の結果から、limit 抜きで)。 */
export function eventNewsQueryFromFilters(f: EventNewsFilters): EventNewsQuery {
  return {
    limit: Math.max(f.per * 2, 20),
    category: f.category || undefined,
    feed: f.feed || undefined,
    channel: f.channel || undefined,
    jp: f.jp || undefined,
    since_hours: f.sinceHours || undefined,
    ...severityFacetQueryParams(f.severity),
  };
}

export function EventNewsWidget({ config, mobile }: WidgetProps) {
  // カテゴリ表示は backend 配信 vocab を SSoT に (記事フィード widget と同じ合成)。
  const categoryLabelMap: Record<string, string> = { ...useVocabMap("category"), ...useVocabMap("category_group") };
  const chMeta = useChannelMeta();

  // ビュー選択 (2026-10-04、docs/news_filter_ux.md §4)。view が選ばれていればその絞り込みを
  // 使う。未選択 (旧保存設定も含む) は resolveEventNewsFilters の従来どおりの個別項目
  // (= ad-hoc view) を使うため、既存の保存設定は変更なしで動き続ける。
  const legacy = resolveEventNewsFilters(config);
  const { userViews } = useNewsViews();
  const viewId = cfgStr(config, "view", "");
  const { view, filters } = resolveWidgetViewFilters(viewId, userViews, {
    category: legacy.category,
    feed: legacy.feed,
    channel: legacy.channel,
    jp: legacy.jp,
    minSeverity: legacy.severity.minSeverity,
    relevantOnly: legacy.severity.relevantOnly,
    includeStrategic: legacy.severity.includeStrategic,
    since: String(legacy.sinceHours),
  });
  const { category, feed, channel, jp } = filters;
  const severity: SeverityFacetState = {
    minSeverity: filters.minSeverity, relevantOnly: filters.relevantOnly, includeStrategic: filters.includeStrategic,
  };
  const sinceHours = Number(filters.since) || 0;
  const { per, wantSummary } = legacy;

  const [openId, setOpenId] = useState<string | null>(null);
  const { data, isError } = useQuery({
    queryKey: ["dash-eventnews", category, feed, channel, jp, severity, sinceHours, per],
    queryFn: () => fetchEventNews(eventNewsQueryFromFilters({ category, feed, channel, jp, per, sinceHours, wantSummary, severity })),
    refetchInterval: 5 * 60_000,
  });
  const items = (data?.items ?? []).slice(0, per);
  const channelLabel = channel ? chMeta(channel).label : "";
  // Title = ビュー名 (選ばれていれば)。ad-hoc (旧設定・未選択) は従来どおり絞り込みから組む。
  const title = view ? viewWidgetTitle("事象ニュース", view) : buildFacetedTitle({ category, feed, channelLabel, severity, jp, categoryLabelMap, defaultBase: "事象ニュース" });
  const href = toEventNewsPageHref(filters);

  return (
    <WidgetCard title={title} href={href} linkLabel="すべて →">
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
              {wantSummary && it.preview && (
                <p className={`text-[12.5px] text-fg-muted leading-[1.7] mt-0.5 ${mobile ? "line-clamp-1" : "line-clamp-2"}`}>{it.preview}</p>
              )}
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

export function buildEventNewsHref({ category, feed, channel, severity, jp, sinceHours }: {
  category: string; feed: string; channel: string; severity: SeverityFacetState; jp: string; sinceHours: number;
}): string {
  const q = new URLSearchParams();
  if (category) q.set("category", category);
  if (feed) q.set("feed", feed);
  if (channel) q.set("channel", channel);
  writeSeverityFacet(q, severity);
  if (jp) q.set("jp", jp);
  // EventNewsPage の URL クエリ名は "since_hours" (News ページの "since" とは異なる)。
  if (sinceHours) q.set("since_hours", String(sinceHours));
  const qs = q.toString();
  return qs ? `/app/eventnews?${qs}` : "/app/eventnews";
}
