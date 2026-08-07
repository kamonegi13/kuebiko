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
}

const ANONYMOUS: RuntimeFlags = { read_only: false, authenticated: false, auth_available: false };

declare global {
  interface Window {
    // サーバが index.html に埋め込む初期値 (src/ui/app.py react_spa_fallback)。
    // fetch 完了前の初回ペイントから正しいメニューを描画する (サイドバー flash 根治)。
    __READ_ONLY__?: boolean;
    __AUTHENTICATED__?: boolean;
    __AUTH_AVAILABLE__?: boolean;
  }
}

function seedFlags(): RuntimeFlags | undefined {
  if (typeof window === "undefined" || typeof window.__READ_ONLY__ !== "boolean") return undefined;
  return {
    read_only: window.__READ_ONLY__,
    authenticated: window.__AUTHENTICATED__ === true,
    auth_available: window.__AUTH_AVAILABLE__ === true,
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
    staleTime: Infinity, // 起動時 1 回 fetch、以降 cache
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  return data || ANONYMOUS;
}

// fullOnly (編集/操作系) ページを隠すか。readonly instance かつ未認証のときだけ隠す。
// Sidebar / CommandPalette / App のルートガードが同じ判定を共有する。
export function shouldHideFullOnly(flags: RuntimeFlags): boolean {
  return flags.read_only && !flags.authenticated;
}
