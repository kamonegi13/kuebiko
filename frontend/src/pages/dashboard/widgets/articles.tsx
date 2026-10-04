// 記事フィード widget (柔軟エンジン)。category/feed/channel/importance/mode を config で
// 切り替え、脆弱性 / 特定サイト / ニュースサマリー / ヘッドライン 等の preset に化ける。
// backend: /api/v1/articles (src/ui/api/articles_feed.py)。

import { useQuery } from "@tanstack/react-query";
import { articlesApi } from "../../../api/articles";
import { formatJstCompact } from "../../../utils/date";
import { useChannelMeta } from "../../../components/channel";
import { label } from "../../../utils/labels";
import { useVocabMap } from "../../../hooks/useVocab";
import { LevelBadge, migrateImportanceToLevelFilter } from "../../../components/news/facets";
import {
  WidgetCard, Loading, Empty, WidgetError, ImportanceDot, extractCves, cfgStr, cfgNum, type WidgetProps,
} from "../shared";

export function ArticleFeedWidget({ config, mobile }: WidgetProps) {
  const chMeta = useChannelMeta();
  // カテゴリ表示は backend 配信 vocab を SSoT に。個別カテゴリ + 合成カテゴリ (vuln/threat/
  // incident_breach) を 1 つの写像として合成し、未知キーは原値 fallback。
  const categoryLabelMap: Record<string, string> = { ...useVocabMap("category"), ...useVocabMap("category_group") };
  const category = cfgStr(config, "category", "");
  const feed = cfgStr(config, "feed", "");
  const channel = cfgStr(config, "channel", "");
  // 重要度 6 段階 (2026-10-04)。保存済み widget 設定の旧 "importance" (high/medium/low)
  // は level_filter が未設定のときだけ移行する (level_filter が明示的に保存されていれば
  // そちらを優先)。
  const levelFilterRaw = cfgStr(config, "level_filter", "");
  const legacyImportance = cfgStr(config, "importance", "");
  const levelFilter = (levelFilterRaw || migrateImportanceToLevelFilter(legacyImportance)) as
    "" | "top" | "notable" | "relevant";
  const mode = cfgStr(config, "mode", "headline"); // headline | summary
  const per = cfgNum(config, "per", 8);
  const sinceHours = cfgNum(config, "since_hours", 0);
  const jp = cfgStr(config, "jp", "") as "" | "targeted_affected" | "mentioned";
  const wantSummary = mode === "summary";

  const { data, isError } = useQuery({
    queryKey: ["article-feed", category, feed, channel, levelFilter, jp, wantSummary, per, sinceHours],
    queryFn: () => articlesApi.list({
      category: category || undefined,
      feed: feed || undefined,
      channel: channel || undefined,
      level_filter: levelFilter || undefined,
      jp: jp || undefined,
      status: "posted",
      since_hours: sinceHours || undefined,
      limit: per,
      include_summary: wantSummary,
    }),
    refetchInterval: 2 * 60_000,
  });

  const arts = data?.articles ?? [];
  // チャンネル名は useChannelMeta (SSoT) で解決 (未登録 id は原値 fallback)。
  const channelLabel = channel ? chMeta(channel).label : "";
  const title = buildTitle({ category, feed, channelLabel, levelFilter, categoryLabelMap });
  // widget の絞り込みをそのまま引き継いで News ページへ deep-link
  const href = buildNewsHref({ category, feed, channel, levelFilter, sinceHours, jp });

  return (
    <WidgetCard title={title} href={href} linkLabel="記事一覧 →">
      {isError ? <WidgetError /> : !data ? <Loading /> : arts.length === 0 ? <Empty>該当記事がありません。</Empty> : (
        <ul className="divide-y divide-border-subtle">
          {arts.map((a) => {
            const cves = a.title ? extractCves(a.title) : [];
            return (
              <li key={a.id ?? a.article_id} className="flex items-start gap-2 py-2.5 first:pt-0">
                <ImportanceDot importance={a.importance} className="mt-[7px] w-1.5 h-1.5" />
                <div className="flex-1 min-w-0">
                  {/* L2 タイトル (主役): 強 (13.5px / medium)。クリックで in-app 記事詳細 (CTI 分析付き)。 */}
                  <a href={`/app/article/${encodeURIComponent(a.article_id)}`}
                    className="block text-[14.5px] font-semibold leading-[1.55] text-fg hover:text-accent hover:underline line-clamp-2" title={a.title}>
                    {a.title}
                  </a>
                  {/* L3 要約 (補助): 淡 (11px / muted)。mobile は 1 行で密度を抑える */}
                  {wantSummary && a.summary && (
                    <p className={`text-[12.5px] text-fg-muted leading-[1.7] mt-0.5 ${mobile ? "line-clamp-1" : "line-clamp-2"}`}>{a.summary}</p>
                  )}
                  {/* L4 メタ: 弱 (10px / subtle) */}
                  <div className="text-[12px] text-fg-subtle flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-1">
                    {cves.length > 0 && cves.slice(0, 3).map((c) => (
                      <a key={c} href={`https://nvd.nist.gov/vuln/detail/${c}`} target="_blank" rel="noopener noreferrer"
                        className="px-1 rounded bg-critical-soft text-critical font-mono hover:underline">{c}</a>
                    ))}
                    <LevelBadge level={a.level} />
                    <span className="truncate max-w-[180px]">{a.feed_title}</span>
                    {a.posted_channel && (
                      <span className="shrink-0">{chMeta(a.posted_channel).label}</span>
                    )}
                    {(a.published_at ?? a.created_at) && (
                      <span className="shrink-0 ml-auto tnum" title={a.published_at ? "公開時刻" : "取得時刻 (公開時刻不明)"}>
                        {formatJstCompact(a.published_at ?? a.created_at)}
                      </span>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </WidgetCard>
  );
}

function buildNewsHref({ category, feed, channel, levelFilter, sinceHours, jp }: {
  category: string; feed: string; channel: string; levelFilter: string; sinceHours: number; jp: string;
}): string {
  const q = new URLSearchParams();
  if (category) q.set("category", category);
  if (feed) q.set("feed", feed);
  if (channel) q.set("channel", channel);
  if (levelFilter) q.set("level_filter", levelFilter);
  if (jp) q.set("jp", jp);
  if (sinceHours) q.set("since", String(sinceHours));
  const qs = q.toString();
  return qs ? `/app/news?${qs}` : "/app/news";
}

function buildTitle({ category, feed, channelLabel, levelFilter, categoryLabelMap }: {
  category: string; feed: string; channelLabel: string; levelFilter: string; categoryLabelMap: Record<string, string>;
}): string {
  if (feed) return feed;
  const cat = label(categoryLabelMap, category) || category;
  const base = category === "vuln" ? "脆弱性情報"
    : category === "threat" ? "脅威情報"
    : category === "incident_breach" ? "侵害・インシデント"
    : category === "geopolitical" ? "地政情勢"
    : category === "research" ? "研究ウォッチ"
    : category ? cat
    : "最新ニュース";
  const tags: string[] = [];
  if (levelFilter === "top") tags.push("重大");
  else if (levelFilter === "notable") tags.push("注意以上");
  else if (levelFilter === "relevant") tags.push("関連性あり");
  if (channelLabel) tags.push(channelLabel);
  return tags.length > 0 ? `${base} (${tags.join(" · ")})` : base;
}
