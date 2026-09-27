// この記事を証拠にしている情勢 (2026-09-27)。記事から台帳への導線。
// 弱い証拠・統合済みの墓標は backend が外す (/api/v1/situations/by-article)。

import { useQuery } from "@tanstack/react-query";
import { vocabLabel } from "../../hooks/useVocab";
import { situationHref } from "../../utils/intelNav";
import { situationsApi } from "../../api/situations";

const TRACK_LABELS: Record<string, string> = { actor: "アクター追跡", campaign: "キャンペーン" };

export function ArticleSituations({ articleId }: { articleId: string }) {
  const { data } = useQuery({
    queryKey: ["situationsByArticle", articleId],
    queryFn: () => situationsApi.byArticle(articleId),
  });
  const items = data?.situations ?? [];
  if (items.length === 0) return null;
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-3.5">
      <h4 className="m-0 mb-2 text-[12px] text-fg-subtle uppercase tracking-wider font-semibold">
        この記事を証拠にしている情勢
      </h4>
      <ul className="m-0 p-0 list-none space-y-1.5">
        {items.map((s) => (
          <li key={s.situation_id} className="text-sm flex flex-wrap items-baseline gap-2">
            <a href={situationHref(s.situation_id)} className="text-fg hover:text-accent min-w-0">
              {s.title}
            </a>
            <span className="text-[12px] text-fg-subtle">
              {vocabLabel("situation_status", s.status)}
              {s.track && TRACK_LABELS[s.track] && ` ・ ${TRACK_LABELS[s.track]}`}
              {s.polarity && ` ・ ${vocabLabel("polarity", s.polarity)}`}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
