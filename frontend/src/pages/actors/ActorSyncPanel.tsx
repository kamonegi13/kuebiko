import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi, type ActorSyncProposal } from "../../api/pages";
import { Chips, NationBadge, mitreUrl } from "./ActorDetail";

// MITRE 同期のレビュー提案パネル (Actors Stage 4)。
// 自動適用されない差分 = 新規 actor 追加 (nation 推定の妥当性) と alias 衝突のみが並ぶ。
export function ActorSyncPanel({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  // 一括決裁 (監査 2026-07-16): pending 33 件が 1 件ずつ決裁の摩擦で無決裁滞留した対処。
  // 判断は人のまま、決裁の摩擦だけ下げる (自動承認はしない)。
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const { data, isLoading } = useQuery({
    queryKey: ["actor-sync"],
    queryFn: () => pagesApi.getActorSync(),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["actor-sync"] });
    qc.invalidateQueries({ queryKey: ["actors"] });
  };
  const decide = useMutation({
    mutationFn: ({ id, action }: { id: number; action: "approve" | "reject" }) =>
      pagesApi.decideActorProposal(id, action),
    onSuccess: invalidate,
  });
  const bulkDecide = useMutation({
    mutationFn: async (action: "approve" | "reject") => {
      for (const id of Array.from(selected)) {
        await pagesApi.decideActorProposal(id, action);
      }
    },
    onSuccess: () => {
      setSelected(new Set());
      invalidate();
    },
  });
  const toggleSelected = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };
  const runBulk = (action: "approve" | "reject") => {
    const label = action === "approve" ? "承認" : "却下";
    if (window.confirm(`選択中の ${selected.size} 件を一括${label}します。よろしいですか?`)) {
      bulkDecide.mutate(action);
    }
  };

  if (isLoading) return <div className="text-fg-muted text-sm">読み込み中…</div>;
  const proposals = data?.proposals ?? [];

  return (
    <div className="space-y-4">
      <p className="m-0 text-xs text-fg-subtle">
        週次の MITRE 同期で、別名・マルウェア・出典の追加と要約更新 (和訳) は自動適用されます。
        ここには<strong className="text-fg-muted">判断が必要な差分のみ</strong>
        (新規アクターの nation 推定 / 別名の帰属衝突) が並びます。
        却下した提案は再生成されません。
      </p>
      {data && (
        <p className="m-0 text-xs text-fg-subtle tnum">
          MITRE 同期済 要約: {data.synced_actors}/{data.total_actors} 件
        </p>
      )}

      {proposals.length === 0 && (
        <div className="text-fg-muted text-sm py-6 text-center">レビュー待ちの提案はありません</div>
      )}

      {!readOnly && proposals.length > 1 && (
        <div className="flex items-center gap-2 text-xs">
          <label className="flex items-center gap-1.5 cursor-pointer text-fg-muted">
            <input
              type="checkbox"
              checked={selected.size === proposals.length && proposals.length > 0}
              onChange={() =>
                setSelected(
                  selected.size === proposals.length
                    ? new Set()
                    : new Set(proposals.map((p) => p.id)),
                )
              }
            />
            全選択 ({selected.size}/{proposals.length})
          </label>
          <button
            type="button"
            disabled={selected.size === 0 || bulkDecide.isPending}
            onClick={() => runBulk("approve")}
            className="px-2 py-1 rounded bg-success-soft text-success disabled:opacity-40"
          >
            選択を承認
          </button>
          <button
            type="button"
            disabled={selected.size === 0 || bulkDecide.isPending}
            onClick={() => runBulk("reject")}
            className="px-2 py-1 rounded bg-critical-soft text-critical disabled:opacity-40"
          >
            選択を却下
          </button>
          {bulkDecide.isPending && <span className="text-fg-subtle">一括決裁中…</span>}
        </div>
      )}

      {proposals.map((p) => (
        <div key={p.id} className="flex items-start gap-2">
          {!readOnly && proposals.length > 1 && (
            <input
              type="checkbox"
              className="mt-4 shrink-0"
              checked={selected.has(p.id)}
              onChange={() => toggleSelected(p.id)}
            />
          )}
          <div className="flex-1 min-w-0">
            <ProposalCard
              proposal={p}
              readOnly={readOnly}
              isPending={decide.isPending || bulkDecide.isPending}
              onDecide={(action) => decide.mutate({ id: p.id, action })}
            />
          </div>
        </div>
      ))}
      {decide.isError && (
        <p className="m-0 text-critical text-sm">{(decide.error as Error).message}</p>
      )}
      {bulkDecide.isError && (
        <p className="m-0 text-critical text-sm">{(bulkDecide.error as Error).message}</p>
      )}
    </div>
  );
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x)) : [];
}

