// Grok セッションカード (2026-07-05): 死活監視ページに常設。
// 状態可視化 → 再取得ガイド (host コマンド 1 つ) → 完了自動検知 (mtime) → 自動検証。
// cookie 抽出は macOS Keychain 復号が必要でホスト実行必須のため、UI は
// 「コマンド 1 回」への圧縮と前後の自動化 (検知・検証) を担う。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ClipboardCopy, KeyRound, Loader2, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { grokApi } from "../api/grok";
import { vocabLabel } from "../hooks/useVocab";

function ageLabel(hours?: number): string {
  if (hours == null) return "—";
  if (hours < 1) return `${Math.round(hours * 60)} 分前`;
  if (hours < 48) return `${Math.round(hours)} 時間前`;
  return `${Math.round(hours / 24)} 日前`;
}

export function GrokSessionCard() {
  const qc = useQueryClient();
  const [guideOpen, setGuideOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const lastMtimeRef = useRef<string | null>(null);

  const { data } = useQuery({
    queryKey: ["grok-session"],
    queryFn: () => grokApi.sessionStatus(),
    // ガイドを開いている間は再取得完了 (mtime 変化) を素早く検知する
    refetchInterval: guideOpen ? 3_000 : 15_000,
  });

  const verify = useMutation({
    mutationFn: () => grokApi.verify(),
    onSettled: () => qc.invalidateQueries({ queryKey: ["grok-session"] }),
  });

  // state.json の更新 (= host で再取得完了) を検知したら自動で検証する
  useEffect(() => {
    const mtime = data?.state?.modified_at ?? null;
    if (mtime && lastMtimeRef.current && mtime !== lastMtimeRef.current && !verify.isPending) {
      verify.mutate();
      setGuideOpen(false);
    }
    if (mtime) lastMtimeRef.current = mtime;
  }, [data?.state?.modified_at]); // eslint-disable-line react-hooks/exhaustive-deps

  const state = data?.state;
  const lastVerify = data?.last_verify ?? null;
  const lastRun = data?.last_run ?? null;

  const expired =
    lastVerify?.status === "session_expired" || lastRun?.session_expired === true;
  const level: "error" | "warning" | "ok" = !state?.exists || expired
    ? "error"
    : lastVerify?.status === "ok"
      ? "ok"
      : "warning";

  return (
    <div
      className={`rounded-lg border border-border-subtle p-4 border-l-[3px] ${
        level === "error"
          ? "border-l-critical bg-critical-soft"
          : level === "ok"
            ? "border-l-success bg-surface-1"
            : "border-l-warning bg-surface-1"
      }`}
    >
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-fg-muted" />
          <span className="font-bold text-fg text-sm">Grok セッション</span>
          {level === "ok" && (
            <span className="inline-flex items-center gap-1 text-success text-xs font-semibold">
              <Check className="h-3.5 w-3.5" /> 有効
            </span>
          )}
          {expired && (
            <span className="inline-flex items-center gap-1 text-critical text-xs font-semibold">
              <X className="h-3.5 w-3.5" /> セッション失効 — 再取得が必要
            </span>
          )}
          {!state?.exists && (
            <span className="text-critical text-xs font-semibold">認証情報が未取得</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3 disabled:opacity-50"
            onClick={() => verify.mutate()}
            disabled={verify.isPending}
          >
            {verify.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            検証
          </button>
          <button
            className="rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3"
            onClick={() => setGuideOpen((v) => !v)}
          >
            再取得手順
          </button>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-fg-muted">
        <span>
          認証情報: {state?.exists ? `${state.cookie_count} cookies · ${ageLabel(state.age_hours)} 取得` : "未取得"}
        </span>
        {lastVerify && (
          <span>
            最終検証: {vocabLabel("grok_session_status", lastVerify.status)}
            {lastVerify.note ? ` (${lastVerify.note})` : ""}
          </span>
        )}
        {lastRun && (
          <span>
            直近の実行: {vocabLabel("grok_session_status", lastRun.status)} · 取得 {lastRun.total_fetched}
          </span>
        )}
      </div>

      {verify.isError && (
        <div className="mt-2 text-xs text-critical">
          検証リクエスト失敗: {(verify.error as Error).message}
          （閲覧専用モードでは実行できません）
        </div>
      )}

      {guideOpen && (
        <div className="mt-3 rounded-md border border-border-subtle bg-surface-2 p-3 text-xs text-fg space-y-2">
          <p className="m-0 font-semibold">
            再取得はホスト (この Mac) 側で 1 コマンドです。完了するとこのカードが自動で検知して検証します。
          </p>
          <ol className="m-0 pl-4 space-y-1 list-decimal">
            <li>普段使いの Chrome で grok.com に (新しいアカウントで) ログインしていることを確認</li>
            <li>
              ターミナルでリポジトリ直下から次を実行 — macOS Keychain のダイアログが出たら「許可」:
            </li>
          </ol>
          <div className="flex items-center gap-2">
            <code className="flex-1 rounded bg-surface-3 px-2 py-1.5 font-mono text-[11px] overflow-x-auto whitespace-nowrap">
              {data?.acquire_command ?? "uv run python scripts/grok_extract_cookies.py"}
            </code>
            <button
              className="inline-flex items-center gap-1 rounded-md border border-border-subtle px-2 py-1.5 hover:bg-surface-3"
              onClick={() => {
                void navigator.clipboard.writeText(
                  data?.acquire_command ?? "uv run python scripts/grok_extract_cookies.py",
                );
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? <Check className="h-3.5 w-3.5 text-success" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
            </button>
          </div>
          <p className="m-0 text-fg-subtle">
            ※ 自動ログインはサイト側でブロックされるため使いません。
            認証情報の取り出しに Mac のキーチェーンが必要なため、この操作は Mac 本体でのみ実行できます。
          </p>
        </div>
      )}
    </div>
  );
}
