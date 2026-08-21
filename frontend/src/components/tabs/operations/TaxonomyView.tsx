// Operations tab の sub-view: Taxonomy Review。
// 旧 /app/taxonomy の TaxonomyPage を inline 用に再構成 (page wrapper を除く)。

import { Check, Pause, X } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi, type TaxonomyProposal } from "../../../api/pages";
import { useRuntimeFlags } from "../../../hooks/useRuntimeFlags";
import { formatJstShort } from "../../../utils/date";
import { vocabLabel } from "../../../hooks/useVocab";

// proposal_type (src/taxonomy/proposal_generator.py の GeneratedProposal.proposal_type) →
// 日本語ラベル。内部コード (pattern_N) をそのまま出さない。未知キーは原値 fallback。
const PROPOSAL_TYPE_JA: Record<string, string> = {
  pattern_1: "未分類カテゴリ統合",
  pattern_4: "表記ゆれ (typo)",
  pattern_5: "新興アクター候補",
};

export function TaxonomyView() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["taxonomy"],
    queryFn: () => pagesApi.taxonomyReview(),
  });

  return (
    <div className="space-y-3">
      <p className="text-fg-muted text-sm m-0">
        週次提案される 分野 / アクター別名 の誤字・候補をレビュー。採用すると対象辞書 (victim_sectors / actor_aliases) へ即時適用されます。
      </p>

      <TierSection title="区分1 — 誤字 (自動承認の候補)" tone="success" items={data?.tier_1 || []} qc={qc} />
      <TierSection title="区分2 — 手動確認が必須" tone="accent" items={data?.tier_2 || []} qc={qc} />
      <TierSection title="区分3 — 戦略的な検討" tone="warning" items={data?.tier_3 || []} qc={qc} />

      {data?.recent_reviewed && data.recent_reviewed.length > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
          <div className="px-4 py-2.5 border-b border-border-subtle"><h3 className="m-0 text-md font-semibold text-fg">最近の確認履歴</h3></div>
          <div className="overflow-x-auto"><table className="w-full text-sm">
            <thead className="bg-surface-2 text-fg-muted text-xs uppercase"><tr>
              <th className="text-left px-4 py-2 hidden sm:table-cell">種別</th>
              <th className="text-left px-4 py-2">対象</th>
              <th className="text-left px-4 py-2 hidden md:table-cell">変更内容</th>
              <th className="text-left px-4 py-2">状態</th>
              <th className="text-left px-4 py-2 hidden sm:table-cell">確認日時</th>
            </tr></thead>
            <tbody>
              {data.recent_reviewed.map((p) => (
                <tr key={p.id} className="border-t border-border-subtle">
                  <td className="px-4 py-2 text-fg-muted text-xs hidden sm:table-cell">{PROPOSAL_TYPE_JA[p.proposal_type] ?? p.proposal_type}</td>
                  <td className="px-4 py-2 text-fg font-mono text-xs">{p.target_canonical}</td>
                  <td className="px-4 py-2 text-fg-subtle text-xs hidden md:table-cell">{p.proposed_change.slice(0, 60)}</td>
                  <td className="px-4 py-2"><StatusPill status={p.status} /></td>
                  <td className="px-4 py-2 text-fg-subtle text-xs hidden sm:table-cell">{formatJstShort(p.reviewed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>
      )}
    </div>
  );
}

function TierSection({ title, tone, items, qc }: { title: string; tone: "success" | "accent" | "warning"; items: TaxonomyProposal[]; qc: ReturnType<typeof useQueryClient> }) {
  const tones = {
    success: "border-l-success",
    accent: "border-l-accent",
    warning: "border-l-warning",
  };
  return (
    <div className={`bg-surface-1 border border-border-subtle border-l-[3px] ${tones[tone]} rounded-lg overflow-hidden`}>
      <div className="px-4 py-2.5 border-b border-border-subtle flex justify-between items-center">
        <h3 className="m-0 text-md font-semibold text-fg">{title}</h3>
        <span className="text-fg-subtle text-xs tnum">{items.length} 件 未処理</span>
      </div>
      {items.length === 0 && <div className="px-4 py-6 text-center text-fg-subtle text-sm">該当なし</div>}
      {items.map((p) => <ProposalRow key={p.id} proposal={p} qc={qc} />)}
    </div>
  );
}

function ProposalRow({ proposal: p, qc }: { proposal: TaxonomyProposal; qc: ReturnType<typeof useQueryClient> }) {
  const { read_only } = useRuntimeFlags();
  const act = useMutation({
    mutationFn: (action: "accept" | "reject" | "defer") => pagesApi.taxonomyAction(p.id, action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["taxonomy"] });
      // taxonomy 承認は sector/country を追加しうる → vocab を再取得しラベルを反映。
      qc.invalidateQueries({ queryKey: ["vocabularies"] });
    },
  });

  return (
    <div className="px-4 py-3 border-t border-border-subtle flex items-start gap-3">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-xs text-fg-muted mb-1">
          <span className="font-mono bg-surface-2 px-1.5 py-0.5 rounded text-xs">{PROPOSAL_TYPE_JA[p.proposal_type] ?? p.proposal_type}</span>
          {p.target_canonical && <span className="font-mono text-fg-subtle">{p.target_canonical}</span>}
          <span className="ml-auto">確度: <strong className={p.confidence === "high" ? "text-success" : p.confidence === "medium" ? "text-warning" : "text-fg-muted"}>{vocabLabel("confidence", p.confidence)}</strong></span>
          {p.evidence_count > 0 && <span>· 根拠 {p.evidence_count}</span>}
        </div>
        <ChangeSummary json={p.proposed_change} />
        <div className="text-fg-muted text-xs leading-relaxed">{p.rationale}</div>
        {/* 証拠記事リンク (較正格子 §11-B): 件数だけでなく記事そのものに到達できるようにする。
            /app/article/ リンクはグローバル・クリックインターセプトでサイドピークが開く。 */}
        {p.evidence_ids && p.evidence_ids.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1.5 items-center text-[11px]">
            <span className="text-fg-subtle">証拠記事:</span>
            {p.evidence_ids.map((aid, i) => (
              <a
                key={aid}
                href={`/app/article/${encodeURIComponent(aid)}`}
                className="text-accent hover:underline font-mono"
              >
                [{i + 1}]
              </a>
            ))}
          </div>
        )}
      </div>
      {!read_only && (
        <div className="flex flex-col gap-1 shrink-0">
          <button onClick={() => act.mutate("accept")} disabled={act.isPending} className="bg-success-soft text-success border border-success/40 px-3 py-1 rounded text-xs font-semibold hover:bg-success/20 disabled:opacity-40 inline-flex items-center gap-1"><Check className="h-3.5 w-3.5" /> 採用</button>
          <button onClick={() => act.mutate("reject")} disabled={act.isPending} className="bg-critical-soft text-critical border border-critical/40 px-3 py-1 rounded text-xs font-semibold hover:bg-critical/20 disabled:opacity-40 inline-flex items-center gap-1"><X className="h-3.5 w-3.5" /> 却下</button>
          <button onClick={() => act.mutate("defer")} disabled={act.isPending} className="bg-surface-2 text-fg-muted border border-border-subtle px-3 py-1 rounded text-xs hover:bg-surface-3 disabled:opacity-40 inline-flex items-center gap-1"><Pause className="h-3.5 w-3.5" /> 保留</button>
        </div>
      )}
    </div>
  );
}

