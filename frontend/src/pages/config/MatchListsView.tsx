import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Save, Trash2 } from "lucide-react";
import { matchListsApi, type MatchList } from "../../api/matchLists";

// 語彙拡張② user 定義マッチリストの管理。運用者が「名前付きキーワード集合」を
// コードなしで定義し、配信ルールの keyword_list 条件から参照できるようにする。
// 旧 /app/match-lists 専用ページを設定タブへ統合 (2026-07-26 ナビ整理 P3) —
// ConfigPage は read_only 時にタブ自体を出さないため full instance でのみ描画される。
// 履歴/巻き戻しは「変更履歴」タブ (key=match_lists) 側。
export function MatchListsView({ onOpenHistory }: { onOpenHistory?: () => void }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["match-lists"], queryFn: matchListsApi.get });
  const [draft, setDraft] = useState<MatchList[] | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    if (data && draft === null) setDraft(data.lists.map((l) => ({ ...l, terms: [...l.terms] })));
  }, [data, draft]);

  const save = useMutation({
    mutationFn: (lists: MatchList[]) => matchListsApi.save(lists),
    onSuccess: (r) => {
      setMsg({ kind: "ok", text: `保存しました (v${r.version})` });
      qc.invalidateQueries({ queryKey: ["match-lists"] });
      qc.invalidateQueries({ queryKey: ["routing-rules"] });
    },
    onError: (e) => setMsg({ kind: "err", text: String(e instanceof Error ? e.message : e) }),
  });

  const lists = draft ?? [];
  const update = (i: number, patch: Partial<MatchList>) =>
    setDraft(lists.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const addList = () =>
    setDraft([...lists, { name: "", description: "", terms: [] }]);
  const removeList = (i: number) => {
    if (confirm("このマッチリストを削除します。よろしいですか?")) setDraft(lists.filter((_, j) => j !== i));
  };
  const doSave = () => {
    if (confirm("マッチリストを保存します。配信ルールの「キーワードリスト」条件に反映されます。よろしいですか?")) {
      save.mutate(lists);
    }
  };

  return (
    <div className="space-y-4">
      <p className="m-0 text-sm text-fg-muted">
        名前付きキーワード集合を定義すると、配信ルールの「キーワードリスト」条件から参照できます
        (記事のタイトル・本文に語が含まれるか)。コード変更なしで関心の語彙を増やせます。
      </p>

      {isLoading && <div className="text-fg-subtle text-sm">読み込み中…</div>}

      <div className="space-y-3">
        {lists.map((l, i) => (
          <div key={i} className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
            <div className="flex items-center gap-2">
              <input
                value={l.name}
                onChange={(e) => update(i, { name: e.target.value })}
                placeholder="リスト名 (英数字と _)"
                className="w-48 rounded border border-border-subtle bg-surface-2 px-2 py-1 text-sm font-mono text-fg"
              />
              <input
                value={l.description}
                onChange={(e) => update(i, { description: e.target.value })}
                placeholder="説明 (任意)"
                className="flex-1 rounded border border-border-subtle bg-surface-2 px-2 py-1 text-sm text-fg"
              />
              <button
                onClick={() => removeList(i)}
                className="shrink-0 inline-flex items-center gap-1 text-xs text-warning hover:text-fg border border-warning/40 rounded px-2 py-1"
              >
                <Trash2 className="h-3.5 w-3.5" /> 削除
              </button>
            </div>
            <div>
              <label className="text-xs text-fg-subtle">キーワード (カンマ区切り、いずれか一致で該当)</label>
              <textarea
                value={l.terms.join(", ")}
                onChange={(e) =>
                  update(i, { terms: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
                }
                rows={2}
                placeholder="例: Fortinet, FortiOS, FortiGate"
                className="mt-1 w-full rounded border border-border-subtle bg-surface-2 px-2 py-1 text-sm text-fg"
              />
              <div className="text-[11px] text-fg-subtle mt-0.5 tnum">{l.terms.length} 語</div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={addList}
          className="inline-flex items-center gap-1 rounded border border-border-default px-3 py-1.5 text-sm text-fg-muted hover:text-accent hover:border-accent-soft"
        >
          <Plus className="h-4 w-4" /> リストを追加
        </button>
        <button
          onClick={doSave}
          disabled={save.isPending}
          className="inline-flex items-center gap-1 rounded bg-accent text-on-accent px-3 py-1.5 text-sm font-medium hover:opacity-90 disabled:opacity-50"
        >
          <Save className="h-4 w-4" /> 保存
        </button>
        {msg && (
          <span className={`text-sm ${msg.kind === "ok" ? "text-success" : "text-critical"}`}>{msg.text}</span>
        )}
        <a href="/app/config#history" onClick={onOpenHistory} className="ml-auto text-xs text-accent hover:underline">
          変更履歴 / 巻き戻し →
        </a>
      </div>
    </div>
  );
}
