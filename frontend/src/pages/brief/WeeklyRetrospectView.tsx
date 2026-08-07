// 週次振り返り (過去参照): 旧 /app/retrospect (RetrospectPage) をブリーフページの
// 週次ビューとして verbatim 移設 (2026-07-25 ブリーフ×振り返りの時間軸統合)。
// 「あの週、何が起きていたか」= 当時の週次総括 + 深掘りダイジェスト + 主要記事 +
// 活動アクター + 予測の的中検証。当時のスナップショット性は保持 (記録は改変しない)。

import { useState } from "react";
import { Check, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { formatJst } from "../../utils/date";
import { intentLabel } from "../../utils/diamond";
import { vocabLabel } from "../../hooks/useVocab";
import type { RetrospectResponse } from "../../api/types";

const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

function Section({ title, body }: { title: string; body: string }) {
  if (!body) return null;
  return (
    <div>
      <div className="text-fg-muted text-xs font-semibold mb-1">{title}</div>
      <p className="text-sm text-fg-muted leading-relaxed whitespace-pre-wrap">{body}</p>
    </div>
  );
}

export function WeeklyRetrospectView() {
  const [weeksAgo, setWeeksAgo] = useState(1);
  const { data, isFetching } = useQuery<RetrospectResponse>({
    queryKey: ["retrospect", weeksAgo],
    queryFn: () => api.retrospect(weeksAgo),
  });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-fg-muted text-sm m-0">
          「あの週、何が起きていたか」を週次の状況総括 + 主要記事 + 活動したアクター + 予測の的中結果で再構成 (当時のスナップショット)。
        </p>
        <div className="flex items-center gap-2">
          <button onClick={() => setWeeksAgo((w) => w + 1)}
            className="px-2 py-1 rounded border border-border-default text-fg-muted hover:text-fg text-sm">← 前の週</button>
          <span className="text-sm text-fg font-medium tnum min-w-[120px] text-center">
            {data?.period.label ?? `${weeksAgo} 週前`}
          </span>
          <button onClick={() => setWeeksAgo((w) => Math.max(0, w - 1))} disabled={weeksAgo <= 0}
            className="px-2 py-1 rounded border border-border-default text-fg-muted hover:text-fg text-sm disabled:opacity-40">次の週 →</button>
        </div>
      </div>

      {isFetching && <div className="text-fg-subtle text-sm">読み込み中…</div>}

      {data && !data.has_data && (
        <div className="text-fg-subtle text-sm p-10 text-center bg-surface-1 border border-border-subtle rounded-lg">
          この週のデータはありません。
        </div>
      )}

      {data && data.has_data && (
        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px] gap-5 items-start">
          {/* 左: synthesis + 記事 */}
          <div className="space-y-4">
            {data.synthesis ? (
              <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
                <div className="text-fg-muted text-xs uppercase">この週の状況総括</div>
                <p className="text-sm text-fg font-medium leading-relaxed">{data.synthesis.headline}</p>
                <Section title="重心 (CoG)" body={data.synthesis.cog_section} />
                <Section title="連鎖" body={data.synthesis.chain_section} />
                <Section title="波及予想 (当時)" body={data.synthesis.spillover_section} />
              </div>
            ) : (
              <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 text-fg-subtle text-sm">
                この週の状況総括はありません。
              </div>
            )}

            {data.recap && (
              <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
                <div className="flex items-baseline justify-between gap-2">
                  <div className="text-fg-muted text-xs uppercase">この週の深掘りダイジェスト</div>
                  <span className="text-xs text-fg-subtle tnum">候補 {data.recap.candidate_count} 件から選定</span>
                </div>
                <p className="text-sm text-fg-muted leading-relaxed whitespace-pre-wrap m-0">{data.recap.recap_text}</p>
              </div>
            )}

            <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
              <div className="px-4 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">
                この週の主要記事 {data.top_articles.length} 件
              </div>
              <ul className="divide-y divide-border-subtle">
                {data.top_articles.map((a) => (
                  <li key={a.article_id} className="px-4 py-2 hover:bg-surface-2 transition-colors">
                    <a href={`/app/article/${encodeURIComponent(a.article_id)}`}
                      className="text-sm text-fg hover:text-accent block">{a.title}</a>
                    <div className="flex flex-wrap items-center gap-2 mt-0.5 text-xs">
                      {a.importance && <span className={`font-medium ${IMPORTANCE_TONE[a.importance] || "text-fg-subtle"}`}>{vocabLabel("importance", a.importance)}</span>}
                      {a.category && <span className="text-fg-subtle">{vocabLabel("category", a.category)}</span>}
                      {a.socio_political_intent && <span className="text-fg-subtle">{intentLabel(a.socio_political_intent)}</span>}
                      {a.created_at && <span className="text-fg-subtle ml-auto tnum">{formatJst(a.created_at)}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          </div>

          {/* 右: actor + forecast 的中 */}
          <div className="space-y-4">
            <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">活動したアクター</div>
              {data.top_actors.length === 0 ? (
                <div className="px-3 py-4 text-fg-subtle text-sm text-center">記録なし</div>
              ) : (
                <div className="p-2 flex flex-wrap gap-1.5">
                  {data.top_actors.map((a) => (
                    <a key={a.value} href={`/app/news?pivot_type=actor&pivot_value=${encodeURIComponent(a.value)}`}
                      className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted hover:text-accent hover:border-accent-soft transition-colors">
                      <span className="font-mono">{a.value}</span>
                      <span className="text-fg-subtle tnum">{a.count}</span>
                    </a>
                  ))}
                </div>
              )}
            </div>

            <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">
                この週の予測の的中
              </div>
              {data.forecast_outcomes.length === 0 ? (
                <div className="px-3 py-4 text-fg-subtle text-sm text-center">予測指標なし</div>
              ) : (
                <ul className="divide-y divide-border-subtle">
                  {data.forecast_outcomes.map((o) => (
                    <li key={`${o.scope}-${o.target_value}`} className="flex items-center gap-2 px-3 py-1.5 text-sm">
                      <span className="font-mono text-fg-muted truncate flex-1">{o.target_value}</span>
                      {!o.verified ? (
                        <span className="text-xs text-fg-subtle">未検証</span>
                      ) : o.hit ? (
                        <span className="text-xs text-accent font-semibold inline-flex items-center gap-1">的中 <Check className="h-3.5 w-3.5" /></span>
                      ) : (
                        <span className="text-xs text-fg-subtle inline-flex items-center gap-1">外れ <X className="h-3.5 w-3.5" /></span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
