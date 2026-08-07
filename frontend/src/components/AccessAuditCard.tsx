// 外部公開の認証 (Cloudflare Access) の状態 + アクセス履歴カード。
//
// なぜ画面が要るか: 監査証跡は「後から遡って調べる」ためのもので、閲覧しやすさが
// 機能の一部。API だけで画面が無いと実質見ないのと変わらない。
//
// 置き場が「設定 → 接続」である理由: 同タブにあるモバイル公開 (外部公開そのもの) に対し、
// 認証は「その公開面に誰が入れるか」の設定なので同格で隣に並ぶ。
//
// 記録の方針 (src/ui/read_only_policy.py):
//   - authenticated: 認証済みで運用系 API に到達 (同一 subject は 10 分に集約)
//   - rejected: 資格情報を提示したが検証に失敗 (期限切れ / 偽造)
//   - tier1_write: 認証済みのジョブ即時実行 (集約せず必ず記録)
//   - トークン無しの匿名アクセスは記録しない (bot で証跡が埋もれるため)
// §4 により email は保存しない。識別は subject_hash (SHA-256 先頭 12 桁)。

import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { pagesApi } from "../api/pages";
import { formatJst } from "../utils/date";

const EVENT_META: Record<string, { text: string; cls: string }> = {
  authenticated: { text: "認証成功", cls: "bg-success/15 text-success" },
  rejected: { text: "認証拒否", cls: "bg-critical/15 text-critical" },
  tier1_write: { text: "即時実行", cls: "bg-accent-subtle text-accent" },
};

export function AccessAuditCard() {
  const { data, isError } = useQuery({
    queryKey: ["access-audit"],
    queryFn: () => pagesApi.accessAudit(50),
    refetchInterval: 5 * 60_000,
  });

  const configured = data?.auth.configured ?? false;
  const events = data?.events ?? [];

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-fg-muted" />
        <h3 className="m-0 text-sm font-semibold text-fg">外部公開の認証 (Cloudflare Access)</h3>
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
            configured ? "bg-success/15 text-success" : "bg-surface-3 text-fg-muted"
          }`}
        >
          {configured ? "設定済み" : "未設定"}
        </span>
      </div>

      <p className="m-0 text-[11px] text-fg-subtle leading-relaxed">
        {configured ? (
          <>
            公開ページ (匿名で閲覧可) に加えて、認証した利用者だけが運用ページの閲覧と
            ジョブの即時実行を行えます。書き込み設定はこの PC からのみ可能です。
          </>
        ) : (
          <>
            未設定のため、公開ページは匿名閲覧のみです (運用ページは公開面から遮断)。
            有効化の手順は docs/mobile-access.md を参照してください。
          </>
        )}
      </p>

      {configured && data?.auth.team_domain && (
        <div className="text-[11px] text-fg-subtle font-mono break-all">
          {data.auth.team_domain}
        </div>
      )}

      <div className="space-y-1.5">
        <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">
          アクセス履歴 (最新 {events.length} 件 / 保持 180 日)
        </h4>
        {isError || data?.error ? (
          <div className="text-[11px] text-critical">
            履歴を読み出せませんでした{data?.error ? `: ${data.error}` : ""}
          </div>
        ) : events.length === 0 ? (
          <div className="text-[11px] text-fg-subtle">
            まだ記録がありません。
            {configured && "ログイン・認証失敗・即時実行が発生すると記録されます。"}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead className="text-fg-subtle">
                <tr className="[&>th]:text-left [&>th]:font-normal [&>th]:pb-1 [&>th]:pr-3">
                  <th>日時</th>
                  <th>種別</th>
                  <th>接続元</th>
                  <th>対象</th>
                </tr>
              </thead>
              <tbody className="[&>tr>td]:py-1 [&>tr>td]:pr-3 [&>tr>td]:align-top">
                {events.map((e, i) => {
                  const meta = EVENT_META[e.event] ?? {
                    text: e.event,
                    cls: "bg-surface-3 text-fg-muted",
                  };
                  return (
                    <tr key={`${e.at}-${i}`} className="border-t border-border-subtle">
                      <td className="text-fg-subtle whitespace-nowrap">{formatJst(e.at)}</td>
                      <td>
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${meta.cls}`}
                        >
                          {meta.text}
                        </span>
                      </td>
                      <td className="text-fg-subtle font-mono break-all min-w-0">
                        {e.client_ip || "—"}
                        {e.country && <span className="ml-1.5">{e.country}</span>}
                      </td>
                      <td className="text-fg-subtle break-all min-w-0">
                        {e.path || e.detail || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="m-0 text-[10.5px] text-fg-subtle">
          メールアドレスは記録していません (識別は匿名化したハッシュのみ)。
          資格情報を提示しない匿名アクセスは記録対象外です。
        </p>
      </div>
    </div>
  );
}

// 接続タブ用のコンパクト表示 (2026-08-02 設定画面の整理)。
//
// 認証の**設定状態**はモバイル公開 (何を公開するか) と一体で見たいので接続タブに 1 行だけ置き、
// **履歴そのもの**は「履歴・監査」タブへ寄せた。設定と記録は別の種類のものなので混ぜない。
export function AccessStatusLine() {
  const { data } = useQuery({
    queryKey: ["access-audit"],
    queryFn: () => pagesApi.accessAudit(1),
    staleTime: 60_000,
  });
  const configured = data?.auth.configured ?? false;

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg px-4 py-3 flex items-start gap-2 flex-wrap">
      <ShieldCheck className="h-4 w-4 text-fg-muted mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-semibold text-fg">公開面の認証 (Cloudflare Access)</span>
          <span
            className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
              configured ? "bg-success/15 text-success" : "bg-surface-3 text-fg-muted"
            }`}
          >
            {configured ? "設定済み" : "未設定"}
          </span>
        </div>
        <p className="m-0 text-[11px] text-fg-subtle">
          {configured
            ? "認証した利用者だけが運用ページの閲覧とジョブの即時実行を行えます。"
            : "未設定のため公開面は匿名閲覧のみです (運用ページは遮断)。"}
          <a href="/app/config#history" className="text-accent hover:underline ml-1.5">
            アクセス履歴を見る →
          </a>
        </p>
      </div>
    </div>
  );
}
