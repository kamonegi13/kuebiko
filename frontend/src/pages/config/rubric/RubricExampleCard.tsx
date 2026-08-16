// summarizer 判定基準エディタ UI 再設計 (WP-F4): 出力例 1 件のカード。
// キー単位の不正表示 (要件3): example_schema_mismatch issue の keys を使い、
// 該当キーをチップ表示 + JSON テキスト内の宣言行を検出して行番号を出す
// (textarea 内ハイライトは CodeMirror 等の依存を増やさないため行わない)。

import { useMemo } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { RubricExample, RubricIssue } from "../../../api/promptRubric";

interface RubricExampleCardProps {
  index: number; // 0-based
  example: RubricExample;
  issues: RubricIssue[]; // example_index === index+1 のもの
  changed: boolean;
  open: boolean;
  onToggle: () => void;
  onChange: (patch: Partial<RubricExample>) => void;
  onRevert: () => void;
}

function validateJson(text: string): string | null {
  try {
    JSON.parse(text);
    return null;
  } catch (e: unknown) {
    return e instanceof Error ? e.message : "不明なエラー";
  }
}

/** JSON テキスト中で該当キーを最初に宣言している行番号 (1-based) を探す。無ければ null。 */
function findKeyLine(jsonText: string, key: string): number | null {
  const needle = `"${key}"`;
  const lines = jsonText.split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes(needle)) return i + 1;
  }
  return null;
}

export function RubricExampleCard({
  index,
  example,
  issues,
  changed,
  open,
  onToggle,
  onChange,
  onRevert,
}: RubricExampleCardProps) {
  const jsonError = validateJson(example.json_text);
  // keys を持つのは現状 example_schema_mismatch だけだが、code で絞らないと将来
  // keys 付きの code が増えたとき「チップ」と「メッセージ」で二重表示になる。
  const mismatchKeys = useMemo(
    () =>
      Array.from(
        new Set(issues.filter((i) => i.code === "example_schema_mismatch").flatMap((i) => i.keys)),
      ),
    [issues],
  );
  const errorCount = issues.filter((i) => i.severity === "error").length;
  const warnCount = issues.length - errorCount;
  const otherIssues = issues.filter((i) => i.code !== "example_schema_mismatch");
  const anchorId = `example:${index + 1}`;

  return (
    <div
      id={anchorId}
      className={`scroll-mt-24 overflow-hidden rounded-lg border bg-surface-1 ${
        jsonError || errorCount > 0 ? "border-critical" : warnCount > 0 ? "border-warning" : "border-border-subtle"
      }`}
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
        <span className="font-medium text-fg">
          出力例 {index + 1}
          {example.label ? `（${example.label}）` : ""}
        </span>
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
        {/* schema 不一致は warning (保存は拒否しない) なので警告色で揃える */}
        {!open && mismatchKeys.length > 0 && (
          <span className="ml-auto flex flex-wrap gap-1">
            {mismatchKeys.map((k) => (
              <span key={k} className="rounded-sm bg-warning-soft px-1.5 py-px font-mono text-[10px] text-warning">
                ⚠ {k}
              </span>
            ))}
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-border-subtle px-4 pb-3 pt-2">
          <input
            value={example.label}
            onChange={(e) => onChange({ label: e.target.value })}
            placeholder="ラベル (例: 参考)"
            className="w-full rounded border border-border-subtle bg-surface-2 px-2 py-1 text-xs text-fg"
          />
          <textarea
            value={example.json_text}
            onChange={(e) => onChange({ json_text: e.target.value })}
            rows={10}
            spellCheck={false}
            className={`w-full rounded border bg-surface-2 px-2 py-1.5 font-mono text-xs leading-relaxed text-fg ${
              jsonError ? "border-critical" : "border-border-subtle"
            }`}
          />
          {jsonError && <div className="text-[11px] text-critical">JSON が不正です: {jsonError}</div>}
          {!jsonError && mismatchKeys.length > 0 && (
            <div className="space-y-0.5">
              {mismatchKeys.map((k) => {
                const line = findKeyLine(example.json_text, k);
                return (
                  <div key={k} className="text-[11px] text-warning">
                    ⚠ <span className="font-mono">{k}</span>
                    {line !== null && ` — ${line} 行目`}
                    <span className="text-fg-subtle"> (出力スキーマの有効値ではありません)</span>
                  </div>
                );
              })}
            </div>
          )}
          {otherIssues.map((issue, i) => (
            <div
              key={`${issue.code}-${i}`}
              className={`rounded px-1.5 py-0.5 text-[11px] ${
                issue.severity === "error" ? "bg-critical-soft text-critical" : "bg-warning-soft text-warning"
              }`}
            >
              {issue.message}
            </div>
          ))}
          <div className="flex items-center justify-between text-[10px]">
            <button
              type="button"
              onClick={onRevert}
              disabled={!changed}
              className="text-fg-subtle hover:text-fg disabled:cursor-not-allowed disabled:opacity-30"
            >
              変更を取り消す
            </button>
            <span className="tnum text-fg-subtle">{example.json_text.length} 字</span>
          </div>
        </div>
      )}
    </div>
  );
}
