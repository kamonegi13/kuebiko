// 概観ウィジェット共通の「対象期間」ストア (発見6: 帯内の窓不一致を解消)。
// configurable widget grid では各 widget が独立に描画されるため、prop drilling せず
// module-level の小さな observable + useSyncExternalStore で全 overview widget が
// 同一の窓を共有する。選択は localStorage に保存 (readonly/mobile でも write 不要)。

import { useSyncExternalStore } from "react";

export type WindowDays = 1 | 7 | 30 | 90;

export const WINDOW_CHOICES: { value: WindowDays; label: string }[] = [
  { value: 1, label: "24h" },
  { value: 7, label: "7日" },
  { value: 30, label: "30日" },
  // 国家アクター行動など遅い信号用の長窓 (全 API が 90 日まで検証済み)
  { value: 90, label: "90日" },
];

const KEY = "cti.dashboard.overview.window";

function initial(): WindowDays {
  try {
    const n = Number(localStorage.getItem(KEY));
    return n === 1 || n === 30 || n === 90 ? n : 7;
  } catch {
    return 7;
  }
}

let current: WindowDays = initial();
const listeners = new Set<() => void>();

export function setOverviewWindow(d: WindowDays): void {
  if (d === current) return;
  current = d;
  try {
    localStorage.setItem(KEY, String(d));
  } catch {
    /* localStorage 不可環境は無視 */
  }
  listeners.forEach((l) => l());
}

export function useOverviewWindow(): WindowDays {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => current,
    () => current,
  );
}

// ── 共有窓 → synthesis 期間の写像 ──
// synthesis の render 軸は daily/weekly/monthly の 3 種 (四半期はない) のため、
// 30日/90日はともに monthly に落とす。ダッシュボードから synthesis へ遷移する
// リンクと窓連動 widget が同じ写像を使う (遷移先と表示内容の一致)。
export type SynthesisPeriod = "daily" | "weekly" | "monthly";

export function synthesisPeriodForWindow(days: number): SynthesisPeriod {
  if (days <= 1) return "daily";
  if (days <= 7) return "weekly";
  return "monthly";
}

// ── widget 個別の期間 (連動/固定) ──
// config.days が数値なら固定、"auto"/未設定なら共有窓に連動する。発見6 (帯内の窓統一)
// を既定で維持しつつ、個別固定を閲覧設定として許す。固定中の divergence は
// ViewSettingsPill の窓チップが可視化する。
export function useWidgetWindow(config?: Record<string, unknown>): number {
  const shared = useOverviewWindow();
  const raw = config?.days;
  const n =
    raw === undefined || raw === null || raw === "auto" || raw === ""
      ? Number.NaN
      : Number(raw);
  return Number.isFinite(n) ? n : shared;
}
