// Operations tab の裁定カード (較正格子 P4 C5)。
// 係争 = 「E1 ラベル × パネル不一致」— 90 日 backfill の実測でこの不一致はラベルノイズを
// 誤検出ゼロで指した。人間は「どちらが誤りか」を 1 件ずつ 2 択で裁く (証拠提示・2 択化・
// バッチ承認抑止 — C5 要件)。着弾は月 1-2 件想定のため、待ち 0 件なら履歴のみ表示。

import { Check, X } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi, type AdjudicationCase } from "../../../api/pages";
import { useRuntimeFlags } from "../../../hooks/useRuntimeFlags";
import { formatJstShort } from "../../../utils/date";

const RESOLUTION_JA: Record<string, string> = {
  label_wrong: "ラベルが誤り",
  label_correct: "ラベルが正しい (判定の見逃し)",
  expired: "期限切れ (自動アーカイブ)",
};

function panelValues(verdictsJson: string): string {
  try {
    const parsed = JSON.parse(verdictsJson) as { model: string; value: string }[];
    return parsed.map((v) => `${v.model}: ${v.value || "(なし)"}`).join(" / ");
  } catch {
    return verdictsJson;
  }
}

export function AdjudicationCard() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["tuning-labels"],
    queryFn: () => pagesApi.tuningLabels(),
  });
  const pending = data?.adjudication?.pending ?? [];
  const recent = data?.adjudication?.recent ?? [];

  if (pending.length === 0 && recent.length === 0) return null; // 自己隠蔽 (着弾は稀)

  return (
    <div className="bg-surface-1 border border-border-subtle border-l-[3px] border-l-warning rounded-lg overflow-hidden">
      <div className="px-4 py-2.5 border-b border-border-subtle flex justify-between items-center">
        <h3 className="m-0 text-md font-semibold text-fg">係争の裁定 (較正格子)</h3>
        <span className="text-fg-subtle text-xs tnum">{pending.length} 件 裁定待ち</span>
      </div>
      {pending.length > 0 && (
        <p className="px-4 pt-2 pb-0 m-0 text-fg-muted text-xs">
          遅延正解ラベル (E1) とパネル判定が食い違った事例です。証拠記事を開き、どちらが誤りかを裁定してください。
          裁定は即時適用されます (ラベル誤り = E1 を隔離)。無応答は 30 日で保守側に自動アーカイブ。
        </p>
      )}
      {pending.map((c) => (
        <CaseRow key={c.case_key} c={c} qc={qc} />
      ))}
      {recent.length > 0 && (
        <div className="border-t border-border-subtle">
          <div className="px-4 py-1.5 text-[10px] text-fg-subtle uppercase bg-surface-2">裁定履歴</div>
          {recent.map((r) => (
            <div key={r.case_key} className="px-4 py-1.5 border-t border-border-subtle flex items-center gap-2 text-xs">
              <span className="text-fg-subtle font-mono">{r.truth_value || "-"}</span>
              <span className="text-fg-muted">{RESOLUTION_JA[r.resolution] ?? r.resolution}</span>
              {r.resolved_by === "ttl" && <span className="text-fg-subtle">(TTL)</span>}
              <span className="ml-auto text-fg-subtle">{formatJstShort(r.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CaseRow({ c, qc }: { c: AdjudicationCase; qc: ReturnType<typeof useQueryClient> }) {
  const { read_only } = useRuntimeFlags();
  const act = useMutation({
    mutationFn: (resolution: "label_wrong" | "label_correct") =>
      pagesApi.tuningAdjudicate(c.case_key, resolution),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tuning-labels"] }),
  });

  return (
    <div className="px-4 py-3 border-t border-border-subtle flex items-start gap-3">
      <div className="flex-1 min-w-0">
        <div className="text-sm text-fg leading-snug mb-1">
          {c.article_id ? (
            <a
              href={`/app/article/${encodeURIComponent(c.article_id)}`}
              className="text-accent hover:underline"
            >
              {c.title || c.article_id}
            </a>
          ) : (
            <span>{c.title || "(記事なし)"}</span>
          )}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-fg-muted">
          <span>
            遅延正解 (E1):{" "}
            <strong className="text-fg font-mono">{c.truth_value || "(なし)"}</strong>
          </span>
          <span>
            パネル: <span className="font-mono">{panelValues(c.verdicts)}</span>
          </span>
          <span>
            本番判定: <span className="font-mono">{c.production_value || "(なし)"}</span>
          </span>
          <span className="text-fg-subtle">{formatJstShort(c.created_at)}</span>
        </div>
      </div>
      {!read_only && (
        <div className="flex flex-col gap-1 shrink-0">
          <button
            onClick={() => act.mutate("label_wrong")}
            disabled={act.isPending}
            className="bg-critical-soft text-critical border border-critical/40 px-3 py-1 rounded text-xs font-semibold hover:bg-critical/20 disabled:opacity-40 inline-flex items-center gap-1"
          >
            <X className="h-3.5 w-3.5" /> ラベルが誤り
          </button>
          <button
            onClick={() => act.mutate("label_correct")}
            disabled={act.isPending}
            className="bg-success-soft text-success border border-success/40 px-3 py-1 rounded text-xs font-semibold hover:bg-success/20 disabled:opacity-40 inline-flex items-center gap-1"
          >
            <Check className="h-3.5 w-3.5" /> ラベルが正しい
          </button>
        </div>
      )}
    </div>
  );
}