function evidence(p: Record<string, unknown>): Record<string, unknown> {
  const e = p._evidence;
  return e && typeof e === "object" ? (e as Record<string, unknown>) : {};
}

function ProposalCard({
  proposal,
  readOnly,
  isPending,
  onDecide,
}: {
  proposal: ActorSyncProposal;
  readOnly: boolean;
  isPending: boolean;
  onDecide: (action: "approve" | "reject") => void;
}) {
  const kind = proposal.proposal_type;
  const isNew = kind === "mitre_new_actor";
  const isCorpus = kind === "corpus_emerging_actor";
  const isNewsAlias = kind === "news_alias";
  const p = proposal.payload;
  const url = mitreUrl(proposal.mitre_group);
  const ev = evidence(p);
  const badgeLabel = isNew
    ? "新規アクター"
    : isCorpus
      ? "新興アクター (報道から検出)"
      : isNewsAlias
        ? "報道由来の別名"
        : "別名衝突";
  return (
    <div className="rounded border border-border-subtle bg-surface-2 p-3 space-y-2">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        <span
          className={`px-1.5 py-0.5 rounded font-semibold ${
            isNew || isCorpus ? "bg-accent-subtle text-accent" : "bg-surface-3 text-fg"
          }`}
        >
          {badgeLabel}
        </span>
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="text-accent-hover font-mono">
            {proposal.mitre_group} ↗
          </a>
        )}
        <span className="text-fg-subtle ml-auto">{proposal.created_at.slice(0, 10)}</span>
      </div>

      {isCorpus ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-fg">{str(p.canonical)}</span>
            <span className="text-xs text-fg-subtle">
              {String(ev.signal) === "vendor_designation" ? "ベンダ命名" : "LLM抽出"}・
              {Number(ev.article_count) || 0} 記事で観測
            </span>
          </div>
          {strList(ev.sample_titles).length > 0 && (
            <ul className="m-0 pl-4 text-xs text-fg-muted leading-relaxed list-disc">
              {strList(ev.sample_titles)
                .slice(0, 3)
                .map((t, i) => (
                  <li key={i} className="line-clamp-1">
                    {t}
                  </li>
                ))}
            </ul>
          )}
          <p className="m-0 text-[11px] text-fg-subtle">
            承認すると辞書に確定登録し、既存の暫定帰属も確定アクターに昇格します。
          </p>
        </div>
      ) : isNew ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-fg">{str(p.canonical)}</span>
            <NationBadge nation={str(p.nation) || null} />
          </div>
          {str(p.summary) && (
            <p className="m-0 text-xs text-fg-muted leading-relaxed line-clamp-4">{str(p.summary)}</p>
          )}
          {strList(p.aliases).length > 0 && (
            <p className="m-0 text-xs text-fg-subtle">別名: {strList(p.aliases).join(", ")}</p>
          )}
          {strList(p.associated_malware).length > 0 && (
            <Chips items={strList(p.associated_malware).slice(0, 8)} />
          )}
        </div>
      ) : isNewsAlias ? (
        <div className="space-y-2">
          <p className="m-0 text-sm text-fg">
            <strong>{str(p.actor_canonical)}</strong> の別名として
            <strong>『{str(p.alias)}』</strong> が報道で併記 ({Number(ev.article_count) || 0} 記事)
          </p>
          {str(ev.excerpt) && (
            <p className="m-0 text-[11px] text-fg-subtle bg-surface-3 rounded px-2 py-1 line-clamp-2">
              …{str(ev.excerpt)}…
            </p>
          )}
          {strList(ev.sample_titles).length > 0 && (
            <ul className="m-0 pl-4 text-xs text-fg-muted leading-relaxed list-disc">
              {strList(ev.sample_titles)
                .slice(0, 3)
                .map((t, i) => (
                  <li key={i} className="line-clamp-1">
                    {t}
                  </li>
                ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="m-0 text-sm text-fg">
          別名 <strong>『{str(p.alias)}』</strong>:
          現在『{str(p.current_owner_canonical)}』 → MITRE は『{str(p.mitre_actor_canonical)}』に帰属
        </p>
      )}

      <p className="m-0 text-xs text-fg-subtle border-l-2 border-border-subtle pl-2">{proposal.rationale}</p>

      {!readOnly && (
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={() => onDecide("approve")}
            disabled={isPending}
            className="px-3 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
          >
            承認して適用
          </button>
          <button
            onClick={() => onDecide("reject")}
            disabled={isPending}
            className="px-3 py-1 rounded text-xs font-medium bg-surface-3 border border-border-subtle text-fg-muted hover:bg-surface-2 disabled:opacity-50"
          >
            却下
          </button>
        </div>
      )}
    </div>
  );
}