// proposed_change (JSON 文字列) を kind 別に人間可読な一文へ。生 JSON は details に退避。
function describeChange(json: string): { text: string; detail?: string } {
  let c: Record<string, unknown>;
  try {
    c = JSON.parse(json) as Record<string, unknown>;
  } catch {
    return { text: json };
  }
  const kind = String(c.kind ?? "");
  const aliases = Array.isArray(c.aliases) ? (c.aliases as string[]).join("、") : "";
  if (kind === "add_alias") {
    return { text: `別名追加: 「${c.alias}」を正式名「${c.canonical}」の別名にする` };
  }
  if (kind === "new_canonical") {
    return {
      text: `新カテゴリ候補: 「${c.display ?? c.suggested_id}」を新設 (id: ${c.suggested_id})`,
      detail: aliases ? `別名: ${aliases}` : undefined,
    };
  }
  if (kind === "new_actor") {
    return {
      text: `新アクター候補: 「${c.canonical}」をアクター別名辞書に登録 (id: ${c.suggested_id})`,
      detail: aliases ? `別名: ${aliases}` : undefined,
    };
  }
  // 未知 kind: key-value を読みやすく列挙 (生 JSON よりはまし)
  const kv = Object.entries(c)
    .filter(([k]) => k !== "kind")
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join("、") : v}`)
    .join(" / ");
  return { text: kv || json };
}

function ChangeSummary({ json }: { json: string }) {
  const c = describeChange(json);
  return (
    <div className="mb-1">
      <div className="text-sm text-fg leading-snug">{c.text}</div>
      {c.detail && <div className="text-xs text-fg-subtle mt-0.5">{c.detail}</div>}
      <details className="mt-1">
        <summary className="text-[10px] text-fg-subtle cursor-pointer hover:text-fg-muted">詳細データを表示</summary>
        <pre className="bg-surface-2 text-fg-subtle text-[11px] p-1.5 rounded font-mono whitespace-pre-wrap break-words mt-1">{json}</pre>
      </details>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  // 色 (map) は UI 固有のスタイルなのでローカルに保持。ラベルは backend vocab
  // "taxonomy_proposal_status" (vocabLabel) を SSoT に解決する。
  const map: Record<string, string> = {
    accepted: "bg-success-soft text-success",
    rejected: "bg-critical-soft text-critical",
    deferred: "bg-surface-3 text-fg-muted",
    pending: "bg-warning-soft text-warning",
  };
  return <span className={`text-xs px-2 py-0.5 rounded font-semibold ${map[status] || "bg-surface-3 text-fg-muted"}`}>{vocabLabel("taxonomy_proposal_status", status)}</span>;
}
