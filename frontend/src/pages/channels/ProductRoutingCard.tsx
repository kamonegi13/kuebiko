// 通知再設計: curated product (朝刊/夕刊・recap・synthesis・spotlight) の配信先チャンネルを
// 情報フローから編集する自己完結カード。channel が push=false なら Web のみ (Discord 配信なし)。

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellOff, Save } from "lucide-react";
import { productRoutingApi, type ProductRoutingResponse } from "../../api/productRouting";
import { channelColor } from "../../components/channel";

export function ProductRoutingCard({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  const { data } = useQuery<ProductRoutingResponse>({
    queryKey: ["product-routing"],
    queryFn: productRoutingApi.get,
  });
  const [draft, setDraft] = useState<Record<string, string> | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (data) setDraft(data.routing);
  }, [data]);

  const saveMut = useMutation({
    mutationFn: (routing: Record<string, string>) => productRoutingApi.save(routing),
    onSuccess: () => {
      setMsg("プロダクト配信を保存しました");
      qc.invalidateQueries({ queryKey: ["product-routing"] });
    },
    onError: (e: unknown) => setMsg(`保存失敗: ${e instanceof Error ? e.message : String(e)}`),
  });

  if (!data || !draft) return null;
  const pushById = new Map(data.channels.map((c) => [c.id, c.push]));
  const dirty = JSON.stringify(draft) !== JSON.stringify(data.routing);

  return (
    <section className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="m-0 text-sm font-bold text-fg">まとめ記事の配信先 (ダイジェスト)</h3>
        {!readOnly && (
          <button
            onClick={() => saveMut.mutate(draft)}
            disabled={!dirty || saveMut.isPending}
            className="inline-flex items-center gap-1 px-2.5 h-7 rounded-md border border-border-subtle bg-surface-2 text-fg-muted hover:text-fg text-xs font-medium disabled:opacity-40"
          >
            <Save className="h-3.5 w-3.5" /> 保存
          </button>
        )}
      </div>
      <p className="text-xs text-fg-subtle">
        朝刊/夕刊・週次深掘りダイジェスト・状況総括・スポットライトの配信先。チャンネルが「保存のみ」なら
        Web のみ (Discord 配信なし) になります。記事は配信ルールで、ダイジェストはここで制御します。
      </p>
      <ul className="divide-y divide-border-subtle">
        {Object.keys(data.routing).map((pid) => {
          const ch = draft[pid];
          const webOnly = pushById.get(ch) === false;
          return (
            <li key={pid} className="flex flex-wrap items-center gap-2 py-1.5">
              <span className="min-w-[180px] flex-1 text-sm text-fg">{data.labels[pid] ?? pid}</span>
              <select
                value={ch}
                disabled={readOnly}
                onChange={(e) => setDraft({ ...draft, [pid]: e.target.value })}
                className="h-7 rounded border border-border-subtle bg-surface-2 px-2 text-sm font-medium"
                style={{ color: channelColor(ch) }}
              >
                {data.channels.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                    {c.push ? "" : " (保存のみ)"}
                  </option>
                ))}
              </select>
              {webOnly && (
                <span
                  className="inline-flex items-center gap-1 text-[10px] text-fg-subtle"
                  title="このチャンネルは Web のみ (Discord 配信なし)"
                >
                  <BellOff className="h-3 w-3" /> 保存のみ
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {msg && <div className="text-xs text-fg-subtle">{msg}</div>}
    </section>
  );
}
