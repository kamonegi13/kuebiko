// SIR (収集要求) の定義管理 — 設定【定義】タブ (S4)。
//
// 配置基準 (CLAUDE.md §11、2026-09-16):「専用ページは閲覧のみ、定義の変更は設定カテゴリ」。
// `/app/pir` (SIR / Spotlight) は**閲覧面**として残り、KPI と Spotlight を読む。
// 有効化・承認・削除・作成・編集という**定義の変更**はここに集約する。
//
// 移設の前提条件 (docs/settings_consolidation_plan.md §4) を満たすため、この画面は
//   ①効果  … 編集画面の preview (pirApi.preview) が担う
//   ②参照関係 … used_by_questions (この SIR を参照している常設情報要求)
//   ③成績  … match_7d / 30d / last_match
// を揃えて出す。揃えずに移すと、旧基準と同じ「理屈だけ」の状態を設定側に再現する。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ExternalLink, Pencil, Plus, Trash2 } from "lucide-react";

import { pirApi } from "../../api/pir";

function fmtDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

export function SirDefinitionsView() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({ queryKey: ["pir-list"], queryFn: pirApi.list });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["pir-list"] });
  };
  const toggle = useMutation({ mutationFn: (id: string) => pirApi.toggle(id), onSuccess: invalidate });
  const approve = useMutation({
    mutationFn: (id: string) => pirApi.approve(id),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) => pirApi.delete(id),
    onSuccess: invalidate,
  });

  if (isLoading) return <div className="text-sm text-fg-subtle">読み込み中…</div>;
  if (error) return <div className="text-sm text-warning">SIR 一覧の取得に失敗しました</div>;

  const items = data?.priorities ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="m-0 text-lg font-bold text-fg tracking-tight">SIR (収集要求)</h3>
          <span className="text-xs text-fg-subtle">
            {items.length} 件{data?.version !== undefined && ` / v${data.version}`}
          </span>
          <a
            href="/app/pir"
            className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            動向・Spotlight を見る
          </a>
        </div>
        <a
          href="/app/config/sir/edit"
          className="inline-flex items-center gap-1 text-xs text-accent border border-accent/40 rounded px-2 py-1 no-underline"
        >
          <Plus className="h-3.5 w-3.5" /> 新規作成
        </a>
      </div>

      <p className="m-0 text-sm text-fg-muted">
        SIR は「何を集め、何を重要とするか」の定義。配信チャンネルの決定権は
        <a href="/app/flow" className="text-accent hover:underline mx-1">
          情報フロー
        </a>
        が持つ (背骨: SIR → 重要度 → チャンネル)。
      </p>

      <div className="space-y-2">
        {items.map((p) => (
          <div
            key={p.id}
            className="bg-surface-1 border border-border-subtle rounded-lg p-3 space-y-2"
          >
            <div className="flex items-start justify-between gap-3 flex-wrap">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-semibold text-fg">{p.title}</span>
                  <span className="font-mono text-[11px] text-fg-subtle">{p.id}</span>
                  {!p.enabled && (
                    <span className="text-[11px] text-fg-subtle border border-border-default rounded px-1">
                      無効
                    </span>
                  )}
                  {!p.approved_by_user && (
                    <span className="text-[11px] text-warning border border-warning/40 rounded px-1">
                      未承認
                    </span>
                  )}
                </div>
                {/* ③成績: この定義がいま何を拾っているか */}
                <div className="mt-1 text-[12px] text-fg-subtle">
                  該当 7日 {p.match_count_7d} / 30日 {p.match_count_30d} · 最終{" "}
                  {fmtDate(p.last_match_at)}
                </div>
                {/* ②参照関係: 消すと孤児になる先。⚠ 空でも「消してよい」ではない */}
                <div className="mt-0.5 text-[12px] text-fg-subtle">
                  {p.used_by_questions && p.used_by_questions.length > 0 ? (
                    <>参照している問い {p.used_by_questions.length} 件</>
                  ) : (
                    <>どの問いからも参照されていません</>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-1 shrink-0">
                <a
                  href={`/app/config/sir/edit/${encodeURIComponent(p.id)}`}
                  className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent border border-border-subtle rounded px-2 py-1 no-underline"
                >
                  <Pencil className="h-3.5 w-3.5" /> 編集
                </a>
                <button
                  onClick={() => toggle.mutate(p.id)}
                  disabled={toggle.isPending}
                  className="text-xs text-fg-muted hover:text-fg border border-border-subtle rounded px-2 py-1"
                >
                  {p.enabled ? "無効化" : "有効化"}
                </button>
                {!p.approved_by_user && (
                  <button
                    onClick={() => approve.mutate(p.id)}
                    disabled={approve.isPending}
                    className="inline-flex items-center gap-1 text-xs text-accent border border-accent/40 rounded px-2 py-1"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" /> 承認
                  </button>
                )}
                <button
                  onClick={() => {
                    // 参照している問いがあるなら件数を示してから確認する
                    // (消すと SIR リンクが孤児になる)。
                    const refs = p.used_by_questions?.length ?? 0;
                    const note =
                      refs > 0 ? `\n\n⚠ ${refs} 件の問いがこの SIR を参照しています。` : "";
                    if (confirm(`SIR「${p.title}」を削除します。${note}`)) remove.mutate(p.id);
                  }}
                  disabled={remove.isPending}
                  className="inline-flex items-center gap-1 text-xs text-warning hover:text-fg border border-warning/40 rounded px-2 py-1"
                >
                  <Trash2 className="h-3.5 w-3.5" /> 削除
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
