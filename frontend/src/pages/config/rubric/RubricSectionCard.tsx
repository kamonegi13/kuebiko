// summarizer 判定基準エディタ UI 再設計 (WP-F3): 判定基準 1 フィールド分のカード。
// 型/必須/enum バッジは contract (SummaryOutput の JSON Schema 由来) からのみ描画する
// — フロントに型表を複製しない (ラベル SSoT 規約)。kind (rubric/suppressed) は
// 生 enum のまま出さず日本語に写像する。
//
// 旧 config/RubricSectionCard.tsx からの変更点:
//   - <details> をやめ制御コンポーネント化 (目次からのジャンプで強制展開するため)
//   - 効く先 (guide) / 統計 (FieldStatBlock) / issue 局在化 / プロンプト出力順バッジ / 変更取消 を追加
//   - 「本文が空」の判定は preview.issues (empty_rubric_body) に一本化し、ローカルで再判定しない

import { useEffect, useRef } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type {
  ContractField,
  FieldStat,
  RubricFieldGuide,
  RubricIssue,
  RubricSection,
  RubricSectionKind,
  StatsDays,
} from "../../../api/promptRubric";
import { FieldStatBlock } from "./FieldStatBlock";
import { previewLine } from "./format";

const KIND_LABEL: Record<RubricSectionKind, string> = {
  rubric: "判定基準",
  suppressed: "出力させない",
};

function TypeBadges({ contract }: { contract: ContractField | undefined }) {
  if (!contract) return null;
  return (
    <>
      <span className="rounded-sm bg-surface-3 px-1.5 py-px font-mono text-[10px] text-fg-subtle">
        {contract.json_type}
      </span>
      {contract.required && (
        <span className="rounded-sm bg-warning-soft px-1.5 py-px text-[10px] font-semibold text-warning">必須</span>
      )}
      {contract.nullable && (
        <span className="rounded-sm bg-surface-3 px-1.5 py-px text-[10px] text-fg-subtle">省略可</span>
      )}
      {contract.enum.length > 0 && (
        <span className="rounded-sm bg-surface-3 px-1.5 py-px font-mono text-[10px] text-fg-subtle">
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
      className="w-full resize-none overflow-hidden rounded border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-xs leading-relaxed text-fg"
    />
  );
}

interface RubricSectionCardProps {
  section: RubricSection;
  contract: ContractField | undefined;
  guide: RubricFieldGuide | undefined;
  stat: FieldStat | undefined;
  statsDays: StatsDays;
  statsLoading: boolean;
  /** body 非空セクション中の 1-based プロンプト出力順。空セクションは null (= 未出力)。 */
  promptOrder: number | null;
  issues: RubricIssue[];
  changed: boolean;
  open: boolean;
  onToggle: () => void;
  onChangeBody: (body: string) => void;
  onRevert: () => void;
}

export function RubricSectionCard({
  section,
  contract,
  guide,
  stat,
  statsDays,
  statsLoading,
  promptOrder,
  issues,
  changed,
  open,
  onToggle,
  onChangeBody,
  onRevert,
}: RubricSectionCardProps) {
  const charCount = section.body.length;
  const errorCount = issues.filter((i) => i.severity === "error").length;
  const warnCount = issues.length - errorCount;
  const suppressedWithBody = section.kind === "suppressed" && section.body.trim().length > 0;
  const borderClass = errorCount > 0 ? "border-critical" : warnCount > 0 ? "border-warning" : "border-border-subtle";

  return (
    <div
      id={`field:${section.field_id}`}
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
        <span className="font-medium text-fg">{section.title}</span>
        <code className="font-mono text-[11px] text-fg-subtle">{section.field_id}</code>
        <TypeBadges contract={contract} />
        {section.kind === "suppressed" && (
          <span className="rounded-sm bg-surface-3 px-1.5 py-px text-[10px] font-semibold text-fg-subtle">
            {KIND_LABEL.suppressed}
          </span>
        )}
        {promptOrder !== null ? (
          <span
            className="rounded-sm bg-surface-3 px-1.5 py-px font-mono text-[10px] text-fg-subtle"
            title="プロンプト出力順 (本文が空でないセクション中の位置)"
          >
            出力順 #{promptOrder}
          </span>
        ) : (
          <span
            className="rounded-sm bg-surface-3 px-1.5 py-px text-[10px] text-fg-subtle"
            title="本文が空のためプロンプトに出力されません"
          >
            未出力
          </span>
        )}
        {changed && (
          <span className="rounded-full bg-accent-soft px-1.5 py-px text-[10px] font-semibold text-accent-hover">
            ● 変更あり
          </span>
        )}
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
        {!open && (
          <span className="ml-auto flex min-w-0 items-center gap-2 text-[11px]">
            <FieldStatBlock stat={stat} loading={statsLoading} days={statsDays} compact />
            {section.body.trim() && (
              <span className="max-w-[240px] truncate italic text-fg-subtle">{previewLine(section.body)}</span>
            )}
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-2 border-t border-border-subtle px-4 pb-3 pt-2">
          {guide && (
            <p className="m-0 text-[11px] text-fg-muted" title={guide.sources.join("\n")}>
              効く先: {guide.effect}
            </p>
          )}
          {section.note && <p className="m-0 text-[11px] text-fg-subtle">{section.note}</p>}
          {suppressedWithBody && (
            <p className="m-0 text-[11px] text-warning">本文があるためプロンプトに出力されます。</p>
          )}
          <FieldStatBlock stat={stat} loading={statsLoading} days={statsDays} />
          {issues.map((issue, i) => (
            <div
              key={`${issue.code}-${i}`}
              className={`rounded px-1.5 py-0.5 text-[11px] ${
                issue.severity === "error" ? "bg-critical-soft text-critical" : "bg-warning-soft text-warning"
              }`}
            >
              {issue.message}
            </div>
          ))}
          {guide?.editable === false ? (
            /* 下流が必ず上書きするフィールド。空の入力欄は「ここに書けば判定される」と
               誤解させるので、書ける場所を出さずに理由を書く (2026-08-18)。 */
            <p className="m-0 rounded border border-border-subtle bg-surface-2 px-2 py-1.5 text-[11px] leading-relaxed text-fg-muted">
              この項目は取込時の分類器が確定するため、判定基準を書いても出力は上書きされます。
              {guide.sources.length > 0 && <> 判定は <code className="text-fg-subtle">{guide.sources[0]}</code> にあります。</>}
            </p>
          ) : (
            <AutoGrowTextarea value={section.body} onChange={onChangeBody} />
          )}
          <div className="flex items-center justify-between text-[10px]">
            <button
              type="button"
              onClick={onRevert}
              disabled={!changed}
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
