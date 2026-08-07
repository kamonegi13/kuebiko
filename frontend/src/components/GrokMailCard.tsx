// Grok メール受信 (IMAP) カード (設定・死活の画面統合 P2): 購読ソース画面に常設。
// Grok レポート通知メールの受信経路 (IMAP) を、設定 + 死活 (ログイン試行) の
// 一体管理にする。.env は不可視の保存層 — 表示は常にマスク値。

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Mail, RefreshCw, X } from "lucide-react";
import { grokMailApi } from "../api/grokMail";

export function GrokMailCard({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  const [editOpen, setEditOpen] = useState(false);
  const [fields, setFields] = useState<{ host: string; port: string; user: string; password: string }>(
    { host: "", port: "", user: "", password: "" },
  );
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const { data: state } = useQuery({ queryKey: ["grok-mail"], queryFn: grokMailApi.get });
  const health = useQuery({
    queryKey: ["grok-mail-health"],
    queryFn: grokMailApi.health,
    refetchInterval: 5 * 60_000,
  });

  const saveMut = useMutation({
    mutationFn: () => grokMailApi.save(fields),
    onSuccess: () => {
      setFields({ host: "", port: "", user: "", password: "" });
      setEditOpen(false);
      setMessage({ kind: "success", text: "保存しました (即時反映) — 接続テストを実行します" });
      qc.invalidateQueries({ queryKey: ["grok-mail"] });
      void health.refetch();
    },
    onError: (e: unknown) =>
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) }),
  });

  const level: "ok" | "warning" | "error" = health.data?.status ?? "warning";
  const hasChanges = Object.values(fields).some((v) => v.trim() !== "");

  // 死活の表現は接続タブの統一様式 (ドット + テキスト) に合わせる — カード全体の
  // ボーダー/背景色変化は Grok だけ挙動が違って見えるため使わない (2026-08-02)。
  const dotClass =
    level === "ok" ? "bg-success" : level === "error" ? "bg-critical" : "bg-warning";

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Mail className="h-4 w-4 text-fg-muted" />
          <span className="text-md font-semibold text-fg">Grok メール受信 (IMAP)</span>
          <span className={`inline-block h-2 w-2 rounded-full ${dotClass}`} />
          {level === "ok" && (
            <span className="inline-flex items-center gap-1 text-xs font-semibold text-success">
              <Check className="h-3.5 w-3.5" /> ログインOK
            </span>
          )}
          {level === "error" && (
            <span className="inline-flex items-center gap-1 text-xs font-semibold text-critical">
              <X className="h-3.5 w-3.5" /> 接続エラー
            </span>
          )}
          {level === "warning" && (
            <span className="text-xs font-semibold text-warning">未設定 (Grok 経路は無効)</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3 disabled:opacity-50"
            onClick={() => void health.refetch()}
            disabled={health.isFetching}
          >
            {health.isFetching ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            接続テスト
          </button>
          {!readOnly && (
            <button
              className="rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3"
              onClick={() => setEditOpen((v) => !v)}
            >
              設定
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-fg-muted">
        <span>サーバ: {state?.host_masked || "未設定"}:{state?.port ?? "—"}</span>
        <span>ログインID: {state?.user_masked || "未設定"}</span>
        <span>パスワード: {state?.password_set ? "設定済" : "未設定"}</span>
        {health.data && <span>疎通: {health.data.detail}</span>}
      </div>

      {message && (
        <p className={`m-0 mt-2 text-xs ${message.kind === "success" ? "text-success" : "text-critical"}`}>
          {message.text}
        </p>
      )}

      {editOpen && !readOnly && (
        <div className="mt-3 space-y-2 rounded-md border border-border-subtle bg-surface-2 p-3 text-xs">
          <p className="m-0 text-fg-subtle">
            Grok タスクの通知メール (x.ai) を受信する Gmail の IMAP 設定。空欄の項目は既存値を維持します。
            保存先は .env (即時反映・再起動不要)。
          </p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <label className="block text-fg-subtle">
              IMAP サーバ
              <input
                value={fields.host}
                onChange={(e) => setFields((f) => ({ ...f, host: e.target.value }))}
                placeholder={state?.host_masked ? `設定済 (${state.host_masked})` : "imap.gmail.com"}
                autoComplete="off"
                className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-xs text-fg"
              />
            </label>
            <label className="block text-fg-subtle">
              ポート
              <input
                value={fields.port}
                onChange={(e) => setFields((f) => ({ ...f, port: e.target.value }))}
                placeholder={String(state?.port ?? 993)}
                inputMode="numeric"
                className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-xs text-fg tnum"
              />
            </label>
            <label className="block text-fg-subtle">
              ログインID (Gmail アドレス)
              <input
                value={fields.user}
                onChange={(e) => setFields((f) => ({ ...f, user: e.target.value }))}
                placeholder={state?.user_masked ? `設定済 (${state.user_masked})` : "you@gmail.com"}
                autoComplete="off"
                className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-xs text-fg"
              />
            </label>
            <label className="block text-fg-subtle">
              アプリパスワード
              <input
                type="password"
                value={fields.password}
                onChange={(e) => setFields((f) => ({ ...f, password: e.target.value }))}
                placeholder={state?.password_set ? "設定済 — 置き換え" : "2段階認証のアプリパスワード"}
                autoComplete="off"
                className="mt-0.5 w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-xs text-fg"
              />
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => saveMut.mutate()}
              disabled={saveMut.isPending || !hasChanges}
              className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white hover:bg-accent-hover disabled:opacity-50"
            >
              {saveMut.isPending ? "保存中…" : "保存"}
            </button>
            <button
              onClick={() => setEditOpen(false)}
              className="text-xs text-fg-subtle hover:text-fg"
            >
              キャンセル
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
