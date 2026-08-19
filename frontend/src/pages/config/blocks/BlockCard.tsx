// block 方式プロンプト編集の 1 block 分カード。summarizer の RubricSectionCard.tsx の
// 簡略版 — フィールド統計・contract バッジ・guide (「効く先」) は block 方式に対応する
// 概念が無いため出さない (block の kind は現状 backend の合成・検証で一切参照されない
// 実質的に死んだ値なので、判定基準の kind バッジも表示しない — 出しても何も説明しない)。

import { ChevronDown, ChevronRight } from "lucide-react";
import type { RubricSection } from "../../../api/promptRubric";
import { AutoGrowTextarea } from "../../../components/AutoGrowTextarea";

interface BlockCardProps {
  block: RubricSection;
  /** slots (skeleton のマーカー出現順) 内での 1-based 位置。見つからなければ null。 */
  order: number | null;
  changed: boolean;
  /** 同じ block id のカードが他にもある (編集は相互汚染するので止め、削除だけ許す)。 */
  duplicate: boolean;
  open: boolean;
  onToggle: () => void;
  onChangeBody: (body: string) => void;
  onRevert: () => void;
  onRemove: () => void;
}

export function BlockCard({
  block,
  order,
  changed,
  duplicate,
  open,
  onToggle,
  onChangeBody,
  onRevert,
  onRemove,
}: BlockCardProps) {
  const charCount = block.body.length;
  const borderClass = duplicate ? "border-critical" : "border-border-subtle";

  const handleRemove = () => {
    if (window.confirm(`重複しているカード「${block.title}」(${block.field_id}) を削除します。よろしいですか?`)) {
      onRemove();
    }
  };

  return (
    <div
      id={`block:${block.field_id}`}
      className={`scroll-mt-24 overflow-hidden rounded-lg border bg-surface-1 ${borderClass}`}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-2 px-4 py-2.5 text-left text-sm"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-fg-subtle" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-fg-subtle" />
        )}
        <span className="font-medium text-fg">{block.title}</span>
        <code className="font-mono text-[11px] text-fg-subtle">{block.field_id}</code>
        {order !== null && (
          <span
            className="rounded-sm bg-surface-3 px-1.5 py-px font-mono text-[10px] text-fg-subtle"
            title="プロンプト内の出現順 (skeleton のマーカー順)"
          >
            順序 #{order}
          </span>
        )}
        {duplicate && (
          <span className="rounded-sm bg-critical-soft px-1.5 py-px text-[10px] font-semibold text-critical">
            重複
          </span>
        )}
        {changed && (
          <span className="rounded-full bg-accent-soft px-1.5 py-px text-[10px] font-semibold text-accent-hover">
            ● 変更あり
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-2 border-t border-border-subtle px-4 pb-3 pt-2">
          {block.note && <p className="m-0 text-[11px] text-fg-subtle">{block.note}</p>}
          {duplicate ? (
            // 同じ block id のカードが複数ある状態。編集キーが field_id なので入力欄を
            // 出すともう片方も書き換わり、保存も backend が拒否する。直す唯一の手段
            // (不要な方の削除) だけを出す。
            <div className="space-y-1.5 rounded border border-critical bg-critical-soft px-2 py-1.5 text-[11px] leading-relaxed text-critical">
              <p className="m-0">
                同じ項目のカードが 2 枚以上あります。この状態では編集がもう一方にも及び、保存もできません。
                不要な方を削除してください。
              </p>
              <button
                type="button"
                onClick={handleRemove}
                className="rounded border border-critical px-1.5 py-0.5 font-semibold hover:bg-critical hover:text-fg-inverse"
              >
                このカードを削除
              </button>
            </div>
          ) : (
            <AutoGrowTextarea value={block.body} onChange={onChangeBody} />
          )}
          <div className="flex items-center justify-between text-[10px]">
            <button
              type="button"
              onClick={onRevert}
              disabled={!changed || duplicate}
              className="text-fg-subtle hover:text-fg disabled:cursor-not-allowed disabled:opacity-30"
            >
              変更を取り消す
            </button>
            <span className="tnum text-fg-subtle">{charCount} 字</span>
          </div>
        </div>
      )}
    </div>
  );
}
