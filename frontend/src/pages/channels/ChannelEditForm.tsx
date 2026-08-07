// 1 チャンネルの編集フォーム (表示名 / webhook キー / fallback / 優先順 / 有効 / routable)。
// ChannelsPage の一覧カードと 情報フローの ChannelDrawer で共有する。

import type { ChannelDef } from "../../api/channels";

export function ChannelEditForm({
  channel,
  allIds,
  isBuiltin,
  referenced,
  readOnly,
  onChange,
}: {
  channel: ChannelDef;
  allIds: string[];
  isBuiltin: boolean;
  referenced: boolean;
  readOnly: boolean;
  onChange: (patch: Partial<ChannelDef>) => void;
}) {
  const c = channel;
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <label className="block text-xs text-fg-subtle">
          表示名
          <input
            value={c.label}
            disabled={readOnly}
            onChange={(e) => onChange({ label: e.target.value })}
            className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-fg"
          />
        </label>
        <label className="block text-xs text-fg-subtle">
          投稿先URLの環境変数キー
          <input
            value={c.webhook_env_key}
            disabled={readOnly || isBuiltin}
            onChange={(e) => onChange({ webhook_env_key: e.target.value })}
            title={isBuiltin ? "組み込みチャンネルのキーは変更できません" : undefined}
            className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-xs text-fg disabled:opacity-60"
          />
        </label>
        <label className="block text-xs text-fg-subtle">
          投稿先URL 未設定時の流し先
          <select
            value={c.fallback ?? ""}
            disabled={readOnly}
            onChange={(e) => onChange({ fallback: e.target.value || null })}
            className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-fg"
          >
            <option value="">(なし = 投稿失敗扱い)</option>
            {allIds
              .filter((id) => id !== c.id)
              .map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
          </select>
        </label>
        <label className="block text-xs text-fg-subtle">
          優先順 (小さいほど上位)
          <input
            type="number"
            min={0}
            max={999}
            value={c.order}
            disabled={readOnly}
            onChange={(e) => onChange({ order: parseInt(e.target.value, 10) || 0 })}
            className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-fg tnum"
          />
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-4 text-xs text-fg-muted">
        <label className="flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={c.enabled}
            disabled={readOnly || (referenced && c.enabled)}
            onChange={(e) => onChange({ enabled: e.target.checked })}
          />
          有効 (投稿先として使う)
        </label>
        <label
          className="flex items-center gap-1.5"
          title={isBuiltin ? "組み込みチャンネルでは変更できません" : undefined}
        >
          <input
            type="checkbox"
            checked={c.routable}
            disabled={readOnly || isBuiltin}
            onChange={(e) => onChange({ routable: e.target.checked })}
          />
          ルーティング対象 (ルールの投稿先に選べる)
        </label>
        <label
          className="flex items-center gap-1.5"
          title={
            c.id === "alert"
              ? "アラートは常に Discord 配信 (緊急通知のため無効化不可)"
              : "OFF = Discord に投稿せず Web に保存のみ。朝刊/夕刊ダイジェストは別の経路で配信継続"
          }
        >
          <input
            type="checkbox"
            checked={c.push}
            disabled={readOnly || c.id === "alert"}
            onChange={(e) => onChange({ push: e.target.checked })}
          />
          Discord 配信 (OFF=保存のみ)
        </label>
      </div>
    </div>
  );
}
