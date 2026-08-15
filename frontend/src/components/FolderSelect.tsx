// フォルダ (ソースの分類) の選択欄。登録ウィザードと取得設定の編集で共用する。
//
// 候補はコードに固定せず **実際に使われているフォルダ** を API から引く
// (固定リストは分類を変えたときに stale 化する。旧 news_en / advisory_jp の
// ハードコードが実データと乖離していた)。新設したいときだけ入力欄を出す。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { sourcesV2Api } from "../api/sources_v2";

const NEW_FOLDER = "__new__";

export function FolderSelect({
  value,
  onChange,
  className,
}: {
  value: string;
  onChange: (folder: string) => void;
  className?: string;
}) {
  const { data } = useQuery({
    queryKey: ["source-folders"],
    queryFn: () => sourcesV2Api.folders(),
    staleTime: 60_000,
  });
  const folders = data?.folders ?? [];
  const [creating, setCreating] = useState(false);
  // 新設を選んだ / 現在値が一覧に無い (削除済みフォルダ等) なら入力欄を開く。
  const isNew = creating || (value !== "" && !folders.includes(value));
  const box = className ?? "w-full bg-surface-3 border border-border-default rounded px-2 py-1 text-sm text-fg";

  return (
    <div>
      <select
        value={isNew ? NEW_FOLDER : value}
        onChange={(e) => {
          if (e.target.value === NEW_FOLDER) {
            setCreating(true);
            onChange("");
            return;
          }
          setCreating(false);
          onChange(e.target.value);
        }}
        className={box}
      >
        <option value="">未分類</option>
        {folders.map((f) => (
          <option key={f} value={f}>
            {f}
          </option>
        ))}
        <option value={NEW_FOLDER}>＋ 新しいフォルダ…</option>
      </select>
      {isNew && (
        <input
          autoFocus
          value={value}
          onChange={(e) => onChange(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
          placeholder="新しいフォルダ名 (英小文字・数字・_)"
          className={`mt-1.5 ${box} font-mono text-xs`}
        />
      )}
    </div>
  );
}
