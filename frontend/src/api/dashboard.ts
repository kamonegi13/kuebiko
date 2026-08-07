// ダッシュボード レイアウト (configurable widgets) API client。
// backend: src/ui/api/dashboard_layout.py
//
// v2 (2026-07-10): 12 カラム × 8px 行グリッドの明示座標 (x,y,w,h)。編集/表示が同一座標を
// 使うため「保存するとズレる」が構造的に起きない。旧 v1 (順序+span+実測 masonry) の
// レイアウトは読込時のみ受理し、DashboardPage の migrateLayout が座標へ移行する。

// v2: 明示座標。単位は 12 カラム × 8px 行 (margin 12px → 1 行増加あたり実効 20px)。
export interface WidgetPlacement {
  id: string;
  // 一意のインスタンス ID。同一 widget を設定違いで複数配置するための key。
  // 旧レイアウト (uid 無し) は load 時に採番する。
  uid?: string;
  x: number; // 0..11
  y: number; // 0..
  w: number; // 1..12
  h: number; // 行数 (1 行 ≈ 20px)
  // per-widget 設定 (軸/件数/期間 等)。widget が解釈する。
  config?: Record<string, unknown>;
}

// legacy v1 (順序 + span フローグリッド)。読込専用 — migrateLayout が v2 座標へ変換する。
export interface LegacyWidgetPlacement {
  id: string;
  uid?: string;
  span?: 1 | 2 | 3 | 4; // 4 カラム基準の幅
  h?: number | null; // 固定高さ px (null/未指定 = auto)
  config?: Record<string, unknown>;
}

export type StoredWidgetPlacement = WidgetPlacement | LegacyWidgetPlacement;

// 読込時の形 (v1/v2 混在可能性あり)。migrateLayout を通すと DashboardLayout (v2) になる。
export interface StoredDashboardLayout {
  widgets: StoredWidgetPlacement[];
}

export interface DashboardLayout {
  widgets: WidgetPlacement[];
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
  return (await r.json()) as T;
}

// モバイル専用レイアウトは localStorage に保存する。
// 理由: モバイルは readonly instance 経由でアクセスされ server write が 403。
// localStorage なら write 不要で保存でき、PC (server 保存) とも独立に持てる。
const MOBILE_LAYOUT_KEY = "cti.dashboard.layout.mobile";

export function loadMobileLayout(): StoredDashboardLayout | null {
  try {
    const raw = localStorage.getItem(MOBILE_LAYOUT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredDashboardLayout;
    return Array.isArray(parsed.widgets) ? parsed : null;
  } catch {
    return null;
  }
}

export function saveMobileLayout(layout: DashboardLayout): void {
  try {
    localStorage.setItem(MOBILE_LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    /* localStorage 不可環境は黙って無視 */
  }
}

// PC レイアウト: 基本は server (PUT) に保存するが、外部 readonly 経由では write が 403。
// その場合この端末の localStorage に fallback 保存する (= 端末ごとにレイアウトを持てる)。
// load 時は localStorage override > server (共有 default)。書込可能 PC は server 保存成功時に
// override を消し、server (共有) に追従させる。
const PC_LAYOUT_KEY = "cti.dashboard.layout.pc";

export function loadPcLayout(): StoredDashboardLayout | null {
  try {
    const raw = localStorage.getItem(PC_LAYOUT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredDashboardLayout;
    return Array.isArray(parsed.widgets) ? parsed : null;
  } catch {
    return null;
  }
}

export function savePcLayout(layout: DashboardLayout): void {
  try {
    localStorage.setItem(PC_LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    /* ignore */
  }
}

export function clearPcLayout(): void {
  try {
    localStorage.removeItem(PC_LAYOUT_KEY);
  } catch {
    /* ignore */
  }
}

export const dashboardLayoutApi = {
  // GET は legacy v1 (span) を素通しで返しうる (backend は移行しない)。
  get: () => getJson<StoredDashboardLayout>("/api/v1/dashboard/layout"),
  // PUT は v2 のみ受理 (backend が extra=forbid で legacy キーを拒否)。
  put: async (layout: DashboardLayout): Promise<DashboardLayout> => {
    const r = await fetch("/api/v1/dashboard/layout", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(layout),
      credentials: "same-origin",
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    return (await r.json()) as DashboardLayout;
  },
};
