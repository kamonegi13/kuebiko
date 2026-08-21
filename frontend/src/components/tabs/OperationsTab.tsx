// Operations tab — inline 化 (Phase Diamond verify-fix)。
// taxonomy_review / editorial_quality を sub-tab 形式で同 tab 内で完結させる。

import { useFilters } from "../../state/filters";
import { TaxonomyView } from "./operations/TaxonomyView";
import { EditorialView } from "./operations/EditorialView";
import { TuningLabelsCard } from "./operations/TuningLabelsCard";

export function OperationsTab() {
  const f = useFilters();

  return (
    <div className="space-y-3">
      <div className="px-1">
        <h3 className="m-0 mb-1 text-lg font-bold text-fg tracking-tight">運用</h3>
        <div className="text-fg-muted text-sm">分類語彙・アクター別名・論調の運用観察と手動確認</div>
      </div>

      {/* 較正格子 P1: 遅延正解ラベルの蓄積状況 (サブビュー横断の常設カード) */}
      <TuningLabelsCard />

      {/* サブビュー切替 (Taxonomy / Editorial) は上部コントロールバー (Shell) が担う。 */}
      {/* Active sub-view */}
      <div className="animate-fade-in pt-1">
        {f.op === "taxonomy" && <TaxonomyView />}
        {f.op === "editorial" && <EditorialView />}
      </div>
    </div>
  );
}
