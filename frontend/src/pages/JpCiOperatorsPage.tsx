import { useMemo, useState } from "react";
import { ArrowLeft, Plus, Save, Trash2 } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { jpciOperatorsApi, type OperatorEntry } from "../api/jpci";

/** 重要インフラ 指定事業者名簿の管理 (DB SSoT・版履歴は config-history)。
 *  名簿は「事業者名 → 分野」のみ — 技術・露出情報は保持しない (資産インベントリ非構築の原則)。 */
export function JpCiOperatorsPage() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["jpci-operators"],
    queryFn: jpciOperatorsApi.list,
  });
  // 編集バッファ (null = 未編集でサーバ値を表示)
  const [draft, setDraft] = useState<OperatorEntry[] | null>(null);
  const [filter, setFilter] = useState("");
  const operators = draft ?? data?.operators ?? [];
  const sectors = data?.sectors ?? {};

  const save = useMutation({
    mutationFn: (ops: OperatorEntry[]) => jpciOperatorsApi.save(ops),
    onSuccess: () => {
      setDraft(null);
      void qc.invalidateQueries({ queryKey: ["jpci-operators"] });
      void qc.invalidateQueries({ queryKey: ["jpci-board"] });
    },
  });

  const bySector = useMemo(() => {
    const filtered = filter
      ? operators.filter((o) =>
          [o.canonical, ...o.aliases].some((n) => n.toLowerCase().includes(filter.toLowerCase())),
        )
      : operators;
    const grouped = new Map<string, OperatorEntry[]>();
    for (const op of filtered) {
      grouped.set(op.sector, [...(grouped.get(op.sector) ?? []), op]);
    }
    return grouped;
  }, [operators, filter]);

  const update = (id: string, patch: Partial<OperatorEntry>) => {
    setDraft(operators.map((o) => (o.id === id ? { ...o, ...patch } : o)));
  };
  const remove = (id: string) => setDraft(operators.filter((o) => o.id !== id));
  const addTo = (sector: string) => {
    const newId = `custom_${Math.random().toString(36).slice(2, 10)}`;
    setDraft([...operators, { id: newId, canonical: "", aliases: [], sector }]);
  };

  const onSave = () => {
    if (draft === null) return;
    const empty = draft.filter((o) => !o.canonical.trim());
    if (empty.length > 0) {
      window.alert("事業者名が空の行があります。入力するか削除してください。");
      return;
    }
    // 危険操作の二段階確認 (§12): 名簿は board の観測レーン全体に効く
    if (!window.confirm(`名簿 ${draft.length} 件を保存します。脅威ボードの判定に即時反映されます。よろしいですか？`)) {
      return;
    }
    save.mutate(draft);
  };

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <a
            href="/app/jpci"
            className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            脅威ボード
          </a>
          <h2 className="m-0 text-xl font-bold text-fg tracking-tight">指定事業者名簿</h2>
          {data && (
            <span className="text-xs text-fg-subtle">
              {operators.length} 事業者{data.version !== null && ` / v${data.version}`}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="事業者名で検索"
            className="bg-surface-2 border border-border-subtle rounded px-2 py-1.5 text-sm text-fg w-44"
          />
          <button
            onClick={onSave}
            disabled={draft === null || save.isPending}
            className="inline-flex items-center gap-1.5 bg-accent/20 text-accent border border-accent/40 px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-40"
          >
            <Save className="h-4 w-4" />
            {save.isPending ? "保存中…" : "保存"}
          </button>
        </div>
      </div>

      <p className="m-0 text-xs text-fg-subtle">
        観測事象は 名簿 (指定事業者) → 事業者型 (〜病院/〜市/〜銀行 等) → 周辺観測 の順で判定される。
        名簿は事業者名→分野のみを保持し、技術・露出情報は持たない。別名 (英名/略称/主要子会社) を
        登録すると表記ゆれの報道が同一事業者に集約される。版履歴・巻き戻しは設定履歴から。
      </p>

      {error && (
        <div className="p-3 bg-critical/10 text-critical rounded text-sm">
          {error instanceof Error ? error.message : String(error)}
        </div>
      )}
      {save.error && (
        <div className="p-3 bg-critical/10 text-critical rounded text-sm">
          保存エラー: {save.error instanceof Error ? save.error.message : String(save.error)}
        </div>
      )}
      {isLoading && <div className="text-center py-8 text-fg-muted text-sm">読み込み中…</div>}

      {Object.entries(sectors).map(([sectorId, label]) => (
        <div key={sectorId} className="border border-border-subtle rounded-lg bg-surface-1">
          <div className="flex items-center justify-between px-3 py-2 bg-surface-2/60 rounded-t-lg">
            <span className="text-sm font-semibold text-fg">
              {label}
              <span className="ml-2 text-[11px] text-fg-subtle font-normal">
                {(bySector.get(sectorId) ?? []).length}件
              </span>
            </span>
            <button
              onClick={() => addTo(sectorId)}
              className="inline-flex items-center gap-1 text-[11px] text-fg-muted hover:text-accent"
            >
              <Plus className="h-3.5 w-3.5" />
              追加
            </button>
          </div>
          <div className="divide-y divide-border-subtle/50">
            {(bySector.get(sectorId) ?? []).map((op) => (
              <OperatorRow key={op.id} op={op} onChange={update} onRemove={remove} />
            ))}
            {(bySector.get(sectorId) ?? []).length === 0 && (
              <div className="px-3 py-2 text-xs text-fg-subtle">
                名簿なし (事業者型判定のみで運用)
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function OperatorRow({
  op,
  onChange,
  onRemove,
}: {
  op: OperatorEntry;
  onChange: (id: string, patch: Partial<OperatorEntry>) => void;
  onRemove: (id: string) => void;
}) {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 flex-wrap sm:flex-nowrap">
      <input
        value={op.canonical}
        onChange={(e) => onChange(op.id, { canonical: e.target.value })}
        placeholder="事業者名"
        className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-sm text-fg w-full sm:w-64 shrink-0"
      />
      <input
        value={op.aliases.join(", ")}
        onChange={(e) =>
          onChange(op.id, {
            aliases: e.target.value
              .split(/[,、]/)
              .map((s) => s.trim())
              .filter(Boolean),
          })
        }
        placeholder="別名 (カンマ区切り: 英名/略称/主要子会社)"
        className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs text-fg-muted flex-1 min-w-40"
      />
      <button
        onClick={() => onRemove(op.id)}
        aria-label={`${op.canonical || "この行"} を削除`}
        className="text-fg-subtle hover:text-critical shrink-0"
      >
        <Trash2 className="h-4 w-4" />
      </button>
    </div>
  );
}
