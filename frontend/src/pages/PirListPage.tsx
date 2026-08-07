// PIR 一覧画面 (/app/pir)。
// - 全 PIR の table 形式表示 + KPI snapshot (match_7d / match_30d / last_match)
// - draft (未 approve) PIR の banner
// - 個別 toggle (enabled on/off)
// - 新規追加 / 詳細表示への遷移

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Info, Pause, Play } from "lucide-react";
import { pageContainer } from "../components/Page";
import { pirApi, type PirListItem } from "../api/pir";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { formatJstShort } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";

// PIR 固有の値 "auto" は共通 SSoT に無い (PIR 以外では使わない値のため)。
function importanceOrAuto(v: string): string {
  return v === "auto" ? "自動" : vocabLabel("importance", v);
}

export function PirListPage() {
  const qc = useQueryClient();
  const { read_only } = useRuntimeFlags();
  const { data, isLoading } = useQuery({
    queryKey: ["pir-list"],
    queryFn: () => pirApi.list(),
    refetchInterval: 60_000,
  });

  const toggleMut = useMutation({
    mutationFn: (id: string) => pirApi.toggle(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pir-list"] }),
  });

  const items = data?.priorities || [];
  const pendingDraft = data?.pending_draft_count || 0;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">PIR (Priority Intelligence Requirements)</h2>
        <div className="flex items-center gap-2">
          <span className="text-fg-subtle text-xs">{items.length} PIRs · 60s 自動更新</span>
          {!read_only && (
            <a
              href="/app/pir/edit"
              className="bg-accent hover:bg-accent-hover text-fg rounded px-3 py-1.5 text-xs font-semibold no-underline"
            >
              + 新規 PIR
            </a>
          )}
        </div>
      </div>

      {/* Draft approval banner */}
      {pendingDraft > 0 && (
        <div className="bg-warning-soft border border-warning rounded-md px-4 py-2.5 text-warning text-sm flex items-center gap-2">
          <Info className="h-4 w-4 shrink-0" /> {pendingDraft} 件の未承認の PIR が承認待ちです。詳細画面で内容を確認 → 「承認」してください。
        </div>
      )}

      {isLoading && (
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-6 text-fg-muted text-sm text-center">
          読み込み中...
        </div>
      )}

      {!isLoading && items.length === 0 && (
        <div className="bg-surface-1 border border-dashed border-border-default rounded-lg p-10 text-center text-fg-muted">
          <p className="m-0">まだ PIR がありません。</p>
          {!read_only && (
            <p className="m-0 mt-2">
              <a href="/app/pir/edit" className="text-accent hover:underline">+ 新規 PIR を作成</a>
            </p>
          )}
        </div>
      )}

      {items.length > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-surface-2 text-fg-muted text-[10.5px] uppercase tracking-wider">
              <tr>
                <th className="text-left px-3 py-2.5 font-semibold">PIR</th>
                <th className="text-left px-3 py-2.5 font-semibold w-24">状態</th>
                <th className="text-left px-3 py-2.5 font-semibold w-28 hidden md:table-cell">重要度</th>
                <th className="text-right px-3 py-2.5 font-semibold w-16 hidden sm:table-cell">7d</th>
                <th className="text-right px-3 py-2.5 font-semibold w-16 hidden sm:table-cell">30d</th>
                <th className="text-left px-3 py-2.5 font-semibold w-40 hidden lg:table-cell">最終一致</th>
                <th className="text-right px-3 py-2.5 font-semibold w-20">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <PirRow key={p.id} pir={p} onToggle={() => toggleMut.mutate(p.id)} readOnly={read_only} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PirRow({
  pir,
  onToggle,
  readOnly,
}: {
  pir: PirListItem;
  onToggle: () => void;
  readOnly: boolean;
}) {
  const detailUrl = `/app/pir/${encodeURIComponent(pir.id)}`;
  return (
    <tr className="border-t border-border-subtle hover:bg-surface-2">
      <td className="px-3 py-2.5">
        <div>
          <a href={detailUrl} className="text-fg font-semibold no-underline hover:text-accent-hover">
            {pir.title}
          </a>
          {!pir.approved_by_user && (
            <span className="ml-2 text-[9px] uppercase bg-warning-soft text-warning px-1.5 py-0.5 rounded font-mono">
              未承認
            </span>
          )}
          {pir.enabled && pir.match_count_30d === 0 && (
            <span
              className="ml-2 text-[9px] bg-critical-soft text-critical px-1.5 py-0.5 rounded font-mono"
              title="有効なのに 30 日間 1 件も一致していない — 定義文の再作成か絞り込みの見直し候補"
            >
              30日 一致0件
            </span>
          )}
        </div>
        <div className="text-[11px] text-fg-subtle font-mono">{pir.id}</div>
      </td>
      <td className="px-3 py-2.5">
        {pir.enabled ? (
          <span className="text-[10px] uppercase bg-success-soft text-success px-2 py-0.5 rounded font-mono">有効</span>
        ) : (
          <span className="text-[10px] uppercase bg-surface-3 text-fg-subtle px-2 py-0.5 rounded font-mono">無効</span>
        )}
      </td>
      <td className="px-3 py-2.5 text-fg text-xs hidden md:table-cell">{importanceOrAuto(pir.target_importance)}</td>
      <td className="px-3 py-2.5 text-right text-fg tnum hidden sm:table-cell">{pir.match_count_7d}</td>
      <td className="px-3 py-2.5 text-right text-fg tnum hidden sm:table-cell">{pir.match_count_30d}</td>
      <td className="px-3 py-2.5 text-fg-muted text-xs font-mono hidden lg:table-cell">{formatJstShort(pir.last_match_at)}</td>
      <td className="px-3 py-2.5 text-right">
        <div className="inline-flex items-center gap-1">
          <a
            href={detailUrl}
            className="text-fg bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-xs no-underline inline-flex items-center"
          ><Eye className="h-3.5 w-3.5" /></a>
          {!readOnly && (
            <button
              onClick={onToggle}
              title={pir.enabled ? "無効化" : "有効化"}
              className="text-fg bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-xs inline-flex items-center"
            >{pir.enabled ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}</button>
          )}
        </div>
      </td>
    </tr>
  );
}
