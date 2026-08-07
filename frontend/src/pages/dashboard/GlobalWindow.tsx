// ダッシュボード全体の対象期間セレクタ (ページヘッダ常設)。
//
// 旧配置の構造欠陥 (2026-07-14 修正): チップは KPI 概観 widget の内部にあり、その widget を
// ダッシュボードから外すと「全体に効く制御」自体が消えていた。全体制御はページ chrome に
// 属するのでヘッダへ移設。widget 個別の固定は ⚙ (ViewSettingsPill) が担う。
import { setOverviewWindow, useOverviewWindow, WINDOW_CHOICES } from "./overviewWindow";

export function GlobalWindowSelector() {
  const days = useOverviewWindow();
  return (
    <div
      className="inline-flex rounded-md border border-border-subtle overflow-hidden"
      title="各ウィジェット共通の表示期間 (⚙ でウィジェットごとに固定もできる)"
    >
      {WINDOW_CHOICES.map((c) => (
        <button
          key={c.value}
          onClick={() => setOverviewWindow(c.value)}
          className={`px-2 py-0.5 text-[11px] font-medium transition-colors ${
            days === c.value ? "bg-accent text-white" : "text-fg-muted hover:bg-surface-3"
          }`}
        >
          {c.label}
        </button>
      ))}
    </div>
  );
}
