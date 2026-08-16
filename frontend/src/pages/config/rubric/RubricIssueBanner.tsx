// summarizer 判定基準エディタ UI 再設計 (WP-F4): 保存前検証 (preview.issues) の上部バナー。
// 各行に該当カードへの [移動 →] ボタンを出し、エラー局在化 (要件3) の起点にする。

import { AlertCircle, AlertTriangle } from "lucide-react";
import type { RubricIssue } from "../../../api/promptRubric";

interface RubricIssueBannerProps {
  issues: RubricIssue[];
  onJump: (anchorId: string) => void;
}

function anchorFor(issue: RubricIssue): string | null {
  if (issue.field_id) return `field:${issue.field_id}`;
  if (issue.example_index > 0) return `example:${issue.example_index}`;
  return null;
}

export function RubricIssueBanner({ issues, onJump }: RubricIssueBannerProps) {
  if (issues.length === 0) return null;
  return (
    <div className="space-y-1">
      {issues.map((issue, i) => {
        const anchor = anchorFor(issue);
        const isError = issue.severity === "error";
        return (
          <div
            key={`${issue.code}-${issue.field_id}-${issue.example_index}-${i}`}
            className={`flex items-center gap-2 rounded px-2 py-1 text-xs ${
              isError
                ? "border border-critical bg-critical-soft text-critical"
                : "border border-warning bg-warning-soft text-warning"
            }`}
          >
            {isError ? (
              <AlertCircle className="h-3.5 w-3.5 shrink-0" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            )}
            <span className="flex-1">
              {issue.message}
              {issue.keys.length > 0 && <span className="ml-1 font-mono">({issue.keys.join(", ")})</span>}
            </span>
            {anchor && (
              <button
                type="button"
                onClick={() => onJump(anchor)}
                className="shrink-0 font-medium underline hover:no-underline"
              >
                移動 →
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
