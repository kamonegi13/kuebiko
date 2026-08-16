// summarizer 判定基準エディタ UI 再設計 (WP-F2): preview.issues を局在化する純ロジック。
// backend が返す issue (field_id / example_index) を section 単位・example 単位・
// グローバルの 3 バケツに振り分ける。文言 (message) は backend の値をそのまま使い、
// ここではラベルや文言を一切生成しない。

import type { RubricIssue } from "../../../api/promptRubric";

export interface IssueIndex {
  bySection: Map<string, RubricIssue[]>;
  byExample: Map<number, RubricIssue[]>; // key = 1-based example_index (RubricIssue.example_index と同じ)
  global: RubricIssue[];
}

const EMPTY_ISSUES: RubricIssue[] = [];

/** preview.issues を 3 バケツ (section / example / global) に振り分ける。 */
export function buildIssueIndex(issues: readonly RubricIssue[]): IssueIndex {
  const bySection = new Map<string, RubricIssue[]>();
  const byExample = new Map<number, RubricIssue[]>();
  const global: RubricIssue[] = [];

  for (const issue of issues) {
    if (issue.field_id) {
      bySection.set(issue.field_id, [...(bySection.get(issue.field_id) ?? []), issue]);
    } else if (issue.example_index > 0) {
      byExample.set(issue.example_index, [...(byExample.get(issue.example_index) ?? []), issue]);
    } else {
      global.push(issue);
    }
  }
  return { bySection, byExample, global };
}

export function issuesForSection(index: IssueIndex, fieldId: string): RubricIssue[] {
  return index.bySection.get(fieldId) ?? EMPTY_ISSUES;
}

/** exampleIdx は 0-based (draft.examples の index)。RubricIssue.example_index は 1-based。 */
export function issuesForExample(index: IssueIndex, exampleIdx: number): RubricIssue[] {
  return index.byExample.get(exampleIdx + 1) ?? EMPTY_ISSUES;
}

export function countSeverity(issues: readonly RubricIssue[]): { errorCount: number; warnCount: number } {
  let errorCount = 0;
  let warnCount = 0;
  for (const issue of issues) {
    if (issue.severity === "error") errorCount += 1;
    else warnCount += 1;
  }
  return { errorCount, warnCount };
}

/** 複数の issue リスト (グループ内の各カード、出力例の各件 等) を合算する。 */
export function sumIssueCounts(lists: readonly RubricIssue[][]): { errorCount: number; warnCount: number } {
  let errorCount = 0;
  let warnCount = 0;
  for (const list of lists) {
    const c = countSeverity(list);
    errorCount += c.errorCount;
    warnCount += c.warnCount;
  }
  return { errorCount, warnCount };
}
