// モバイル公開 (Cloudflare tunnel) トグル + named tunnel 設定カード。
// - ON/OFF トグル: flag file 経由で supervisor が cloudflared を起動/停止 (実装済)。
// - named tunnel 設定 (2026-07-30 C2): token / hostname を data/ ファイル (0600) に保存し、
//   launcher がファイル変化を検知して自動再起動 → 固定 URL 公開に切替 (再デプロイ不要)。
//   token の生値は API から返らない (token_set の boolean と public hostname のみ)。
// 公開されるのは readonly instance (port 8002、write は 403 固定) のみ。
// 設定編集は full instance のみ (!readOnly)。readonly instance では write API が 403。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { MobileTunnelStatus } from "../api/pages";
import { pagesApi } from "../api/pages";

const MODE_LABEL: Record<MobileTunnelStatus["mode"], { text: string; cls: string }> = {
  named: { text: "固定 URL (named)", cls: "bg-success/15 text-success" },
  quick: { text: "変動 URL (quick)", cls: "bg-warning/15 text-warning" },
  off: { text: "停止中", cls: "bg-surface-3 text-fg-muted" },
};

export function MobileTunnelCard({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["mobile-tunnel"],
    queryFn: pagesApi.mobileTunnelStatus,
    refetchInterval: 30_000,
  });

  const [token, setToken] = useState("");
  const [hostname, setHostname] = useState("");
  // 既設定の hostname をフォームに反映 (server 値が変わったときのみ)。
  useEffect(() => {
    if (data?.hostname) setHostname(data.hostname);
  }, [data?.hostname]);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["mobile-tunnel"] });

  const toggleMut = useMutation({
    mutationFn: (enable: boolean) =>
      enable ? pagesApi.mobileTunnelEnable() : pagesApi.mobileTunnelDisable(),
    onSuccess: invalidate,
  });

  const saveMut = useMutation<MobileTunnelStatus, Error, { token?: string; hostname?: string }>({
    mutationFn: (body) => pagesApi.mobileTunnelSetNamedConfig(body),
    onSuccess: () => {
      setToken(""); // 秘密をフォームに残さない
      invalidate();
    },
  });

  const clearMut = useMutation({
    mutationFn: () => pagesApi.mobileTunnelClearNamedConfig(),
    onSuccess: () => {
      setToken("");
      invalidate();
    },
  });

  const enabled = data?.enabled ?? false;
  const mode = data?.mode ?? "off";

  const save = () => {
    const body: { token?: string; hostname?: string } = { hostname: hostname.trim() };
    if (token.trim()) body.token = token.trim();
    saveMut.mutate(body);
  };

  return (
    <div className="rounded-lg border border-border-subtle bg-surface-1 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Globe className="h-4 w-4 text-fg-muted" />
          <span className="text-sm font-bold text-fg">モバイル公開 (tunnel)</span>
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              enabled ? (data?.url ? "bg-success" : "bg-warning") : "bg-surface-3"
            }`}
            title={enabled ? (data?.url ? "公開中" : "起動中 (URL 取得待ち)") : "停止中"}
          />
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${MODE_LABEL[mode].cls}`}
          >
            {MODE_LABEL[mode].text}
          </span>
        </div>
        {!readOnly && (
          <button
            onClick={() => {
              if (enabled) {
                toggleMut.mutate(false);
              } else if (
                confirm("閲覧専用画面を Cloudflare tunnel で外部公開します。よろしいですか?")
              ) {
                toggleMut.mutate(true);
              }
            }}
            disabled={toggleMut.isPending || !data}
            className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold disabled:opacity-50 ${
              enabled
                ? "border border-border-subtle bg-surface-2 text-fg hover:bg-surface-3"
                : "bg-accent text-white hover:bg-accent-hover"
            }`}
          >
            {toggleMut.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {enabled ? "公開を停止" : "公開を開始"}
          </button>
        )}
      </div>

      <div className="mt-2 space-y-1 text-xs text-fg-muted">
        {enabled && data?.url && (
          <div>
            公開 URL:{" "}
            <a
              href={data.url}
              target="_blank"
              rel="noreferrer"
              className="font-mono text-accent hover:underline break-all"
            >
              {data.url}
            </a>
          </div>
        )}
        {enabled && !data?.url && (
          <div>tunnel 起動中 — URL 検出待ち (数十秒かかることがあります)。</div>
        )}
        <p className="m-0 text-[11px] text-fg-subtle">
          公開されるのは閲覧専用インスタンス (書込 API は 403 固定) のみ。切替状態は再起動後も維持されます。
        </p>
      </div>

      {/* named tunnel 設定 (full instance のみ)。固定ドメインでの公開に切り替える。 */}
      {!readOnly && (
        <div className="mt-3 space-y-2 border-t border-border-subtle pt-3">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-fg">固定ドメイン (named tunnel)</span>
            {data?.token_set && (
              <span className="rounded bg-success/15 px-1.5 py-0.5 text-[10px] font-semibold text-success">
                token 設定済み
              </span>
            )}
          </div>
          <label className="block text-[11px] text-fg-muted">
            Cloudflare トンネル token
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={data?.token_set ? "設定済み（変更する場合のみ入力）" : "eyJ… を貼り付け"}
              autoComplete="off"
              spellCheck={false}
              className="mt-1 w-full rounded-md border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-xs text-fg"
            />
          </label>
          <label className="block text-[11px] text-fg-muted">
            公開ホスト名
            <input
              type="text"
              value={hostname}
              onChange={(e) => setHostname(e.target.value)}
              placeholder="kuebiko.example"
              autoComplete="off"
              spellCheck={false}
              className="mt-1 w-full rounded-md border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-xs text-fg"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={save}
              disabled={saveMut.isPending || (!token.trim() && !hostname.trim())}
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-hover disabled:opacity-50"
            >
              {saveMut.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              保存して反映
            </button>
            {data?.token_set && (
              <button
                onClick={() => {
                  if (
                    confirm(
                      "named tunnel 設定を解除して quick tunnel (変動 URL) に戻します。よろしいですか?",
                    )
                  ) {
                    clearMut.mutate();
                  }
                }}
                disabled={clearMut.isPending}
                className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs font-semibold text-fg hover:bg-surface-3 disabled:opacity-50"
              >
                解除 (quick に戻す)
              </button>
            )}
          </div>
          <p className="m-0 text-[11px] text-fg-subtle">
            token は 0600 で保存され、保存後は「設定済み」表示のみで生値は取得できません。保存すると数秒で自動反映されます
            (再デプロイ不要)。token は Cloudflare Zero Trust → Tunnels の作成画面で取得します。
          </p>
          {saveMut.isError && (
            <p className="m-0 text-[11px] text-critical">
              保存に失敗しました:{" "}
              {saveMut.error instanceof Error ? saveMut.error.message : "unknown"}
            </p>
          )}
        </div>
      )}

      {toggleMut.isError && (
        <p className="m-0 mt-2 text-xs text-critical">
          切替に失敗しました:{" "}
          {toggleMut.error instanceof Error ? toggleMut.error.message : "unknown"}
        </p>
      )}
    </div>
  );
}
