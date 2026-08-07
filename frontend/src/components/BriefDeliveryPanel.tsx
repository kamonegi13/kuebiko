import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { pagesApi } from "../api/pages";
import { useChannelMeta } from "./channel";
import { vocabLabel } from "../hooks/useVocab";

// brief チャンネルの配信調整 (source_quality: DB config_store・版履歴あり)。
// brief_cap_24h と high_threat カテゴリは「brief チャンネルへの配信制御」= Flow の領域。
// UI は隣の ProductRoutingCard に揃える: カード見出し + 最上部の説明を 1 つだけ持ち、
// 2 設定は ul/divide-y のリスト項目 (=見出しに従属) として並べる。各設定のヒントは
// muted で控えめにコントロールへ直付けし、節見出しに見えないようにする。
export function BriefDeliveryPanel({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  const chMeta = useChannelMeta();
  const briefLabel = chMeta("brief").label;
  const watchLabel = chMeta("watch").label;
  const { data } = useQuery({
    queryKey: ["source-quality"],
    queryFn: () => pagesApi.getSourceQuality(),
  });
  const [cap, setCap] = useState<number | null>(null);
  const [cats, setCats] = useState<string[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (data) {
      setCap(data.brief_cap_24h);
      setCats(data.high_threat_brief_categories);
    }
  }, [data]);

  const saveMut = useMutation({
    mutationFn: () => pagesApi.saveSourceQuality(cap ?? 0, cats ?? []),
    onSuccess: () => {
      setMsg("保存しました (次回の配信から反映)");
      qc.invalidateQueries({ queryKey: ["source-quality"] });
    },
    onError: (e: unknown) => setMsg(`保存失敗: ${e instanceof Error ? e.message : String(e)}`),
  });

  if (!data || cap === null || cats === null) return null;

  const dirty =
    cap !== data.brief_cap_24h ||
    JSON.stringify([...cats].sort()) !==
      JSON.stringify([...data.high_threat_brief_categories].sort());
  const toggle = (c: string) =>
    setCats((prev) => (prev!.includes(c) ? prev!.filter((x) => x !== c) : [...prev!, c]));

  return (
    <section className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="m-0 text-sm font-bold text-fg">{briefLabel} チャンネルの配信調整</h3>
        {!readOnly && (
          <button
            onClick={() => saveMut.mutate()}
            disabled={!dirty || saveMut.isPending}
            className="inline-flex items-center gap-1 px-2.5 h-7 rounded-md border border-border-subtle bg-surface-2 text-fg-muted hover:text-fg text-xs font-medium disabled:opacity-40"
          >
            <Save className="h-3.5 w-3.5" /> 保存
          </button>
        )}
      </div>
      <p className="text-xs text-fg-subtle">
        朝夕に通読する {briefLabel} チャンネルへ流す記事の「量」と「取りこぼしたくない脅威」を調整します。
      </p>

      <ul className="m-0 list-none divide-y divide-border-subtle p-0">
        {/* 投稿上限 — 量の制御 (ProductRoutingCard の行と同じ「ラベル左・操作右」) */}
        <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
          <div className="min-w-[220px] flex-1">
            <div className="text-sm text-fg">1 日の投稿上限</div>
            <div className="text-[11px] text-fg-subtle">
              この件数を超えた分は、重要度「中」以下の記事を {watchLabel} チャンネルへ回します。
              重要度「高」は上限に関わらず必ず {briefLabel} に載ります。0 で上限なし。
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <input
              type="number"
              min={0}
              value={cap}
              disabled={readOnly}
              onChange={(e) => setCap(Math.max(0, parseInt(e.target.value, 10) || 0))}
              className="h-7 w-16 rounded border border-border-subtle bg-surface-2 px-2 text-sm text-fg text-right disabled:opacity-50"
            />
            <span className="text-xs text-fg-subtle">件 / 日</span>
          </div>
        </li>

        {/* 優先カテゴリ — 取りこぼし防止。チップ選択 */}
        <li className="py-2">
          <div className="flex items-baseline justify-between gap-2">
            <div className="text-sm text-fg">{briefLabel} へ優先する脅威カテゴリ</div>
            <span className="tnum shrink-0 text-[10px] text-fg-subtle">
              {cats.length}/{data.available_categories.length} 選択
            </span>
          </div>
          <div className="text-[11px] text-fg-subtle">
            重要度「高」の記事のうち、選んだカテゴリ (実際に対処が要る脅威) は、通常なら {watchLabel} へ回る場合でも
            {briefLabel} に載せます。取りこぼしたくない種別だけを選びます。
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {data.available_categories.map((c) => {
              const on = cats.includes(c);
              return (
                <button
                  key={c}
                  type="button"
                  disabled={readOnly}
                  aria-pressed={on}
                  onClick={() => toggle(c)}
                  className={`h-6 rounded border px-2 font-mono text-xs transition-colors disabled:opacity-50 ${
                    on
                      ? "border-accent bg-accent-subtle text-fg"
                      : "border-border-subtle bg-surface-2 text-fg-subtle hover:text-fg"
                  }`}
                >
                  {vocabLabel("category", c)}
                </button>
              );
            })}
          </div>
        </li>
      </ul>

      {msg && <div className="text-xs text-fg-subtle">{msg}</div>}
    </section>
  );
}
