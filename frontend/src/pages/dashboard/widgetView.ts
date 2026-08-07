// ウィジェット個別の「閲覧時設定」(view overrides) ストア。
//
// 状態の 3 層モデル (2026-07-14):
//   実効 config = defaultConfig < layout.config < ここ (view overrides)
//                 (コード既定)    (構造・共有既定。      (端末ローカル・モードレス。
//                                 カスタマイズモードで編集)  ⚙ ポップオーバーで即時変更)
//
// 「構造 (何をどこに置くか)」と「見え方 (期間・件数・表示形式)」は寿命が違う —
// 前者は draft→保存のトランザクション (カスタマイズモード) が適切、後者は即時反映・
// 保存儀式なしが適切。overviewWindow が確立した「閲覧状態は localStorage・モードレス」
// の層を widget 個別設定へ一般化したもの。readonly / モバイルでもサーバ write 不要で動く。

import { useSyncExternalStore } from "react";

type Overrides = Record<string, Record<string, string>>;

const KEY = "cti.dashboard.widget-view";

function load(): Overrides {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Overrides) : {};
  } catch {
    return {};
  }
}

let current: Overrides = load();
const listeners = new Set<() => void>();

function persist(): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    /* localStorage 不可環境は無視 (セッション内のみ有効) */
  }
}

function notify(): void {
  persist();
  listeners.forEach((l) => l());
}

/** uid の option 1 つを上書きする (即時反映)。 */
export function setWidgetViewValue(uid: string, key: string, value: string): void {
  current = { ...current, [uid]: { ...(current[uid] ?? {}), [key]: value } };
  notify();
}

/** uid の上書きを全消去 = 共有既定 (layout.config) に戻す。 */
export function clearWidgetView(uid: string): void {
  if (!current[uid]) return;
  const next = { ...current };
  delete next[uid];
  current = next;
  notify();
}

/** レイアウトに存在しない uid の残骸を掃除する (widget 削除後の孤児 override)。 */
export function pruneWidgetView(liveUids: string[]): void {
  const live = new Set(liveUids);
  const orphans = Object.keys(current).filter((uid) => !live.has(uid));
  if (orphans.length === 0) return;
  const next = { ...current };
  for (const uid of orphans) delete next[uid];
  current = next;
  notify();
}

/** 全 override を購読する (DashboardPage が cfg merge に使う)。 */
export function useWidgetViewAll(): Overrides {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => current,
    () => current,
  );
}
