// 統合検索 (unified_search) の結果リスト。NewsPage の検索ビューで使う。
// quick(融合スコア順) → precise(LLM 関連度順) の progressive 表示に対応。

import { Check } from "lucide-react";
import { formatJst } from "../../utils/date";
import { intentLabel, isHypothesisIntent } from "../../utils/diamond";
import { vocabLabel } from "../../hooks/useVocab";
import type { SearchHitItem, SearchResponse } from "../../api/search";

const IMPORTANCE_TONE: Record<string, string> = {
  high: "text-critical",
  medium: "text-warning",
  low: "text-fg-subtle",
};

export function SearchResults({ data, precising }: { data: SearchResponse; precising: boolean }) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
      <div className="px-4 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle flex items-center gap-2 flex-wrap">
        <span>{data.count} 件</span>
        <span className={`normal-case inline-flex items-center gap-1 ${data.reranked ? "text-accent" : "text-fg-subtle"}`}>
          {data.reranked ? <><Check className="h-3.5 w-3.5" /> LLM 精密化済 (関連度順)</> : "クイック (総合スコア順)"}
        </span>
        {precising && <span className="normal-case text-fg-subtle animate-pulse ml-auto">LLM で精密化中…</span>}
      </div>
      {data.results.length === 0 && (
        <div className="px-4 py-6 text-center text-fg-subtle text-sm">該当記事なし (別の表現で試してください)</div>
      )}
      <ul className="divide-y divide-border-subtle">
        {data.results.map((r: SearchHitItem, i) => (
          <li key={`${r.article_id}-${i}`} className="px-4 py-2.5 hover:bg-surface-2 transition-colors">
            <div className="flex items-start gap-3">
              {r.rerank_score != null ? (
                <span className={`tnum text-sm font-bold shrink-0 w-9 text-right ${r.rerank_score >= 7 ? "text-accent" : r.rerank_score >= 4 ? "text-fg" : "text-fg-subtle"}`} title="LLM 関連度 0-10">
                  {r.rerank_score}
                </span>
              ) : (
                <span className="text-fg-subtle shrink-0 w-9 text-right text-xs tnum" title="総合スコア">·</span>
              )}
              <div className="min-w-0 flex-1">
                <a href={`/app/article/${encodeURIComponent(r.article_id)}`} className="text-sm text-fg hover:text-accent block">{r.title}</a>
                {r.reason && <p className="text-[11px] text-accent/80 mt-0.5">↳ {r.reason}</p>}
                {r.summary && <p className="text-xs text-fg-subtle mt-0.5 line-clamp-2">{r.summary}</p>}
                <div className="flex flex-wrap items-center gap-2 mt-1 text-xs">
                  {r.importance && <span className={`font-medium ${IMPORTANCE_TONE[r.importance] || "text-fg-subtle"}`}>{vocabLabel("importance", r.importance)}</span>}
                  {r.category && <span className="text-fg-subtle">{vocabLabel("category", r.category)}</span>}
                  {r.socio_political_intent && <span className="text-fg-subtle">{intentLabel(r.socio_political_intent)}{isHypothesisIntent(r.intent_confidence) ? " (仮説)" : ""}</span>}
                  {r.matched_via.length > 0 && (
                    <span className="text-fg-subtle">
                      一致: {r.matched_via.map((v) => vocabLabel("matched_via", v)).join(" / ")}
                    </span>
                  )}
                  {r.created_at && <span className="text-fg-subtle ml-auto tnum">{formatJst(r.created_at)}</span>}
                </div>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
