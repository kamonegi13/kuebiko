// summarizer プロンプトの層分け (WP3): 判定基準 1 フィールド分のカード。
// 型/必須/enum バッジは contract (SummaryOutput の JSON Schema 由来) からのみ描画する
// — フロントに型表を複製しない (ラベル SSoT 規約)。kind (rubric/suppressed) は
// 生 enum のまま出さず日本語に写像する。

import { useEffect, useRef } from "react";
import type { ContractField, RubricSection, RubricSectionKind } from "../../api/promptRubric";

const KIND_LABEL: Record<RubricSectionKind, string> = {
  rubric: "判定基準",
  suppressed: "出力させない",
};

function TypeBadges({ contract }: { contract: ContractField | undefined }) {
  if (!contract) return null;
  return (
    <>
      <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-mono">
        {contract.json_type}
      </span>
      {contract.required && (
        <span className="text-[10px] px-1.5 py-px rounded-sm bg-warning-soft text-warning font-semibold">
          必須
        </span>
      )}
      {contract.nullable && (
        <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle">省略可</span>
      )}
      {contract.enum.length > 0 && (
        <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-mono">
          {contract.enum.join(" / ")}
        </span>
      )}
    </>
  );
}

// auto-grow textarea (mono)。scrollHeight を反映して高さを内容に追従させる。
function AutoGrowTextarea({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={2}
      spellCheck={false}
      className="w-full resize-none overflow-hidden rounded border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-xs text-fg leading-relaxed"
    />
  );
}

interface RubricSectionCardProps {
  section: RubricSection;
  contract: ContractField | undefined;
  onChangeBody: (body: string) => void;
}

export function RubricSectionCard({ section, contract, onChangeBody }: RubricSectionCardProps) {
  const charCount = section.body.length;
  const isEmpty = section.body.trim().length === 0;

  // suppressed (「出力させない」) フィールドは折りたたみ既定。editorial_stance のように
  // body が非空のものは合成時に実際に出力されるため、編集自体は rubric と同じ textarea。
  if (section.kind === "suppressed") {
    return (
      <details className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <summary className="cursor-pointer px-4 py-2.5 flex items-center gap-2 flex-wrap text-sm select-none">
          <span className="text-fg font-medium">{section.title}</span>
          <code className="text-[11px] text-fg-subtle font-mono">{section.field_id}</code>
          <TypeBadges contract={contract} />
          <span className="ml-auto text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-semibold">
            {KIND_LABEL.suppressed}
          </span>
        </summary>
        <div className="px-4 pb-3 pt-2 border-t border-border-subtle space-y-1.5">
          {section.note && <p className="m-0 text-[11px] text-fg-subtle">{section.note}</p>}
          <AutoGrowTextarea value={section.body} onChange={onChangeBody} />
          <div className="text-right text-[10px] text-fg-subtle tnum">{charCount} 字</div>
        </div>
      </details>
    );
  }

  return (
    <div
      className={`bg-surface-1 border rounded-lg p-4 space-y-1.5 ${
        isEmpty ? "border-critical" : "border-border-subtle"
      }`}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-fg font-medium text-sm">{section.title}</span>
        <code className="text-[11px] text-fg-subtle font-mono">{section.field_id}</code>
        <TypeBadges contract={contract} />
      </div>
      <AutoGrowTextarea value={section.body} onChange={onChangeBody} />
      <div className="flex items-center justify-between text-[10px]">
        {isEmpty && <span className="text-critical">判定基準が空です</span>}
        <span className="ml-auto text-fg-subtle tnum">{charCount} 字</span>
      </div>
    </div>
  );
}
