// Operations tab の常設カード: 遅延正解ラベル (較正格子 P1) の件数表示。
// tuning_labels の消費者第 1 号 (write-only 列を作らない規約)。週次収穫
// (weekly-tuning-label-harvest) が蓄積した「後日確定した事実」の供給量を種別ごとに見せる。

import { useQuery } from "@tanstack/react-query";
import { pagesApi } from "../../../api/pages";
import { formatJstShort } from "../../../utils/date";

// 内部コード → 日本語 (生 enum 直接表示禁止の規約)。未知キーは原値 fallback。
const FIELD_JA: Record<string, string> = {
  subject_actor: "主体アクター (犯行声明 突合)",
  editorial_stance: "論調の人手訂正",
  taxonomy_decision: "分類提案の裁定",
  actor_alias: "アクター別名の確定 (MITRE)",
};

const SOURCE_JA: Record<string, string> = {
  E0: "決定論",
  E1: "遅延正解",
  E2: "パネル",
  E3: "人間裁定",
};

// P2: 評価・裁定の種別/結果の日本語写像
const EVAL_KIND_JA: Record<string, string> = {
  goldset_cutover: "goldset 切替評価",
  auto_rollback: "自動 rollback 裁定",
};

const VERDICT_JA: Record<string, string> = {
  pass: "合格",
  degraded: "劣化",
  would_rollback: "戻すべき (シャドー)",
  rolled_back: "復元済み",
};

const VERDICT_TONE: Record<string, string> = {
  pass: "bg-success-soft text-success",
  degraded: "bg-warning-soft text-warning",
  would_rollback: "bg-warning-soft text-warning",
  rolled_back: "bg-critical-soft text-critical",
};

// §11-C: taxonomy 提案の区分ラベル (TaxonomyView と同じ区分体系)
const TIER_JA: Record<string, string> = {
  tier_1_auto: "区分1 (誤字)",
  tier_2_review: "区分2 (手動確認)",
  tier_3_strategic: "区分3 (戦略)",
};

export function TuningLabelsCard() {
  const { data } = useQuery({
    queryKey: ["tuning-labels"],
    queryFn: () => pagesApi.tuningLabels(),
  });

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
      <div className="px-4 py-2.5 border-b border-border-subtle flex justify-between items-center">
        <h3 className="m-0 text-md font-semibold text-fg">遅延正解ラベル (較正格子)</h3>
        <span className="text-fg-subtle text-xs">
          週次収穫 — 後日確定した事実との突合。プロンプト評価と few-shot の恒久資産
        </span>
      </div>
      {(!data || data.summary.length === 0) && (
        <div className="px-4 py-4 text-fg-subtle text-sm">
          まだラベルがありません (週次収穫は水曜深夜。人手操作なしでも蓄積されます)
        </div>
      )}
      {data && data.summary.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-surface-2 text-fg-muted text-xs uppercase">
              <tr>
                <th className="text-left px-4 py-2">種別</th>
                <th className="text-left px-4 py-2">証拠源</th>
                <th className="text-right px-4 py-2">現行</th>
                <th className="text-right px-4 py-2 hidden sm:table-cell">総数</th>
                <th className="text-left px-4 py-2 hidden md:table-cell">最終到着</th>
              </tr>
            </thead>
            <tbody>
              {data.summary.map((r) => (
                <tr key={`${r.field}:${r.source}`} className="border-t border-border-subtle">
                  <td className="px-4 py-2 text-fg text-xs">{FIELD_JA[r.field] ?? r.field}</td>
                  <td className="px-4 py-2">
                    <span className="text-xs px-2 py-0.5 rounded font-semibold bg-surface-3 text-fg-muted font-mono">
                      {r.source} {SOURCE_JA[r.source] ?? ""}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-right text-fg tnum font-semibold">{r.active}</td>
                  <td className="px-4 py-2 text-right text-fg-subtle tnum text-xs hidden sm:table-cell">
                    {r.total}
                  </td>
                  <td className="px-4 py-2 text-fg-subtle text-xs hidden md:table-cell">
                    {formatJstShort(r.last_arrived_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {/* P3: シャドーパネル累計 + taxonomy 区分別人間同意率 (§11-C) */}
      {data && (data.panel?.judged > 0 || (data.taxonomy_agreement ?? []).length > 0) && (
        <div className="border-t border-border-subtle px-4 py-2.5 flex flex-wrap gap-x-6 gap-y-1 text-xs text-fg-muted">
          {data.panel?.judged > 0 && (
            <span>
              パネル裁定 累計 <strong className="text-fg tnum">{data.panel.judged}</strong> 件 · 分裂率{" "}
              <strong className="text-fg tnum">
                {data.panel.split_rate != null ? `${(data.panel.split_rate * 100).toFixed(1)}%` : "-"}
              </strong>
              <span className="text-fg-subtle"> (外部 LLM に上がるはずだった率)</span>
            </span>
          )}
          {(data.taxonomy_agreement ?? []).map((t) => (
            <span key={t.tier}>
              {TIER_JA[t.tier] ?? t.tier} 同意率{" "}
              <strong className="text-fg tnum">
                {t.agreement_rate != null ? `${(t.agreement_rate * 100).toFixed(0)}%` : "-"}
              </strong>
              <span className="text-fg-subtle tnum"> ({t.accepted}/{t.accepted + t.rejected})</span>
            </span>
          ))}
        </div>
      )}
      {/* P2: goldset 評価 / auto-rollback 裁定の履歴 (rubric 変更の翌週に自動で並ぶ) */}
      {data && data.evals && data.evals.length > 0 && (
        <div className="border-t border-border-subtle">
          <div className="px-4 py-2 text-xs text-fg-muted uppercase bg-surface-2">評価・裁定 (P2)</div>
          {data.evals.map((e) => (
            <div key={e.id} className="px-4 py-2 border-t border-border-subtle flex items-center gap-2 text-xs">
              <span className="text-fg">{EVAL_KIND_JA[e.kind] ?? e.kind}</span>
              <span className="text-fg-subtle font-mono">
                {e.prompt_id} v{e.from_version ?? "?"}→v{e.to_version ?? "?"}
              </span>
              <span className={`px-2 py-0.5 rounded font-semibold ${VERDICT_TONE[e.verdict] ?? "bg-surface-3 text-fg-muted"}`}>
                {VERDICT_JA[e.verdict] ?? e.verdict}
              </span>
              {e.mode === "shadow" && <span className="text-fg-subtle">(シャドー)</span>}
              <span className="ml-auto text-fg-subtle">{formatJstShort(e.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
