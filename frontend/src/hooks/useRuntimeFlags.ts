// Phase Diamond verify-mobile: runtime flag を起動時に取得し、write button の hide 等に使う。
// 2 instance 構成 (full / readonly) のうち readonly instance は READ_ONLY=1 → write button 非表示。

import { useQuery } from "@tanstack/react-query";

export interface RuntimeFlags {
  read_only: boolean;
  // Cloudflare Access の Tier1 状態 (2026-08-01)。authenticated なら fullOnly ページの
  // 閲覧とジョブ即時実行が解放される (write は引き続きローカル full instance のみ)。
  authenticated: boolean;
  // Access が設定済みか (未設定ならログイン導線そのものを出さない)
  auth_available: boolean;
  // 遠隔からの設定変更が開いているか (2026-08-29)。開いていても書けるのは
  // DB 由来の運用設定だけ。**遮断の実体は常にサーバ側の名簿** で、これは表示用。
  remote_write: boolean;
  // 公開版 (標準 + 拡張) のオリジン (2026-10-08)。運用画面の未ログインの入口が案内に使う。空 = 未設定
  public_site_origin?: string;
}

const ANONYMOUS: RuntimeFlags = {
  read_only: false,
  authenticated: false,
  auth_available: false,
  remote_write: false,
};

declare global {
  interface Window {
    // サーバが index.html に埋め込む初期値 (src/ui/app.py react_spa_fallback)。
    // fetch 完了前の初回ペイントから正しいメニューを描画する (サイドバー flash 根治)。
    __READ_ONLY__?: boolean;
    __AUTHENTICATED__?: boolean;
    __AUTH_AVAILABLE__?: boolean;
    __PUBLIC_SITE_ORIGIN__?: string;
  }
}

function seedFlags(): RuntimeFlags | undefined {
  if (typeof window === "undefined" || typeof window.__READ_ONLY__ !== "boolean") return undefined;
  return {
    read_only: window.__READ_ONLY__,
    authenticated: window.__AUTHENTICATED__ === true,
    auth_available: window.__AUTH_AVAILABLE__ === true,
    // 初期値には無い (埋め込みは認証状態までで十分)。fetch 後に確定する。
    remote_write: false,
    // 公開版のオリジン (2026-10-08)。staleTime が無限で fetch し直さないので、埋め込みから読む
    public_site_origin:
      typeof window.__PUBLIC_SITE_ORIGIN__ === "string" ? window.__PUBLIC_SITE_ORIGIN__ : "",
  };
}

async function fetchRuntimeFlags(): Promise<RuntimeFlags> {
  try {
    const r = await fetch("/api/v1/runtime-flags", { credentials: "same-origin" });
    if (!r.ok) return ANONYMOUS;
    return { ...ANONYMOUS, ...((await r.json()) as Partial<RuntimeFlags>) };
  } catch {
    return ANONYMOUS;
  }
}

export function useRuntimeFlags(): RuntimeFlags {
  const { data } = useQuery({
    queryKey: ["runtime-flags"],
    queryFn: fetchRuntimeFlags,
    // 埋め込み seed があれば初回ペイントから確定値 (fetch は裏で整合を取るだけ)
    initialData: seedFlags,
    staleTime: Infinity,
    // ⚠ 前面復帰では**必ず**取り直す。iOS の PWA は Cloudflare Access のログインを
    // 別ドメインのアプリ内ブラウザで行うため、ログイン完了はこのアプリの外で起きる。
    // 起動時 1 回の取得だと、ログイン後にオーバーレイを閉じても匿名の表示が続く
    // (2026-08-27 実測: ✕ で閉じると Tier0 に戻るだけだった)。"always" は
    // staleTime: Infinity を無視して再取得する。エンドポイントは軽量 (即答の JSON)。
    refetchOnWindowFocus: "always",
    refetchOnReconnect: true,
  });
  return data || ANONYMOUS;
}

// fullOnly (編集/操作系) ページを隠すか。readonly instance かつ未認証のときだけ隠す。
// Sidebar / CommandPalette / App のルートガードが同じ判定を共有する。
export function shouldHideFullOnly(flags: RuntimeFlags): boolean {
  return flags.read_only && !flags.authenticated;
}

/** ログイン導線の URL。
 *
 * インストール型 (standalone PWA) からのログインは、完了ページを出すための
 * 目印 `?display=standalone` を付ける。iOS は別ドメインの Access ログインを
 * アプリ内ブラウザで開くため、完了後に /app/ を返すとアプリ全体がオーバーレイ内に
 * 描画されて紛らわしい (2026-08-27 実測)。ブラウザからのログインは従来どおり
 * /app/ へ戻る。 */
export function loginUrl(origin = ""): string {
  const standalone =
    typeof window !== "undefined" &&
    (window.matchMedia?.("(display-mode: standalone)").matches ||
      // iOS Safari 旧来の判定 (navigator.standalone は非標準)
      (navigator as { standalone?: boolean }).standalone === true);
  return `${origin}/auth/login${standalone ? "?display=standalone" : ""}`;
}

/** DB 由来の運用設定を、この画面から変更できるか。
 *
 *  ⚠ **ファイル由来の操作 (接続設定 / raw YAML / .j2 直編集 / 名簿 yaml) には使わない。**
 *  それらは遠隔からは書けないので、素の read_only で隠したままにする。
 *  ここで true を返しても、実際に通るかはサーバ側の名簿が決める — 画面は
 *  「出すか隠すか」だけを決める (2026-08-01 の fullOnly と同じ関係)。
 */
export function canEditOperationalConfig(flags: RuntimeFlags): boolean {
  if (!flags.read_only) return true; // ローカル (full instance)
  return flags.authenticated && flags.remote_write;
}
