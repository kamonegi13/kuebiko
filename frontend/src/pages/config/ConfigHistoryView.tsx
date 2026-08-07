import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, RotateCcw } from "lucide-react";
import { configHistoryApi } from "../../api/configHistory";
import { formatJst } from "../../utils/date";

// 運用 config (routing/channels/source_quality/pir 等) の変更履歴 + 任意版への巻き戻し。
// SSoT は DB (config_store)。旧 /app/config-history 専用ページを設定タブへ統合 (2026-07-26
// ナビ整理 P1) — ConfigPage は read_only 時にタブ自体を出さないため、この view は
// full instance でのみ描画される (server 側 READ_ONLY middleware も二重防御)。
export function ConfigHistoryView() {
  const qc = useQueryClient();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [viewVersion, setViewVersion] = useState<number | null>(null);

  const keysQuery = useQuery({
    queryKey: ["config-history-keys"],
    queryFn: configHistoryApi.listKeys,
  });

  // 初回ロードで先頭 key を選択 (render 中の派生 = state を汚さない)
  const keys = keysQuery.data?.keys ?? [];
  const activeKey = selectedKey ?? keys[0]?.key ?? null;

  const historyQuery = useQuery({
    queryKey: ["config-history", activeKey],
    queryFn: () => configHistoryApi.history(activeKey as string),
    enabled: activeKey != null,
  });

  const valueQuery = useQuery({
    queryKey: ["config-version", activeKey, viewVersion],
    queryFn: () => configHistoryApi.version(activeKey as string, viewVersion as number),
    enabled: activeKey != null && viewVersion != null,
  });

  const revertMut = useMutation({
    mutationFn: ({ key, version }: { key: string; version: number }) =>
      configHistoryApi.revert(key, version),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config-history-keys"] });
      qc.invalidateQueries({ queryKey: ["config-history", activeKey] });
      setViewVersion(null);
    },
  });

  const selectKey = (key: string) => {
    setSelectedKey(key);
    setViewVersion(null);
  };

  const doRevert = (version: number) => {
    if (!activeKey) return;
    const label = keys.find((k) => k.key === activeKey)?.label ?? activeKey;
    if (
      confirm(
        `「${label}」を v${version} の内容に戻します。\n` +
          `現在値はそのまま履歴に残り、v${version} の内容が新しい版として復元されます。よろしいですか?`,
      )
    ) {
      revertMut.mutate({ key: activeKey, version });
    }
  };

  return (
    <div className="space-y-4">
      <p className="m-0 text-sm text-fg-muted">
        運用設定 (ルーティング / チャンネル / ソース品質 / PIR) の保存履歴。任意の版に巻き戻せます。
      </p>

      {/* config key セレクタ */}
      <div className="flex flex-wrap gap-2">
        {keys.map((k) => (
          <button
            key={k.key}
            onClick={() => selectKey(k.key)}
            className={`rounded border px-3 py-1.5 text-sm transition-colors ${
              k.key === activeKey
                ? "border-accent text-accent-hover font-semibold bg-surface-2"
                : "border-border-default text-fg-muted hover:text-fg hover:border-accent-soft"
            }`}
          >
            {k.label}
            <span className="ml-2 text-xs text-fg-subtle tnum">{k.version_count} 版</span>
          </button>
        ))}
      </div>

      {keysQuery.isError && (
        <div className="text-critical text-sm bg-surface-1 border border-border-subtle rounded-lg px-4 py-3">
          履歴一覧の取得に失敗しました。
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4">
        {/* 版リスト */}
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
          <div className="text-fg-muted text-xs uppercase">版履歴 (新しい順)</div>
          {historyQuery.isLoading && <div className="text-fg-subtle text-sm">読み込み中…</div>}
          {historyQuery.data?.versions.length === 0 && (
            <div className="text-fg-subtle text-sm">保存履歴がありません。</div>
          )}
          <ul className="m-0 p-0 list-none space-y-1.5">
            {historyQuery.data?.versions.map((v) => (
              <li
                key={v.version}
                className={`flex items-center gap-2 rounded border px-3 py-2 ${
                  v.version === viewVersion
                    ? "border-accent-soft bg-surface-2"
                    : "border-border-subtle"
                }`}
              >
                <span className="font-mono text-sm text-fg tnum">v{v.version}</span>
                {v.is_current && (
                  <span className="inline-flex items-center gap-0.5 text-xs text-success">
                    <Check className="h-3 w-3" /> 現行
                  </span>
                )}
                <span className="text-sm text-fg-muted truncate flex-1" title={v.note}>
                  {v.note || "(メモなし)"}
                </span>
                <span className="text-xs text-fg-subtle tnum shrink-0">{formatJst(v.created_at)}</span>
                <button
                  onClick={() => setViewVersion(v.version)}
                  className="shrink-0 text-xs text-fg-muted hover:text-accent border border-border-default rounded px-2 py-0.5 transition-colors"
                >
                  値を見る
                </button>
                {!v.is_current && (
                  <button
                    onClick={() => doRevert(v.version)}
                    disabled={revertMut.isPending}
                    className="shrink-0 inline-flex items-center gap-0.5 text-xs text-warning hover:text-fg border border-warning/40 rounded px-2 py-0.5 transition-colors disabled:opacity-50"
                  >
                    <RotateCcw className="h-3 w-3" /> 戻す
                  </button>
                )}
              </li>
            ))}
          </ul>
          {revertMut.isError && (
            <div className="text-critical text-xs">
              巻き戻しに失敗しました: {String(revertMut.error instanceof Error ? revertMut.error.message : revertMut.error)}
            </div>
          )}
        </div>

        {/* 選択した版の値 */}
        <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
          <div className="text-fg-muted text-xs uppercase">
            {viewVersion != null ? `v${viewVersion} の内容` : "値"}
          </div>
          {viewVersion == null && (
            <div className="text-fg-subtle text-sm">左の「値を見る」で版の内容を表示します。</div>
          )}
          {viewVersion != null && valueQuery.isLoading && (
            <div className="text-fg-subtle text-sm">読み込み中…</div>
          )}
          {viewVersion != null && valueQuery.data && (
            <pre className="m-0 overflow-x-auto text-xs text-fg-muted bg-surface-2 rounded p-3 max-h-[60vh] overflow-y-auto">
              {JSON.stringify(valueQuery.data.value, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
