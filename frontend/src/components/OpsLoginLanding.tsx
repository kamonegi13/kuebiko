import { loginUrl, type RuntimeFlags } from "../hooks/useRuntimeFlags";

/** 運用画面の公開側に未ログインで来た人の入口 (2026-10-08)。
 *
 *  運用画面はログインした運用者のためのもので、標準 / 拡張の切り替えは持たない。
 *  未ログインなら「ログインして運用画面を開く」と「公開版 (拡張) を開く」の 2 つだけを出す。 */
export function OpsLoginLanding({ flags }: { flags: RuntimeFlags }) {
  const publicOrigin = flags.public_site_origin || "";
  return (
    <main className="min-h-screen flex items-center justify-center bg-bg px-5">
      <div className="w-full max-w-sm space-y-5 text-center">
        <div>
          <div className="text-[22px] font-bold tracking-tight text-fg">kuebiko</div>
          <div className="text-[13px] text-fg-subtle">運用画面</div>
        </div>
        <p className="text-[14px] text-fg-muted leading-relaxed">
          運用画面を開くにはログインが必要です。
        </p>
        <div className="space-y-2.5">
          {flags.auth_available && (
            <a
              href={loginUrl()}
              className="block rounded-lg bg-accent px-4 py-2.5 text-[14px] font-medium text-white hover:opacity-90"
            >
              ログインして運用画面を開く
            </a>
          )}
          {publicOrigin && (
            <a
              href={`${publicOrigin}/app`}
              className="block rounded-lg border border-border px-4 py-2.5 text-[14px] text-fg hover:bg-surface-2"
            >
              公開版を開く
            </a>
          )}
        </div>
      </div>
    </main>
  );
}
