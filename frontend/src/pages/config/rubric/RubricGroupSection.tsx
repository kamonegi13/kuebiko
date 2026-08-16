// summarizer 判定基準エディタ UI 再設計 (WP-F4): グループ見出し + 配下カード列挙。
// カード自体の構築は親 (SummarizerRubricEditor) の renderCard に委ねる (props 爆発回避)。

import type { ReactNode } from "react";
import type { RubricGroupId, RubricSection } from "../../../api/promptRubric";

interface RubricGroupSectionProps {
  groupId: RubricGroupId;
  label: string;
  description: string;
  /** 表示対象のセクション (検索フィルタ適用済)。表示順は親が guide.order で並べ替え済み。 */
  sections: RubricSection[];
  isOpen: (fieldId: string) => boolean;
  onToggleGroup: (open: boolean) => void;
  renderCard: (section: RubricSection) => ReactNode;
  errorCount: number;
  warnCount: number;
}

export function RubricGroupSection({
  groupId,
  label,
  description,
  sections,
  isOpen,
  onToggleGroup,
  renderCard,
  errorCount,
  warnCount,
}: RubricGroupSectionProps) {
  const allOpen = sections.length > 0 && sections.every((s) => isOpen(s.field_id));

  return (
    <section id={`group:${groupId}`} className="scroll-mt-24 space-y-2">
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle pb-1.5">
        <h4 className="m-0 text-sm font-semibold text-fg">{label}</h4>
        <span className="tnum text-[10.5px] text-fg-subtle">{sections.length} 件</span>
        {errorCount > 0 && (
          <span className="rounded-sm bg-critical-soft px-1.5 py-px text-[10px] font-semibold text-critical">
            ⚠ {errorCount}
          </span>
        )}
        {errorCount === 0 && warnCount > 0 && (
          <span className="rounded-sm bg-warning-soft px-1.5 py-px text-[10px] font-semibold text-warning">
            ⚠ {warnCount}
          </span>
        )}
        {sections.length > 0 && (
          <button
            type="button"
            onClick={() => onToggleGroup(!allOpen)}
            className="ml-auto text-[11px] text-accent hover:underline"
          >
            {allOpen ? "すべて閉じる" : "すべて開く"}
          </button>
        )}
      </div>
      <p className="m-0 text-[11px] text-fg-subtle">{description}</p>
      {sections.length === 0 ? (
        <p className="m-0 text-[11px] italic text-fg-subtle">一致するフィールドがありません。</p>
      ) : (
        <div className="space-y-2">
          {sections.map((s) => (
            <div key={s.field_id}>{renderCard(s)}</div>
          ))}
        </div>
      )}
    </section>
  );
}
