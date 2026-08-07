// チャンネルの投稿先 webhook URL 設定 (設定・死活の画面統合 P1)。
// .env は不可視の保存層 — 値は per-channel API 経由で保存し、表示は常にマスク値。
// 情報フローのチャンネル編集 Drawer で ChannelEditForm の下に置く。

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { channelsApi, type WebhookHealthStatus } from "../../api/channels";

export function WebhookStatusDot({
  status,
  detail,
}: {
  status: WebhookHealthStatus | "unset";
  detail?: string;
}) {
  const color =
    status === "ok"
      ? "bg-success"
      : status === "error"
        ? "bg-critical"
        : status === "warning"
          ? "bg-warning"
          : "bg-surface-3";
  const label =
    status === "ok"
      ? "疎通OK"
      : status === "error"
        ? "疎通エラー"
        : status === "warning"
          ? "URL未設定"
          : "未確認";
  return (
    <span
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`}
      title={detail ? `${label}: ${detail}` : label}
    />
  );
}

export function ChannelWebhookField({
  channelId,
  masked,
  health,
  readOnly,
}: {
  channelId: string;
  masked: string;
  health: { status: WebhookHealthStatus; detail: string } | undefined;
  readOnly: boolean;
}) {
  const qc = useQueryClient();
  const [input, setInput] = useState("");
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const saveMut = useMutation({
    mutationFn: (url: string) => channelsApi.saveWebhook(channelId, url),
    onSuccess: (r) => {
      setInput("");
      setMessage({
        kind: "success",
        text: r.webhook_set ? "投稿先 URL を保存しました" : "投稿先 URL を削除しました",
      });
      qc.invalidateQueries({ queryKey: ["channels"] });
      qc.invalidateQueries({ queryKey: ["channels-health"] });
    },
    onError: (e: unknown) =>
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) }),
  });

  const isSet = masked !== "";
  return (
    <div className="space-y-1.5 rounded border border-border-subtle bg-surface-2/40 p-2.5">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-fg">投稿先 URL (Discord webhook)</span>
        <WebhookStatusDot status={health?.status ?? (isSet ? "unset" : "warning")} detail={health?.detail} />
        {isSet ? (
          <span className="text-[10px] text-fg-subtle">設定済 ({masked})</span>
        ) : (
          <span className="text-[10px] text-warning">未設定</span>
        )}
      </div>
      {!readOnly && (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="password"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              isSet ? "https://discord.com/api/webhooks/… — 置き換え" : "https://discord.com/api/webhooks/…"
            }
            autoComplete="off"
            className="min-w-[180px] flex-1 rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-xs text-fg"
          />
          <button
            onClick={() => saveMut.mutate(input.trim())}
            disabled={saveMut.isPending || !input.trim()}
            className="rounded bg-accent px-2 py-1 text-xs font-medium text-white hover:bg-accent-hover disabled:opacity-50"
          >
            保存
          </button>
          {isSet && (
            <button
              onClick={() => {
                if (confirm("投稿先 URL を削除します。よろしいですか?")) saveMut.mutate("");
              }}
              disabled={saveMut.isPending}
              className="text-[11px] text-critical hover:underline disabled:opacity-50"
            >
              削除
            </button>
          )}
        </div>
      )}
      {message && (
        <p className={`m-0 text-[11px] ${message.kind === "success" ? "text-success" : "text-critical"}`}>
          {message.text}
        </p>
      )}
      <p className="m-0 text-[10px] text-fg-subtle">
        保存先は .env (即時反映・再起動不要)。表示は常にマスクされ、平文は画面に出ません。
      </p>
    </div>
  );
}
