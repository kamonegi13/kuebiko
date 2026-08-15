// 購読ソース画面で一覧と詳細ビューが共有する型と小コンポーネント。
// SubscriptionsPage.tsx / FeedDetailView.tsx の双方から参照する (循環 import を避ける)。
import type { FeedStats, Subscription } from "../../api/pages";

export type GroupKey =
  | "fetch_error"
  | "body_error"
  | "warn"
  | "high_value"
  | "normal"
  | "quiet"
  | "no_data";

export interface EnrichedFeed extends Subscription {
  stats?: FeedStats;
  group: GroupKey;
}

export const LOW_CONTRIB_LABELS: Record<string, string> = {
  new: "新規",
  "no-articles": "記事なし",
  "no-posts": "投稿なし",
  "low-volume": "低頻度",
  "high-dup": "重複多",
  "watch-only": "観察止まり",
};

export function QualityBadge({ score }: { score: number | undefined }) {
  if (score === undefined) return <span className="text-fg-subtle">-</span>;
  const tone =
    score >= 70
      ? "text-success bg-success-soft border-success/40"
      : score >= 40
        ? "text-warning bg-warning-soft border-warning/40"
        : "text-critical bg-critical-soft border-critical/40";
  return (
    <span
      className={`inline-block min-w-[2.2rem] text-center px-1.5 py-0.5 rounded text-[10px] font-semibold border ${tone}`}
    >
      {score}
    </span>
  );
}
