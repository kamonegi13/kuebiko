// dashboard 全体で共有する「選択中の国」状態。
//
// 脅威マップページ (MapPage.tsx) は地図と被害国ランキングが同一画面内の state を共有して
// 連動する (地図/ランキングどちらで国を選んでも同じ記事一覧が右パネルに出る)。dashboard では
// mini_map / geo_ranking widget が別々の Component として独立に配置されるため、同じ連動を
// 実現するには widget を跨ぐ共有 state が要る → DashboardPage が提供する React Context。
//
// Provider の外 (widget 単体テスト・ツールボックスの hover プレビュー等) でも useDashboardSelection
// を安全に呼べるよう、Provider が無ければ no-op (常に非選択・選択しても何も起きない) を返す。
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export interface DashboardSelection {
  selectedIso: string | null;
  // 同じ iso を渡すと選択解除 (トグル)。クリックしなおす/× ボタンでの解除もこれを呼ぶ。
  selectIso: (iso: string | null) => void;
}

const NOOP_SELECTION: DashboardSelection = {
  selectedIso: null,
  selectIso: () => {},
};

const DashboardSelectionContext = createContext<DashboardSelection | null>(null);

export function DashboardSelectionProvider({ children }: { children: ReactNode }) {
  const [selectedIso, setSelectedIso] = useState<string | null>(null);
  const value = useMemo<DashboardSelection>(
    () => ({
      selectedIso,
      selectIso: (iso) => setSelectedIso((prev) => (iso == null || prev === iso ? null : iso)),
    }),
    [selectedIso],
  );
  return <DashboardSelectionContext.Provider value={value}>{children}</DashboardSelectionContext.Provider>;
}

export function useDashboardSelection(): DashboardSelection {
  return useContext(DashboardSelectionContext) ?? NOOP_SELECTION;
}
