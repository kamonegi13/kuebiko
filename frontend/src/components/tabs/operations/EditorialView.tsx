// Operations tab の sub-view: Editorial Quality。
// 旧 /app/editorial の EditorialPage を inline 用に再構成 (page wrapper を除く)。
// Phase 2: actor_id filter (Threats drawer 連動) を accept する。

import { useState } from "react";
import { Pencil } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi } from "../../../api/pages";
import { useRuntimeFlags } from "../../../hooks/useRuntimeFlags";
import { formatJstShort } from "../../../utils/date";
import { useChannelMeta } from "../../channel";
import { vocabLabel } from "../../../hooks/useVocab";

interface EditorialViewProps {
  /** Phase 2 (Drawer 連動): 特定 actor の関連 article のみ表示する場合に指定 */
  actorFilter?: string;
}

export function EditorialView({ actorFilter }: EditorialViewProps = {}) {
  const qc = useQueryClient();
  const [stanceFilter, setStanceFilter] = useState("");
  const [feedFilter, setFeedFilter] = useState("");
  const [lookback, setLookback] = useState(14);

  const { data } = useQuery({
    queryKey: ["editorial", lookback, stanceFilter, feedFilter, actorFilter],
    queryFn: () => pagesApi.editorialQuality({
      lookback_days: lookback,
      stance_filter: stanceFilter,
      feed_filter: feedFilter,
    }),
  });

  return (
    <div className="space-y-3">
      <p className="text-fg-muted text-sm m-0">
        LLM が判定した論調をフィード別に集計し、誤分類を手動で印付け。分類精度の観察用。
        {actorFilter && <span className="ml-2 text-accent">(アクター: {actorFilter} に限定)</span>}
      </p>

      {/* Stance totals */}
      <div className="grid grid-cols-5 gap-3">
        {data?.stances.map((s) => (
          <div key={s} className={`bg-surface-1 border border-border-subtle border-l-[3px] rounded-lg p-3 text-center ${
            s === "propaganda" ? "border-l-critical" :
            s === "factual_report" ? "border-l-accent" :
            s === "analytical" ? "border-l-success" :
            s === "opinion" ? "border-l-warning" : "border-l-fg-subtle"
          }`}>
            <div className="text-xs text-fg-muted">{vocabLabel("stance", s)}</div>
            <div className="text-xl font-bold text-fg mt-1 tnum">{data.stance_totals[s] || 0}</div>
          </div>
        ))}
      </div>
      <div className="text-xs text-fg-subtle">合計: {data?.total_articles || 0} 件 ({lookback} 日)</div>

      {/* Feed × stance crosstab */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <div className="px-4 py-2.5 border-b border-border-subtle"><h3 className="m-0 text-md font-semibold text-fg">フィード×論調 (プロパガンダ比率順)</h3></div>
        <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead className="bg-surface-2 text-fg-muted text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-2">フィード</th>
              <th className="text-right px-4 py-2 hidden sm:table-cell">合計</th>
              {data?.stances.map((s) => <th key={s} className="text-right px-4 py-2 hidden md:table-cell">{vocabLabel("stance", s)}</th>)}
              <th className="text-right px-4 py-2">プロパガンダ %</th>
            </tr>
          </thead>
          <tbody>
            {data?.feed_rows.slice(0, 30).map((r) => (
              <tr key={r.feed} className="border-t border-border-subtle hover:bg-surface-2">
                <td className="px-4 py-2 text-fg text-xs">{r.feed.slice(0, 50)}</td>
                <td className="px-4 py-2 text-right text-fg tnum hidden sm:table-cell">{r.total}</td>
                {data.stances.map((s) => <td key={s} className="px-4 py-2 text-right text-fg-muted tnum text-xs hidden md:table-cell">{r.counts[s] || 0}</td>)}
                <td className="px-4 py-2 text-right tnum text-critical font-semibold">{(r.propaganda_ratio * 100).toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </div>

      {/* Filter + article list */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 flex gap-3 flex-wrap items-center">
        <label className="text-xs uppercase text-fg-muted">遡る日数</label>
        <input type="number" min={1} max={60} value={lookback} onChange={(e) => setLookback(parseInt(e.target.value) || 14)} className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm w-20 tnum" />
        <label className="text-xs uppercase text-fg-muted ml-3">論調</label>
        <select value={stanceFilter} onChange={(e) => setStanceFilter(e.target.value)} className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm">
          <option value="">(すべて)</option>
          {data?.stances.map((s) => <option key={s} value={s}>{vocabLabel("stance", s)}</option>)}
        </select>
        <label className="text-xs uppercase text-fg-muted ml-3">フィード</label>
        <input type="text" value={feedFilter} onChange={(e) => setFeedFilter(e.target.value)} placeholder="完全一致で入力" className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm flex-1" />
      </div>

      <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <div className="overflow-x-auto"><table className="w-full text-sm">
          <thead className="bg-surface-2 text-fg-muted text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-2 hidden sm:table-cell">日時</th>
              <th className="text-left px-4 py-2 hidden md:table-cell">フィード</th>
              <th className="text-left px-4 py-2">タイトル</th>
              <th className="text-left px-4 py-2">LLM 判定</th>
              <th className="text-left px-4 py-2">担当者の訂正</th>
            </tr>
          </thead>
          <tbody>
            {data?.articles
              .filter((a) => !actorFilter || (a.title + " " + (a.feed_title || "")).toLowerCase().includes(actorFilter.toLowerCase()))
              .map((a) => (
                <ReviewRow key={a.article_id} article={a} stances={data.stances} qc={qc} />
              ))}
          </tbody>
        </table></div>
      </div>
    </div>
  );
}

function ReviewRow({ article: a, stances, qc }: { article: import("../../../api/pages").EditorialArticle; stances: string[]; qc: ReturnType<typeof useQueryClient> }) {
  const { read_only } = useRuntimeFlags();
  const chMeta = useChannelMeta();
  const [corrected, setCorrected] = useState(a.review?.corrected_stance || a.stance);
  const [comment, setComment] = useState(a.review?.comment || "");

  const review = useMutation({
    mutationFn: () => pagesApi.editorialReview({ article_id: a.article_id, original_stance: a.stance, corrected_stance: corrected, comment }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["editorial"] }),
  });

  return (
    <tr className="border-t border-border-subtle align-top">
      <td className="px-4 py-2 text-fg-subtle text-xs whitespace-nowrap hidden sm:table-cell">{formatJstShort(a.created_at)}</td>
      <td className="px-4 py-2 text-fg-muted text-xs hidden md:table-cell">{a.feed_title.slice(0, 18)}</td>
      <td className="px-4 py-2"><a href={a.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline text-xs">{a.title.slice(0, 70)}</a><div className="text-fg-subtle text-[10px] mt-0.5">[{a.importance ? vocabLabel("importance", a.importance) : "-"}] [{a.category ? vocabLabel("category", a.category) : "-"}] → {a.posted_channel ? chMeta(a.posted_channel).label : "-"}</div></td>
      <td className="px-4 py-2"><StanceBadge stance={a.stance} /></td>
      <td className="px-4 py-2">
        {a.review && <div className="text-fg-subtle text-[10px] mb-1 inline-flex items-center gap-1"><Pencil className="h-3.5 w-3.5" /><strong>{vocabLabel("stance", a.review.corrected_stance)}</strong>{a.review.comment && ` — ${a.review.comment.slice(0, 30)}`}</div>}
        {!read_only && (
          <div className="flex gap-1 items-center">
            <select value={corrected} onChange={(e) => setCorrected(e.target.value)} className="bg-surface-2 border border-border-subtle rounded px-1.5 py-0.5 text-xs">
              {stances.map((s) => <option key={s} value={s}>{vocabLabel("stance", s)}</option>)}
            </select>
            <input type="text" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="コメント" className="bg-surface-2 border border-border-subtle rounded px-1.5 py-0.5 text-xs w-32" />
            <button onClick={() => review.mutate()} disabled={review.isPending} className="bg-accent text-bg px-2 py-0.5 rounded text-xs font-semibold hover:bg-accent-hover disabled:opacity-40">訂正</button>
          </div>
        )}
      </td>
    </tr>
  );
}

function StanceBadge({ stance }: { stance: string }) {
  const map: Record<string, string> = {
    factual_report: "bg-accent-subtle text-accent-hover",
    analytical: "bg-success-soft text-success",
    opinion: "bg-warning-soft text-warning",
    propaganda: "bg-critical-soft text-critical",
    unknown: "bg-surface-3 text-fg-muted",
  };
  return <span className={`text-xs px-2 py-0.5 rounded font-semibold ${map[stance] || "bg-surface-3 text-fg-muted"}`}>{vocabLabel("stance", stance)}</span>;
}
